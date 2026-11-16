"""Pillow engine for the Nautilus image resizer: decode, crop, size, encode.

No GTK import lives here, so the whole engine is testable headless.

The engine has exactly one resize pipeline, `render` — crop, size, resize,
encode — used by both callers:

    estimate_files(...)  → predicts (size, bytes) for the dialog
    process_file(...)    → writes those same bytes to disk

Both reach the pipeline through `render_file`, so the byte count the dialog
shows is the byte count the file gets. The decode hint is the only thing the
estimate adapter may vary, and `render_file` applies it for both.
"""

from __future__ import annotations

import gettext
import io
import math
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


def _translation():
    """User-installed catalogs first, then the system ones."""
    for localedir in (str(Path.home() / ".local/share/locale"),
                      "/usr/share/locale"):
        t = gettext.translation("nautilus-image-resizer", localedir=localedir,
                                fallback=True)
        if isinstance(t, gettext.GNUTranslations):
            return t.gettext
    return gettext.gettext


_ = _translation()

SUPPORTED = {"JPEG", "PNG", "WEBP", "BMP", "TIFF"}
LOSSY = {"JPEG", "WEBP"}
QUALITY_LADDER = (95, 85, 75, 65, 55, 45, 35, 25, 15)
SCALE_STEP = 0.85
MIN_EDGE = 64
_MAX_FANOUT = 8  # estimate decodes in flight, process-wide; bounds RAM
# A superseded estimate run can still have tasks in flight while the next one
# starts; one shared gate keeps the decode count at _MAX_FANOUT overall.
_fanout_gate = threading.Semaphore(_MAX_FANOUT)

SAVE_KW = {
    "JPEG": {"quality": 92, "optimize": True},
    "WEBP": {"quality": 92},
    "PNG": {"optimize": True},
    "TIFF": {"compression": "tiff_lzw"},
    "BMP": {},
}


class ResizeError(Exception):
    """User-readable failure."""


@dataclass
class JobResult:
    src: str
    dst: str
    ok: bool
    note: str | None = None
    out_bytes: int | None = None


@dataclass(frozen=True)
class Output:
    """One encoded result: the bytes, the pixel size they hold, any note."""
    data: bytes
    size: tuple[int, int]
    note: str | None = None


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #

# EXIF Orientation values: 2-4 mirror/rotate without swapping the axes, 5-8
# additionally swap width and height.
EXIF_TRANSFORMS = {2, 3, 4, 5, 6, 7, 8}
EXIF_SWAPS_AXES = {5, 6, 7, 8}


def exif_orientation(im: Image.Image) -> int | None:
    try:
        orientation = im.getexif().get(0x0112)
    except (OSError, ValueError, SyntaxError, TypeError):
        return None
    return orientation if isinstance(orientation, int) else None


def oriented_size(im: Image.Image) -> tuple[int, int]:
    """Displayed pixel size from an opened image's header, without decoding."""
    if exif_orientation(im) in EXIF_SWAPS_AXES:
        return im.height, im.width
    return im.width, im.height


def decode_bound(source_size: tuple[int, int],
                 need: tuple[int, int]) -> tuple[int, int]:
    """Coarsest 1/1, 1/2, 1/4, 1/8 decode that still covers `need`.

    Decoders only scale by powers of two, so asking for the exact target buys
    nothing and would give every tiny parameter change its own cache entry.
    """
    width, height = source_size
    factor = 1.0
    while factor > 0.125:
        half = factor / 2
        if width * half < need[0] or height * half < need[1]:
            break
        factor = half
    return max(1, round(width * factor)), max(1, round(height * factor))


def load_image(path: str, max_size: tuple[int, int] | None = None):
    """Return (transposed image, source format, exif bytes, icc bytes).

    `max_size` is a pixel bound the caller will never enlarge beyond; JPEG
    decoders then hand back a DCT-scaled raster instead of the full one.
    """
    try:
        im = Image.open(path)
        if max_size is not None and im.mode in ("RGB", "L"):
            im.draft(im.mode, (max(1, max_size[0]), max(1, max_size[1])))
        im.load()
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ResizeError(_("not a readable image")) from exc
    if getattr(im, "is_animated", False) or getattr(im, "n_frames", 1) > 1:
        raise ResizeError(_("animated/multi-frame images are not supported"))
    fmt = im.format
    if fmt not in SUPPORTED:
        raise ResizeError(_("{} files are not supported").format(fmt))
    icc = im.info.get("icc_profile")
    # exif_transpose copies the raster even when the image is already upright,
    # so only pay for it when the tag asks for a transform. It is also what
    # drops the Orientation tag, keeping viewers from rotating the written file
    # a second time.
    if exif_orientation(im) in EXIF_TRANSFORMS:
        im = ImageOps.exif_transpose(im)
    exif = im.info.get("exif")
    if im.mode not in ("RGB", "RGBA", "L", "LA", "P", "CMYK", "I;16", "I"):
        im = im.convert("RGB")
    return im, fmt, exif, icc


