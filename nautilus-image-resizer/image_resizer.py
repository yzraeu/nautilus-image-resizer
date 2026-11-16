#!/usr/bin/python3
"""Right-click image resizer for GNOME Files (Nautilus).

Engine: Pillow. GUI: GTK4 + libadwaita. Run with image paths as arguments:

    image_resizer.py photo.jpg [more.png ...]

Importing this module has no side effects beyond defining the engine.
"""

from __future__ import annotations

import io
import math
import sys
import threading
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

APP_ID = "dev.izzy.image-resizer"
APP_NAME = "Nautilus Image Resizer"
APP_VERSION = "1.0.0"
APP_REPO = "https://github.com/yzraeu/nautilus-image-resizer"
APP_AUTHOR = "yzraeu"

SUPPORTED = {"JPEG", "PNG", "WEBP", "BMP", "TIFF"}
LOSSY = {"JPEG", "WEBP"}
QUALITY_LADDER = (95, 85, 75, 65, 55, 45, 35, 25, 15)
SCALE_STEP = 0.85
MIN_EDGE = 64

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


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

def load_image(path: str):
    """Return (transposed image, source format, exif bytes, icc bytes)."""
    try:
        im = Image.open(path)
        im.load()
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ResizeError("not a readable image") from exc
    if getattr(im, "is_animated", False) or getattr(im, "n_frames", 1) > 1:
        raise ResizeError("animated/multi-frame images are not supported")
    fmt = im.format
    if fmt not in SUPPORTED:
        raise ResizeError(f"{fmt} files are not supported")
    icc = im.info.get("icc_profile")
    im = ImageOps.exif_transpose(im)
    # EXIF of the *transposed* image: exif_transpose drops the Orientation tag,
    # so the written file is never rotated a second time by viewers.
    exif = im.info.get("exif")
    if im.mode not in ("RGB", "RGBA", "L", "LA", "P", "CMYK", "I;16", "I"):
        im = im.convert("RGB")
    return im, fmt, exif, icc


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


def apply_crop(im: Image.Image, crop: dict) -> Image.Image:
    x, y, cw, ch = crop_box(im.size, crop)
    if (x, y, cw, ch) == (0, 0, im.width, im.height):
        return im
    return im.crop((x, y, x + cw, y + ch))


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


def _fit_encode(im: Image.Image, fmt: str, exif, icc,
                max_bytes: int) -> tuple[bytes, bool]:
    """Return (best encoding, whether it meets max_bytes)."""
    if fmt in LOSSY:
        data = b""
        for q in QUALITY_LADDER:
            data = _encode(im, fmt, exif, icc, q)
            if len(data) <= max_bytes:
                return data, True
        return data, False
    data = _encode(im, fmt, exif, icc, None)
    return data, len(data) <= max_bytes


def encode_target(im: Image.Image, fmt: str, exif, icc,
                  max_bytes: int | None = None) -> tuple[bytes, str | None, tuple[int, int]]:
    """Encode `im` within max_bytes. Returns (bytes, note, encoded pixel size)."""
    if max_bytes is None:
        return _encode(im, fmt, exif, icc, None), None, im.size

    cur = im
    w, h = im.size
    while True:
        data, fits = _fit_encode(cur, fmt, exif, icc, max_bytes)
        if fits:
            return data, None, (w, h)
        if max(w, h) <= MIN_EDGE:
            return data, "target not reached (wrote smallest result)", (w, h)
        w = max(1, round(w * SCALE_STEP))
        h = max(1, round(h * SCALE_STEP))
        cur = cur.resize((w, h), Image.Resampling.LANCZOS)


def save_image(im: Image.Image, path: str, fmt: str, exif, icc,
               max_bytes: int | None = None) -> str | None:
    """Write one encoding of `im` to `path`. Returns a note when applicable."""
    data, note, _size = encode_target(im, fmt, exif, icc, max_bytes)
    Path(path).write_bytes(data)
    return note


def estimate(im: Image.Image, fmt: str, exif, icc, mode: str, params: dict) -> dict:
    """Predicted pixel size and byte count for the dialog; writes nothing."""
    crop = params.get("crop")
    if crop:
        im = apply_crop(im, crop)
    if mode == "filesize":
        data, note, size = encode_target(im, fmt, exif, icc,
                                         int(params["max_bytes"]))
        return {"size": size, "bytes": len(data), "note": note}
    if mode == "dimensions":
        size = compute_size(im.width, im.height, "dimensions",
                            box=(int(params["width"]), int(params["height"])),
                            lock=bool(params.get("lock", True)))
    elif mode == "percent":
        size = compute_size(im.width, im.height, "percent",
                            percent=float(params["percent"]))
    else:
        raise ResizeError(f"unknown sizing mode {mode!r}")
    scaled = im.resize(size, Image.Resampling.LANCZOS) if size != im.size else im
    data = _encode(scaled, fmt, exif, icc, None)
    return {"size": size, "bytes": len(data), "note": None}