def header_size(path: str) -> tuple[int, int]:
    """Displayed pixel size from the header; no raster is decoded."""
    with Image.open(path) as probe:
        return oriented_size(probe)


# Decoding a 24 MP photo costs ~100 ms, and every spinbox notch re-decoded
# every selected file. Keep recent decodes while the file and the pixel bound
# are unchanged; the estimate only reads them, so one entry can serve many
# passes.
CACHE_BUDGET = 256 * 1024 * 1024
_cache_lock = threading.Lock()
# key -> ((image, fmt, exif, icc), footprint in bytes)
_cache: "OrderedDict[tuple, tuple[tuple, int]]" = OrderedDict()


def cached_load_image(path: str, max_size: tuple[int, int] | None = None):
    """`load_image`, reusing a previous decode of the same file. Read-only."""
    try:
        info = Path(path).stat()
    except OSError:
        return load_image(path, max_size)
    key = (path, info.st_mtime_ns, info.st_size, max_size)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit[0]
    loaded = load_image(path, max_size)
    footprint = loaded[0].width * loaded[0].height * len(loaded[0].getbands())
    with _cache_lock:
        _cache[key] = (loaded, footprint)
        _cache.move_to_end(key)
        total = sum(weight for _entry, weight in _cache.values())
        while total > CACHE_BUDGET and len(_cache) > 1:
            _old_key, (_old_entry, old_weight) = _cache.popitem(last=False)
            total -= old_weight
    return loaded


# --------------------------------------------------------------------------- #
# Sizing and cropping
# --------------------------------------------------------------------------- #

def compute_size(w: int, h: int, mode: str, *, percent: float = 0.0,
                 box: tuple[int, int] | None = None,
                 lock: bool = True) -> tuple[int, int]:
    """Target pixel size, every edge clamped to >= 1."""
    if mode == "percent":
        size = (round(w * percent / 100.0), round(h * percent / 100.0))
    elif mode == "dimensions":
        assert box is not None
        if lock:
            s = min(box[0] / w, box[1] / h)
            size = (round(w * s), round(h * s))
        else:
            size = (box[0], box[1])
    else:
        raise ResizeError(f"unknown sizing mode {mode!r}")
    return (max(1, size[0]), max(1, size[1]))


CROP_PRESETS = {
    "free": None,
    "original": None,
    "1:1": 1.0,
    "5:4": 5 / 4,
    "4:3": 4 / 3,
    "3:2": 3 / 2,
    "16:10": 16 / 10,
    "16:9": 16 / 9,
    "21:9": 21 / 9,
    "19:6": 19 / 6,
}


def crop_box(size: tuple[int, int], crop: dict) -> tuple[int, int, int, int]:
    """Source-pixel rectangle (x, y, w, h) for a fractional crop spec."""
    width, height = size
    x = max(0, min(width - 1, round(crop["fx"] * width)))
    y = max(0, min(height - 1, round(crop["fy"] * height)))
    cw = max(1, min(width - x, round(crop["fw"] * width)))
    ch = max(1, min(height - y, round(crop["fh"] * height)))
    return x, y, cw, ch


def crop_fit_rect(size: tuple[int, int], aspect) -> tuple[int, int, int, int]:
    """Largest centred rectangle of `aspect` that fits inside the image."""
    width, height = size
    if not aspect:
        return 0, 0, width, height
    w = min(width, height * aspect)
    h = w / aspect
    return (round((width - w) / 2), round((height - h) / 2),
            max(1, round(w)), max(1, round(h)))


def crop_refit_rect(rect: tuple[int, int, int, int], aspect) -> tuple[int, int, int, int]:
    """Largest rectangle of `aspect` centred inside `rect`."""
    x, y, width, height = rect
    if not aspect:
        return rect
    w = min(width, height * aspect)
    h = w / aspect
    return (round(x + (width - w) / 2), round(y + (height - h) / 2),
            max(1, round(w)), max(1, round(h)))


def crop_drag_rect(ax: float, ay: float, bx: float, by: float,
                   size: tuple[int, int], aspect=None) -> tuple[float, float, float, float]:
    """Drag rectangle in source pixels, anchored at (ax, ay), clamped to `size`."""
    width, height = size
    ax = min(width, max(0.0, ax))
    ay = min(height, max(0.0, ay))
    bx = min(width, max(0.0, bx))
    by = min(height, max(0.0, by))
    if not aspect:
        x, y = min(ax, bx), min(ay, by)
        return x, y, min(abs(bx - ax), width - x), min(abs(by - ay), height - y)
    dx, dy = bx - ax, by - ay
    room_x = (width - ax) if dx >= 0 else ax
    room_y = (height - ay) if dy >= 0 else ay
    w = min(max(abs(dx), abs(dy) * aspect), room_x, room_y * aspect)
    h = w / aspect
    return (ax if dx >= 0 else ax - w), (ay if dy >= 0 else ay - h), w, h


def crop_move_rect(rect, dx, dy, size) -> tuple[int, int, int, int]:
    """Translate a crop rect by (dx, dy), keeping it inside `size`."""
    width, height = size
    x, y, w, h = (round(v) for v in rect)
    w, h = min(width, max(16, w)), min(height, max(16, h))
    x = max(0, min(x + round(dx), width - w))
    y = max(0, min(y + round(dy), height - h))
    return x, y, w, h


def crop_resize_rect(rect, dw, dh, size, aspect=None) -> tuple[int, int, int, int]:
    """Grow/shrink a crop rect from its top-left corner, clamped to `size`."""
    width, height = size
    x, y, w, h = (round(v) for v in rect)
    if aspect:
        if dw == 0 and dh != 0:
            h = max(16, h + round(dh))
            w = round(h * aspect)
        else:
            w = max(16, w + round(dw))
        w = min(w, width - x)
        h = round(w / aspect)
        if h > height - y:
            h = max(16, height - y)
            w = min(width - x, max(16, round(h * aspect)))
    else:
        w = min(max(16, w + round(dw)), width - x)
        h = min(max(16, h + round(dh)), height - y)
    return x, y, w, h


def apply_crop(im: Image.Image, crop: dict) -> Image.Image:
    x, y, cw, ch = crop_box(im.size, crop)
    if (x, y, cw, ch) == (0, 0, im.width, im.height):
        return im
    return im.crop((x, y, x + cw, y + ch))


# --------------------------------------------------------------------------- #
# The resize pipeline
# --------------------------------------------------------------------------- #

def _encode(im: Image.Image, fmt: str, exif, icc, quality: int | None) -> bytes:
    kw = dict(SAVE_KW[fmt])
    if quality is not None and fmt in LOSSY:
        kw["quality"] = quality
    buf = io.BytesIO()
    extra = {}
    if fmt != "BMP":
        if exif:
            extra["exif"] = exif
        if icc:
            extra["icc_profile"] = icc
    im.save(buf, format=fmt, **kw, **extra)
    return buf.getvalue()


def encode_target(im: Image.Image, fmt: str, exif, icc, max_bytes: int) -> Output:
    """Encode `im` within `max_bytes`, dropping quality then resolution.

    A byte target can only be measured on the raster it encodes, so this runs
    on whatever resolution it is handed: the full one, always — the decode
    hint is None for this mode.
    """
    cur = im
    w, h = im.size
    while True:
        if fmt in LOSSY:
            data, fits = b"", False
            for quality in QUALITY_LADDER:
                data = _encode(cur, fmt, exif, icc, quality)
                if len(data) <= max_bytes:
                    fits = True
                    break
        else:
            data = _encode(cur, fmt, exif, icc, None)
            fits = len(data) <= max_bytes
        if fits:
            return Output(data, (w, h))
        if max(w, h) <= MIN_EDGE:
            return Output(data, (w, h),
                          _("target not reached (wrote smallest result)"))
        w = max(1, round(w * SCALE_STEP))
        h = max(1, round(h * SCALE_STEP))
        cur = cur.resize((w, h), Image.Resampling.LANCZOS)


def render(im: Image.Image, fmt: str, exif, icc, mode: str, params: dict,
           source_size: tuple[int, int] | None = None) -> Output:
    """Crop, size, resize and encode one image. Writes nothing.

    `source_size` is the original pixel size when `im` was decoded smaller
    than the file; the sizing maths must use it, not the decode size.
    """
    size = source_size or im.size
    crop = params.get("crop")
    if crop:
        im = apply_crop(im, crop)
        _x, _y, cw, ch = crop_box(size, crop)
        size = (cw, ch)
    if mode == "filesize":
        return encode_target(im, fmt, exif, icc, int(params["max_bytes"]))
    if mode == "dimensions":
        size = compute_size(size[0], size[1], "dimensions",
                            box=(int(params["width"]), int(params["height"])),
                            lock=bool(params.get("lock", True)))
    elif mode == "percent":
        size = compute_size(size[0], size[1], "percent",
                            percent=float(params["percent"]))
    else:
        raise ResizeError(f"unknown sizing mode {mode!r}")
    scaled = im.resize(size, Image.Resampling.LANCZOS) if size != im.size else im
    return Output(_encode(scaled, fmt, exif, icc, None), size)