def process_file(src: str, dst: str, mode: str, params: dict) -> JobResult:
    """Resize one file. Never raises."""
    try:
        im, fmt, exif, icc = load_image(src)
        crop = params.get("crop")
        if crop:
            im = apply_crop(im, crop)
        note = None
        if mode == "dimensions":
            size = compute_size(im.width, im.height, mode,
                                box=(int(params["width"]), int(params["height"])),
                                lock=bool(params.get("lock", True)))
            im = im.resize(size, Image.Resampling.LANCZOS)
            note = save_image(im, dst, fmt, exif, icc)
        elif mode == "percent":
            size = compute_size(im.width, im.height, mode,
                                percent=float(params["percent"]))
            im = im.resize(size, Image.Resampling.LANCZOS)
            note = save_image(im, dst, fmt, exif, icc)
        elif mode == "filesize":
            note = save_image(im, dst, fmt, exif, icc,
                              max_bytes=int(params["max_bytes"]))
        else:
            raise ResizeError(f"unknown sizing mode {mode!r}")
        return JobResult(src=src, dst=dst, ok=True, note=note,
                         out_bytes=Path(dst).stat().st_size)
    except ResizeError as exc:
        return JobResult(src=src, dst=dst, ok=False, note=str(exc))
    except (OSError, ValueError, KeyError) as exc:
        return JobResult(src=src, dst=dst, ok=False, note=f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# Dialog
# --------------------------------------------------------------------------- #

def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def main(argv: list[str]) -> int:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("GdkPixbuf", "2.0")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import (Adw, Gdk, GdkPixbuf, Gio, GLib, Graphene, Gsk,
                               Gtk)

    GLib.set_application_name(APP_NAME)
    GLib.set_prgname(APP_NAME.lower().replace(" ", "-"))

    paths = [str(p) for p in argv]
    if not paths:
        print("Usage: image_resizer.py IMAGE [IMAGE ...]", file=sys.stderr)
        return 1

    # First readable image drives thumbnail and the dimension prefill.
    first_size: tuple[int, int] | None = None
    first_bytes = 0
    first_path = paths[0]
    for p in paths:
        try:
            with Image.open(p) as probe:
                probe.load()
                transposed = ImageOps.exif_transpose(probe)
                first_size = (transposed.width, transposed.height)
            first_bytes = Path(p).stat().st_size
            first_path = p
            break
        except Exception:
            continue

    src_image = None
    if first_size is not None:
        try:
            src_image = load_image(first_path)
        except ResizeError:
            src_image = None

    app = Adw.Application(application_id=APP_ID,
                          flags=Gio.ApplicationFlags.NON_UNIQUE)

    def on_activate(_app):
        n = len(paths)
        win = Adw.ApplicationWindow(application=_app)
        win.set_title(APP_NAME)
        # The Percentage and File Size page icons ship with the app; register
        # their private theme root so the icon theme can find them.
        for icons in (Path(__file__).resolve().parent / "icons",
                      Path(__file__).resolve().parent.parent / "data" / "icons"):
            if (icons / "hicolor").is_dir():
                Gtk.IconTheme.get_for_display(win.get_display()).add_search_path(
                    str(icons))
                break
        win.set_default_size(560, 860)
        win.set_size_request(460, 620)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        subtitle = f"{n} images selected" if n > 1 else "Select the size and hit Resize"
        title = Adw.WindowTitle(title=APP_NAME, subtitle=subtitle)
        header.set_title_widget(title)

        menu = Gio.Menu()
        menu.append(f"About {APP_NAME}", "win.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic",
                                     menu_model=menu,
                                     tooltip_text="Main Menu")
        header.pack_end(menu_button)

        def show_about(*_a):
            about = Adw.AboutDialog(
                application_name=APP_NAME,
                application_icon="image-x-generic",
                developer_name=APP_AUTHOR,
                developers=[APP_AUTHOR],
                version=APP_VERSION,
                website=APP_REPO,
                issue_url=f"{APP_REPO}/issues",
                copyright=f"© 2026 {APP_AUTHOR}",
                license_type=Gtk.License.GPL_3_0,
            )
            about.add_acknowledgement_section(
                "Dependencies",
                ["Pillow (MIT-CMU)",
                 "GTK 4 (LGPL-2.1-or-later)",
                 "libadwaita (LGPL-2.1-or-later)",
                 "PyGObject (LGPL-2.1-or-later)",
                 "Nautilus (GPL-3.0-or-later)",
                 "nautilus-python (GPL-3.0-or-later)"])
            about.present(win)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", show_about)
        win.add_action(about_action)

        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(12)
        box.set_margin_end(12)

        # --- preview ------------------------------------------------------ #
        preview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        preview.set_margin_top(12)
        preview.set_margin_start(12)
        preview.set_margin_end(12)

        def texture_at(path, size):
            # Decode once at preview size; the widget scales it with the window.
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    return Gdk.Texture.new_for_pixbuf(pixbuf)
            except Exception:
                return None

        class PreviewDeck(Gtk.Widget):
            """The first image on top; the rest peek out as cards behind it."""

            PAD = 10  # px each back card is offset from the one in front of it

            def __init__(self, front, backs, source_size):
                super().__init__()
                self._front = front
                self._backs = backs        # nearest card first
                self._source_size = source_size

            def do_measure(self, orientation, _for_size):
                if orientation == Gtk.Orientation.VERTICAL:
                    return 120, 280, -1, -1
                return 0, 0, -1, -1

            def top_card(self):
                """Rect of the front card, deck and peeking cards included."""
                width, height = self.get_width(), self.get_height()
                inset = self.PAD * len(self._backs)
                avail_w, avail_h = max(1, width - inset), max(1, height - inset)
                if self._source_size:
                    source_w, source_h = self._source_size
                    scale = min(avail_w / source_w, avail_h / source_h)
                    card_w, card_h = source_w * scale, source_h * scale
                else:
                    card_w, card_h = avail_w, avail_h
                # The stack takes card + inset; center that block in the widget.
                return ((width - card_w - inset) / 2,
                        (height - card_h - inset) / 2,
                        card_w, card_h)

            def image_geometry(self):
                """Scale and offset mapping the source image onto the widget."""
                if self._source_size is None:
                    return None
                x, y, width, height = self.top_card()
                scale = width / self._source_size[0]
                return (scale, x, y)

            def _card(self, snapshot, texture, rect, radius, alpha, cover):
                if texture is None:
                    return
                x, y, width, height = rect
                area = Graphene.Rect().init(x, y, width, height)
                rounded = Gsk.RoundedRect()
                rounded.init_from_rect(area, radius)
                snapshot.push_rounded_clip(rounded)
                snapshot.push_opacity(alpha)
                if cover:
                    # Crop to fill the card instead of squashing the photo.
                    texture_w, texture_h = texture.get_width(), texture.get_height()
                    scale = max(width / texture_w, height / texture_h)
                    draw_w, draw_h = texture_w * scale, texture_h * scale
                    snapshot.append_texture(
                        texture,
                        Graphene.Rect().init(x + (width - draw_w) / 2,
                                             y + (height - draw_h) / 2,
                                             draw_w, draw_h))
                else:
                    snapshot.append_texture(texture, area)
                snapshot.pop()
                snapshot.pop()

            def do_snapshot(self, snapshot):
                x, y, width, height = self.top_card()
                radius = 8.0
                for depth in range(len(self._backs), 0, -1):
                    offset = self.PAD * depth
                    alpha = 0.65 + 0.2 * (len(self._backs) - depth)
                    self._card(snapshot, self._backs[depth - 1],
                               (x + offset, y + offset, width, height),
                               radius, alpha, True)
                self._card(snapshot, self._front, (x, y, width, height),
                           radius, 1.0, False)

        front_texture = texture_at(first_path, 900)
        back_textures = [t for t in (texture_at(p, 400) for p in paths[1:5]) if t]
        pic = PreviewDeck(front_texture, back_textures, first_size)

        overlay = Gtk.Overlay()
        overlay.set_child(pic)
        crop_area = Gtk.DrawingArea()
        crop_area.set_can_target(False)
        overlay.add_overlay(crop_area)
        preview.append(overlay)

        crop_state = {"crop": None, "dims": None, "origin": None, "live": None,
                      "on": False, "aspect": None, "preset": "free",
                      "portrait": False}

        def display_geometry():
            if not first_size:
                return None
            return pic.image_geometry()

        def to_source(x, y):
            geometry = display_geometry()
            if geometry is None:
                return 0.0, 0.0
            scale, offset_x, offset_y = geometry
            return (min(first_size[0], max(0.0, (x - offset_x) / scale)),
                    min(first_size[1], max(0.0, (y - offset_y) / scale)))

        def draw_crop(_area, cr, width, height):
            geometry = display_geometry()
            if geometry is None or first_size is None:
                return
            if crop_state["live"] is not None:
                x, y, cw, ch = crop_state["live"]
            elif crop_state["crop"] is not None:
                x, y, cw, ch = crop_box(first_size, crop_state["crop"])
            else:
                return
            scale, offset_x, offset_y = geometry
            rx, ry = offset_x + x * scale, offset_y + y * scale
            rw, rh = cw * scale, ch * scale
            cr.set_source_rgba(0, 0, 0, 0.45)
            for band in ((0, 0, width, ry), (0, ry + rh, width, height - ry - rh),
                         (0, ry, rx, rh), (rx + rw, ry, width - rx - rw, rh)):
                cr.rectangle(*band)
                cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.set_line_width(2)
            cr.rectangle(rx, ry, rw, rh)
            cr.stroke()

        crop_area.set_draw_func(draw_crop)

        def current_aspect():
            if first_size is None:
                return None
            name = crop_state["preset"]
            if name == "free":
                return None
            base = ((first_size[0] / first_size[1]) if name == "original"
                    else CROP_PRESETS[name])
            if base is None:
                return None
            return (1.0 / base) if crop_state["portrait"] else base

        def commit_crop(rect):
            previous = crop_state.get("dims") or first_size
            if rect is None:
                crop_state["crop"] = None
                crop_state["dims"] = first_size
            else:
                x, y, cw, ch = rect
                crop_state["crop"] = {"fx": x / first_size[0], "fy": y / first_size[1],
                                      "fw": cw / first_size[0], "fh": ch / first_size[1]}
                crop_state["dims"] = (round(cw), round(ch))
            resize_fields(previous, crop_state["dims"])
            update_crop_label()
            update_crop_controls()
            crop_area.queue_draw()
            queue_estimate()

        def reapply_aspect():
            crop_state["aspect"] = current_aspect()
            update_crop_controls()
            if not crop_state["on"] or not first_size or crop_state["aspect"] is None:
                queue_estimate()
                return
            if crop_state["crop"] is not None:
                rect = crop_refit_rect(crop_box(first_size, crop_state["crop"]),
                                       crop_state["aspect"])
            else:
                rect = crop_fit_rect(first_size, crop_state["aspect"])
            commit_crop(rect)

        def on_crop_begin(_gesture, start_x, start_y):
            crop_state["origin"] = (start_x, start_y)
            crop_state["live"] = None

        def on_crop_update(_gesture, offset_x, offset_y):
            origin = crop_state["origin"]
            if origin is None:
                return
            x0, y0 = to_source(origin[0], origin[1])
            x1, y1 = to_source(origin[0] + offset_x, origin[1] + offset_y)
            crop_state["live"] = crop_drag_rect(x0, y0, x1, y1, first_size,
                                                crop_state["aspect"])
            crop_area.queue_draw()

        def on_crop_end(_gesture, _offset_x, _offset_y):
            live = crop_state["live"]
            crop_state["origin"] = None
            crop_state["live"] = None
            if live is not None and live[2] >= 16 and live[3] >= 16:
                commit_crop((round(live[0]), round(live[1]),
                             round(live[2]), round(live[3])))
            else:
                commit_crop(None)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", on_crop_begin)
        drag.connect("drag-update", on_crop_update)
        drag.connect("drag-end", on_crop_end)
        crop_area.add_controller(drag)

        total_bytes = 0
        for p in paths:
            try:
                total_bytes += Path(p).stat().st_size
            except OSError:
                continue

        if n > 1:
            details = [_human_size(total_bytes), f"{n} images selected"]
        else:
            details = []
            details.append(f"{first_size[0]}×{first_size[1]}" if first_size
                           else "unreadable")
            if first_bytes:
                details.append(_human_size(first_bytes))

        caption = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name = Gtk.Label(label=Path(first_path).name, wrap=True,
                         justify=Gtk.Justification.CENTER)
        detail = Gtk.Label(label=" · ".join(details), wrap=True,
                           justify=Gtk.Justification.CENTER)
        detail.add_css_class("dim-label")
        caption.append(name)
        caption.append(detail)
        preview.append(caption)

        crop_label = Gtk.Label(label="", wrap=True, xalign=0.0)
        crop_label.add_css_class("dim-label")
        crop_label.set_visible(False)
        preview.append(crop_label)

        def update_crop_label():
            crop = crop_state["crop"]
            if crop is None:
                crop_label.set_visible(crop_state["on"])
                crop_label.set_text("Drag on the preview to select the crop region")
                return
            x, y, cw, ch = crop_box(first_size, crop)
            crop_label.set_visible(True)
            crop_label.set_text(f"Crop {cw} × {ch} px at {x}, {y}")

        def set_crop_enabled(active):
            if crop_state["on"] == active and crop_switch.get_active() == active:
                return
            crop_state["on"] = active
            crop_switch.set_active(active)
            crop_area.set_can_target(active)
            if active:
                crop_area.set_cursor(Gdk.Cursor.new_from_name("crosshair"))
                crop_state["aspect"] = current_aspect()
                if crop_state["aspect"] is not None and first_size:
                    commit_crop(crop_fit_rect(first_size, crop_state["aspect"]))
                else:
                    commit_crop(crop_state["crop"])
            else:
                crop_state["origin"] = None
                crop_state["live"] = None
                commit_crop(None)
            update_crop_controls()

        # --- settings ----------------------------------------------------- #
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(12)
        box.set_margin_end(12)

        stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=stack,
                                    policy=Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_halign(Gtk.Align.CENTER)
        switcher.set_margin_start(12)
        switcher.set_margin_end(12)
        switcher.set_margin_top(6)

        def new_page():
            # A plain box, not Adw.PreferencesPage: that widget is itself a
            # scroller and would swallow the natural height this dialog needs.
            view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            return view

        # Percentage
        page_pct = new_page()
        grp_pct = Adw.PreferencesGroup()
        pct_adj = Gtk.Adjustment(value=50, lower=1, upper=1000, step_increment=1)
        pct_row = Adw.SpinRow(adjustment=pct_adj, climb_rate=1, digits=0)
        pct_row.set_title("Scale (%)")
        grp_pct.add(pct_row)
        pct_out = Adw.ActionRow(title="Output")
        pct_out.set_subtitle("—")
        grp_pct.add(pct_out)
        page_pct.append(grp_pct)
        stack.add_titled(page_pct, "percent", "Percentage")
        stack.get_page(page_pct).set_icon_name("percent-symbolic")

        # Dimensions
        page_dim = new_page()
        grp_dim = Adw.PreferencesGroup()
        w0, h0 = first_size if first_size else (1920, 1080)
        lock = first_size is not None

        w_adj = Gtk.Adjustment(value=w0, lower=1, upper=30000, step_increment=1)
        h_adj = Gtk.Adjustment(value=h0, lower=1, upper=30000, step_increment=1)
        w_row = Adw.SpinRow(adjustment=w_adj, climb_rate=1, digits=0)
        w_row.set_title("Width (px)")
        h_row = Adw.SpinRow(adjustment=h_adj, climb_rate=1, digits=0)
        h_row.set_title("Height (px)")

        chain = Gtk.ToggleButton()
        chain.set_valign(Gtk.Align.CENTER)
        chain.set_active(lock)
        chain.set_sensitive(first_size is not None)
        chain_icon = Gtk.Image.new_from_icon_name(
            "changes-prevent-symbolic" if lock else "changes-allow-symbolic")
        chain_icon.set_pixel_size(16)
        chain.set_child(chain_icon)
        w_row.add_suffix(chain)

        def row_spin_button(row):
            if isinstance(row, Gtk.SpinButton):
                return row
            child = row.get_first_child()
            while child is not None:
                found = row_spin_button(child)
                if found is not None:
                    return found
                child = child.get_next_sibling()
            return None

        # The value is a suffix too, and it makes the row's suffix box fill the
        # row. Put the lock in front of it, with a spacer taking the slack, so
        # the icon ends up immediately left of the value.
        suffix_box = chain.get_parent()
        spin_button = row_spin_button(w_row)
        if suffix_box is not None and spin_button is not None:
            spin_button.set_hexpand(False)
            chain.unparent()
            suffix_box.insert_child_after(chain, None)
            spacer = Gtk.Box()
            spacer.set_hexpand(True)
            suffix_box.insert_child_after(spacer, None)
        syncing = {"active": False}

        def current_ratio():
            """Aspect of the region the resize applies to (crop aware)."""
            if first_size is None:
                return None
            if crop_state["crop"]:
                _x, _y, cw, ch = crop_box(first_size, crop_state["crop"])
            else:
                cw, ch = first_size
            return cw / ch

        def resize_fields(old_dims, new_dims):
            """Keep the dimension fields in step with the crop rectangle."""
            if not old_dims or not new_dims:
                return
            if (int(w_adj.get_value()), int(h_adj.get_value())) != tuple(old_dims):
                return
            syncing["active"] = True
            w_adj.set_value(new_dims[0])
            h_adj.set_value(new_dims[1])
            syncing["active"] = False

        def set_chain_state(active: bool):
            chain_icon.set_from_icon_name(
                "changes-prevent-symbolic" if active else "changes-allow-symbolic")
            label = "Aspect ratio locked" if active else "Aspect ratio unlocked"
            chain.set_tooltip_text(label)
            chain.update_property([Gtk.AccessibleProperty.LABEL], [label])

        set_chain_state(lock)

        def on_chain_toggled(btn):
            set_chain_state(btn.get_active())

        chain.connect("toggled", on_chain_toggled)

        def on_w_changed(row, _pspec):
            ratio = current_ratio()
            if not chain.get_active() or ratio is None or syncing["active"]:
                return
            syncing["active"] = True
            h_adj.set_value(min(30000, max(1, round(row.get_value() / ratio))))
            syncing["active"] = False

        def on_h_changed(row, _pspec):
            ratio = current_ratio()
            if not chain.get_active() or ratio is None or syncing["active"]:
                return
            syncing["active"] = True
            w_adj.set_value(min(30000, max(1, round(row.get_value() * ratio))))
            syncing["active"] = False

        w_row.connect("notify::value", on_w_changed)
        h_row.connect("notify::value", on_h_changed)

        grp_dim.add(w_row)
        grp_dim.add(h_row)
        dim_out = Adw.ActionRow(title="Output")
        dim_out.set_subtitle("—")
        grp_dim.add(dim_out)
        page_dim.append(grp_dim)
        stack.add_titled(page_dim, "dimensions", "Dimensions")
        stack.get_page(page_dim).set_icon_name("zoom-fit-best-symbolic")

        # File size
        page_size = new_page()
        grp_size = Adw.PreferencesGroup()
        size_adj = Gtk.Adjustment(value=500, lower=1, upper=102400,
                                  step_increment=1)
        size_row = Adw.SpinRow(adjustment=size_adj, climb_rate=1, digits=0)
        size_row.set_title("Target maximum size (KB)")
        grp_size.add(size_row)
        size_out = Adw.ActionRow(title="Output")
        size_out.set_subtitle("—")
        grp_size.add(size_out)
        page_size.append(grp_size)
        stack.add_titled(page_size, "filesize", "File Size")
        stack.get_page(page_size).set_icon_name("file-size-symbolic")

        stack.set_visible_child_name("dimensions")
        box.append(stack)

        # --- crop settings -------------------------------------------------- #
        crop_group = Adw.PreferencesGroup()
        crop_expander = Adw.ExpanderRow(title="Crop", subtitle="Off")
        crop_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        crop_switch.set_sensitive(first_size is not None)
        crop_expander.add_suffix(crop_switch)
        crop_group.add(crop_expander)

        def crop_subtitle():
            if not crop_state["on"]:
                return "Off"
            if crop_state["crop"] is None:
                return "Drag on the preview to pick a region"
            x, y, cw, ch = crop_box(first_size, crop_state["crop"])
            return f"{cw} × {ch} px at {x}, {y}"

        def update_crop_controls():
            enabled = crop_state["on"]
            preset_row.set_sensitive(enabled)
            orient_row.set_sensitive(enabled)
            portrait.set_sensitive(enabled and crop_state["preset"] not in ("free", "1:1"))
            crop_expander.set_subtitle(crop_subtitle())

        def on_preset_selected(button, name):
            if not button.get_active():
                if crop_state["preset"] == name:
                    button.set_active(True)   # presets behave as a radio group
                return
            crop_state["preset"] = name
            for other_name, other in preset_buttons.items():
                if other_name != name and other.get_active():
                    other.set_active(False)
            reapply_aspect()

        def on_portrait_toggled(button):
            crop_state["portrait"] = button.get_active()
            reapply_aspect()

        def ratio_icon(aspect, dashed):
            def draw(area, cr, width, height):
                colour = area.get_color()
                cr.set_source_rgba(colour.red, colour.green, colour.blue, colour.alpha)
                pad = 2.0
                avail_w = max(1.0, width - 2 * pad)
                avail_h = max(1.0, height - 2 * pad)
                if aspect is None:
                    w, h = avail_w, avail_h
                else:
                    w = min(avail_w, avail_h * aspect)
                    h = w / aspect
                x, y = (width - w) / 2, (height - h) / 2
                cr.set_line_width(1.5)
                if dashed:
                    cr.set_dash([2.0, 2.0])
                cr.rectangle(x, y, w, h)
                cr.stroke()
            return draw

        preset_row = Adw.PreferencesRow()
        preset_row.set_sensitive(False)
        preset_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        preset_body.set_margin_top(6)
        preset_body.set_margin_bottom(6)
        preset_caption = Gtk.Label(label="Ratio", xalign=0.0)
        preset_caption.add_css_class("dim-label")
        preset_caption.add_css_class("caption")
        preset_body.append(preset_caption)
        preset_flow = Gtk.FlowBox()
        preset_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        preset_flow.set_homogeneous(True)
        preset_flow.set_min_children_per_line(5)
        preset_flow.set_max_children_per_line(5)
        preset_flow.set_column_spacing(4)
        preset_flow.set_row_spacing(4)
        preset_body.append(preset_flow)
        preset_row.set_child(preset_body)
        preset_buttons = {}
        original_aspect = ((first_size[0] / first_size[1]) if first_size else 4 / 3)
        for name, aspect in CROP_PRESETS.items():
            label = {"free": "Free", "original": "Original"}.get(name, name)
            if name == "original":
                aspect = original_aspect
            dashed = name == "free"
            button = Gtk.ToggleButton()
            button.set_tooltip_text(label)
            button.update_property([Gtk.AccessibleProperty.LABEL], [label])
            button.set_active(name == "free")
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            glyph = Gtk.DrawingArea()
            glyph.set_size_request(26, 18)
            glyph.set_halign(Gtk.Align.CENTER)
            glyph.set_draw_func(ratio_icon(aspect, dashed))
            text = Gtk.Label(label=label)
            text.add_css_class("caption")
            content.append(glyph)
            content.append(text)
            button.set_child(content)
            button.connect("toggled", on_preset_selected, name)
            preset_flow.append(button)
            preset_buttons[name] = button
        crop_expander.add_row(preset_row)

        orient_row = Adw.ActionRow(title="Orientation")
        portrait = Gtk.ToggleButton(label="Portrait")
        portrait.set_valign(Gtk.Align.CENTER)
        portrait.set_tooltip_text("Swap the crop ratio to portrait")
        portrait.connect("toggled", on_portrait_toggled)
        orient_row.add_suffix(portrait)
        crop_expander.add_row(orient_row)
        box.append(crop_group)

        crop_switch.connect("notify::active",
                            lambda row, _p: set_crop_enabled(row.get_active()))

        update_crop_controls()

        # --- output ------------------------------------------------------- #
        out_group = Adw.PreferencesGroup(title="Output")
        overwrite_row = Adw.ActionRow(title="Overwrite original files")
        overwrite_row.set_activatable(True)
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        overwrite_row.add_suffix(switch)
        out_group.add(overwrite_row)

        suffix_row = Adw.EntryRow(title="New-file suffix")
        suffix_row.set_text("_resized")
        out_group.add(suffix_row)
        box.append(out_group)

        # --- buttons ------------------------------------------------------ #
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        resize = Gtk.Button(label="Resize")
        resize.add_css_class("suggested-action")
        resize.add_css_class("pill")
        cancel.add_css_class("pill")
        reset = Gtk.Button(label="Reset")
        reset.add_css_class("pill")
        buttons.append(cancel)
        buttons.append(reset)
        buttons.append(resize)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(box)
        scroller.set_vexpand(True)

        buttons.set_margin_start(12)
        buttons.set_margin_end(12)
        buttons.set_margin_top(6)
        buttons.set_margin_bottom(12)

        lower = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        lower.append(switcher)
        lower.append(scroller)

        # A paned split gives the preview a share of the height and lets the
        # settings keep the rest; the preview follows window resizes.
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.set_start_child(preview)
        paned.set_end_child(lower)
        paned.set_position(280)
        paned.set_resize_start_child(True)
        paned.set_shrink_start_child(True)
        paned.set_shrink_end_child(True)
        paned.set_vexpand(True)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.append(paned)
        root.append(buttons)
        toolbar.set_content(root)
        win.set_content(toolbar)

        def update_sensitive(*_a):
            resize.set_sensitive(switch.get_active()
                                 or bool(suffix_row.get_text().strip()))

        def on_switch(*_a):
            suffix_row.set_visible(not switch.get_active())
            update_sensitive()

        switch.connect("notify::active", on_switch)
        suffix_row.connect("notify::text", update_sensitive)
        overwrite_row.connect("activated",
                              lambda *_: switch.set_active(not switch.get_active()))
        update_sensitive()

        # --- live output estimate ------------------------------------------ #
        outputs = {"percent": pct_out, "dimensions": dim_out, "filesize": size_out}
        spinners = {}
        for name, row in outputs.items():
            spinner = Adw.Spinner()
            spinner.set_valign(Gtk.Align.CENTER)
            spinner.set_visible(False)
            row.add_suffix(spinner)
            spinners[name] = spinner

        def px_line(size):
            return f"{size[0]} × {size[1]} px"

        def bytes_line(nbytes):
            line = f"≈ {nbytes / 1024:,.0f} KB"
            if nbytes >= 1048576:
                line += f" ({nbytes / 1048576:.2f} MB)"
            return line

        def pct_line(size):
            if not first_size:
                return ""
            base = first_size
            if crop_state["crop"]:
                _x, _y, cw, ch = crop_box(first_size, crop_state["crop"])
                base = (cw, ch)
            area = (size[0] * size[1]) / (base[0] * base[1])
            return f"{100.0 * math.sqrt(area):.0f} %"

        def params_for(mode):
            if mode == "percent":
                params = {"percent": float(pct_adj.get_value())}
            elif mode == "dimensions":
                params = {"width": int(w_adj.get_value()),
                          "height": int(h_adj.get_value()),
                          "lock": chain.get_active()}
            else:
                params = {"max_bytes": int(size_adj.get_value()) * 1024}
            if crop_state["crop"]:
                params["crop"] = dict(crop_state["crop"])
            return params

        state = {"token": 0, "pending": 0}

        def show_status(text, busy=False):
            active = stack.get_visible_child_name()
            for name, row in outputs.items():
                row.set_subtitle(text if name == active else "—")
                spinners[name].set_visible(busy and name == active)

        def stop_spinner(mode):
            spinners[mode].set_visible(False)

        def apply_estimate(token, mode, results, skipped, error):
            if token != state["token"]:
                return False
            row = outputs[mode]
            stop_spinner(mode)
            if not results:
                row.set_subtitle(f"estimate failed: {error}")
                return False
            if len(results) == 1:
                result = results[0]
                size = result["size"]
                if mode == "percent":
                    parts = [px_line(size), bytes_line(result["bytes"])]
                elif mode == "dimensions":
                    parts = [pct_line(size), bytes_line(result["bytes"])]
                else:
                    parts = [px_line(size), pct_line(size)]
                if result.get("note"):
                    parts.append(result["note"])
            else:
                # One subtitle has to describe every selected file.
                total_bytes = sum(result["bytes"] for result in results)
                sizes = {tuple(result["size"]) for result in results}
                if mode == "percent":
                    parts = [f"{pct_adj.get_value():.0f} %"]
                elif len(sizes) == 1:
                    parts = [px_line(results[0]["size"])]
                elif mode == "dimensions":
                    parts = ["sizes vary"]
                else:
                    parts = []
                parts.append(f"total {bytes_line(total_bytes)}")
                parts.append(f"{len(results)} images")
            if skipped:
                parts.append(f"{skipped} skipped")
            row.set_subtitle(" · ".join(part for part in parts if part))
            return False

        def run_estimate(token, mode, params):
            results = []
            skipped = 0
            error = None
            for path in paths:
                try:
                    image = load_image(path)
                    results.append(estimate(image[0], image[1], image[2],
                                            image[3], mode, params))
                except Exception as exc:
                    skipped += 1
                    error = f"{type(exc).__name__}: {exc}"
            GLib.idle_add(apply_estimate, token, mode, results, skipped, error)

        def queue_estimate(*_a):
            state["token"] += 1
            token = state["token"]
            if state["pending"]:
                GLib.source_remove(state["pending"])
                state["pending"] = 0
            if src_image is None:
                show_status("—")
                return

            def fire():
                # Read the page here, not at signal time: AdwViewStack notifies
                # before get_visible_child_name() reports the new page.
                state["pending"] = 0
                mode = stack.get_visible_child_name()
                params = params_for(mode)
                show_status("Calculating…", busy=True)
                threading.Thread(target=run_estimate,
                                 args=(token, mode, params), daemon=True).start()
                return False

            state["pending"] = GLib.timeout_add(220, fire)

        stack.connect("notify::visible-child", queue_estimate)
        for adj in (pct_adj, w_adj, h_adj, size_adj):
            adj.connect("value-changed", queue_estimate)
        chain.connect("toggled", queue_estimate)

        def finish(_results):
            # No summary dialog: the window just goes away when the work is done.
            win.close()

        def worker(jobs):
            results = [process_file(src, dst, mode, params)
                       for src, dst, mode, params in jobs]
            GLib.idle_add(finish, results)

        def on_reset(*_a):
            set_crop_enabled(False)
            crop_state["preset"] = "free"
            for name, button in preset_buttons.items():
                button.set_active(name == "free")
            crop_state["portrait"] = False
            portrait.set_active(False)
            crop_state["aspect"] = None
            crop_state["dims"] = first_size
            pct_adj.set_value(50)
            syncing["active"] = True
            w_adj.set_value(w0)
            h_adj.set_value(h0)
            syncing["active"] = False
            chain.set_active(lock)
            size_adj.set_value(500)
            switch.set_active(False)
            suffix_row.set_text("_resized")
            stack.set_visible_child_name("dimensions")
            update_crop_controls()
            queue_estimate()

        def on_resize(_btn):
            mode = stack.get_visible_child_name()
            params = params_for(mode)

            overwrite = switch.get_active()
            suffix = suffix_row.get_text()
            jobs = []
            for src in paths:
                if overwrite:
                    dst = src
                else:
                    stem, ext = Path(src).stem, Path(src).suffix
                    dst = str(Path(src).parent / f"{stem}{suffix}{ext}")
                jobs.append((src, dst, mode, params))

            resize.set_sensitive(False)
            cancel.set_sensitive(False)
            reset.set_sensitive(False)
            resize.set_label("Resizing…")
            threading.Thread(target=worker, args=(jobs,), daemon=True).start()

        cancel.connect("clicked", lambda *_: win.close())
        reset.connect("clicked", on_reset)
        resize.connect("clicked", on_resize)
        queue_estimate()
        win.present()

    app.connect("activate", on_activate)
    app.run([])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