def decode_hint(mode: str, params: dict,
                header: tuple[int, int]) -> tuple[int, int] | None:
    """Source-pixel size the decode must still cover, or None for the full one.

    Depends only on the header and the request, so both adapters compute the
    same hint: the pixels the estimate measures are the pixels the job writes.

    The hint is expressed in *source* pixels, not target pixels: a crop keeps
    only a fraction of the decoded raster, so the whole image must be decoded
    at target / crop-fraction for the surviving region to still cover it.
    """
    crop = params.get("crop")
    if crop:
        fx, fy = float(crop["fw"]), float(crop["fh"])
        region = (max(1, round(header[0] * fx)), max(1, round(header[1] * fy)))
    else:
        fx = fy = 1.0
        region = header
    if mode == "dimensions":
        # Never shrinks below the request unless the region is smaller than it,
        # and an upscale needs every pixel the region has.
        need = (min(int(params["width"]), region[0]),
                min(int(params["height"]), region[1]))
    elif mode == "percent":
        scale = float(params["percent"]) / 100.0
        need = (min(region[0], max(1, round(region[0] * scale))),
                min(region[1], max(1, round(region[1] * scale))))
    else:
        return None   # a byte target says nothing about pixels
    return (min(header[0], math.ceil(need[0] / fx)),
            min(header[1], math.ceil(need[1] / fy)))


def render_file(path: str, mode: str, params: dict,
                *, cache: bool = False) -> Output:
    """The one pipeline: header → decode hint → decode → render.

    `cache` is for the estimate, which re-reads the same files on every
    parameter change; a job reads each file once and leaves the cache alone.
    """
    header = header_size(path)
    target = decode_hint(mode, params, header)
    bound = None
    if target is not None:
        bound = decode_bound(header, target)
        if bound == header:
            bound = None           # the hint buys nothing; skip the draft call
    load = cached_load_image if cache else load_image
    im, fmt, exif, icc = load(path, bound)
    return render(im, fmt, exif, icc, mode, params, header)


def estimate_files(paths: list[str], mode: str, params: dict,
                   should_abort=None) -> tuple[list[dict], int, str | None]:
    """Estimate every path in parallel, then reconcile in input order.

    Returns (results, skipped, error): the per-image estimates in input order,
    how many paths could not be read, and the last such failure message.
    `should_abort` is polled before each task; an aborted task contributes
    nothing (not even to `skipped`).
    """
    if not paths:
        return [], 0, None

    def estimate_path(path):
        if should_abort is not None and should_abort():
            return None
        try:
            # The gate, not the pool size, is what bounds concurrent decodes:
            # a superseded run's tasks may still be in flight here.
            with _fanout_gate:
                out = render_file(path, mode, params, cache=True)
                # Drop the encoded bytes here; the dialog only reads the number.
                return ("ok", {"size": out.size, "bytes": len(out.data),
                               "note": out.note})
        except Exception as exc:
            # One unreadable file must not kill the batch.
            return ("fail", f"{type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=min(len(paths), _MAX_FANOUT)) as ex:
        outcomes = list(ex.map(estimate_path, paths))

    results: list[dict] = []
    skipped = 0
    error = None
    for outcome in outcomes:
        if outcome is None:
            continue
        kind, payload = outcome
        if kind == "ok":
            results.append(payload)
        else:
            skipped += 1
            error = payload
    return results, skipped, error


def process_file(src: str, dst: str, mode: str, params: dict) -> JobResult:
    """Resize one file. Never raises: every failure becomes a JobResult.

    The written length is the encoded length; nothing is re-read to learn what
    the encoder already returned.
    """
    try:
        out = render_file(src, mode, params)
        Path(dst).write_bytes(out.data)
        return JobResult(src=src, dst=dst, ok=True, note=out.note,
                         out_bytes=len(out.data))
    except Exception as exc:
        note = (str(exc) if isinstance(exc, ResizeError)
                else f"{type(exc).__name__}: {exc}")
        return JobResult(src=src, dst=dst, ok=False, note=note)
