#!/usr/bin/python3
"""Right-click image resizer for GNOME Files (Nautilus).

GUI: GTK4 + libadwaita. The Pillow engine lives in `resize_engine`, which
imports no GTK. Run with image paths as arguments:

    image_resizer.py photo.jpg [more.png ...]

Importing this module has no side effects beyond defining the dialog.
"""

from __future__ import annotations

import math
import random
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from resize_engine import (
    CROP_PRESETS,
    EXIF_TRANSFORMS,
    _,
    crop_box,
    crop_drag_rect,
    crop_fit_rect,
    crop_move_rect,
    crop_refit_rect,
    crop_resize_rect,
    estimate_files,
    exif_orientation,
    header_size,
    process_file,
)

APP_ID = "dev.izzy.image-resizer"
APP_NAME = "Nautilus Image Resizer"
APP_VERSION = "1.0.0"
APP_REPO = "https://github.com/yzraeu/nautilus-image-resizer"
APP_AUTHOR = "yzraeu"


# --------------------------------------------------------------------------- #
# Dialog
# --------------------------------------------------------------------------- #

def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


# --------------------------------------------------------------------------- #
# Scatter layout
# --------------------------------------------------------------------------- #

# The approved "big spread", in layout pixels. `spread` scales the scatter as a
# whole: SPREAD is the approved look, 0 is a neat pile.
SPREAD = 100.0
CARD_WIDTH = (270.0, 320.0)
CARD_ANGLE = 9.0
CARD_DX = 95.0
CARD_DY = 55.0
CARD_BORDER = 8.0
CARD_RADIUS = 4.0
PHOTO_RADIUS = 3.0
FRONT_SCALE = 1.03

# Entrance: each card falls onto the table once, from above.
FALL_DISTANCE = 170.0
FALL_ANGLE = 6.0
FALL_SCALE = 0.05
FALL_DURATION = 520.0
FALL_DELAY = 90.0
FALL_STEP = 70.0


@dataclass(frozen=True)
class ScatterCard:
    """One card slot in the pile, in layout pixels."""

    width: float
    angle: float
    dx: float
    dy: float


def scatter_layout(seed: int, count: int,
                   spread: float = SPREAD) -> tuple[ScatterCard, ...]:
    """The pile: same seed and count in, same cards out, every time.

    Card 0 is the front. It never rotates or moves: the crop overlay maps to
    its rect through `image_geometry()`, which assumes an axis-aligned
    rectangle. Backs take the full spread so the pile reads as thrown down.
    """
    ratio = spread / SPREAD
    rng = random.Random(seed)
    cards = []
    for index in range(count):
        # Always roll every value, so the front card's fixed transform does
        # not shift the rest of the pile between seeds.
        width = rng.uniform(*CARD_WIDTH)
        angle = rng.uniform(-CARD_ANGLE, CARD_ANGLE) * ratio
        dx = rng.uniform(-CARD_DX, CARD_DX) * ratio
        dy = rng.uniform(-CARD_DY, CARD_DY) * ratio
        if index == 0:
            angle, dx, dy = 0.0, 0.0, 0.0
        cards.append(ScatterCard(width, angle, dx, dy))
    return tuple(cards)


def card_size(card: ScatterCard, aspect: float,
              scale: float = 1.0) -> tuple[float, float]:
    """Outer card size for a photo of `aspect`, white border included."""
    width = card.width * scale
    border = CARD_BORDER * scale
    photo_width = max(1.0, width - 2 * border)
    return width, photo_width / aspect + 2 * border


def scatter_bounds(cards: tuple[ScatterCard, ...], aspects: list[float],
                   margin: float = 0.0) -> tuple[float, float, float, float]:
    """Union of the rotated cards, as (x, y, width, height).

    Cards stick out past the old neat-stack box, so the deck measures this
    instead of card + border * backs.
    """
    if not cards:
        return (0.0, 0.0, 0.0, 0.0)
    left = top = math.inf
    right = bottom = -math.inf
    for index, card in enumerate(cards):
        width, height = card_size(card, aspects[index],
                                  FRONT_SCALE if index == 0 else 1.0)
        radians = math.radians(card.angle)
        cos, sin = abs(math.cos(radians)), abs(math.sin(radians))
        span_w = width * cos + height * sin
        span_h = width * sin + height * cos
        left = min(left, card.dx - span_w / 2)
        right = max(right, card.dx + span_w / 2)
        top = min(top, card.dy - span_h / 2)
        bottom = max(bottom, card.dy + span_h / 2)
    return (left - margin, top - margin,
            right - left + 2 * margin, bottom - top + 2 * margin)


def ease_out(t: float) -> float:
    """`cubic-bezier(.2, .8, .25, 1)`: quick start, no overshoot."""
    t = min(1.0, max(0.0, t))
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0

    def axis(s: float, p1: float, p2: float) -> float:
        u = 1.0 - s
        return 3.0 * u * u * s * p1 + 3.0 * u * s * s * p2 + s * s * s

    low, high = 0.0, 1.0
    for _ in range(24):
        mid = (low + high) / 2
        if axis(mid, 0.2, 0.25) < t:
            low = mid
        else:
            high = mid
    return axis((low + high) / 2, 0.8, 1.0)


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

    app = Adw.Application(application_id=APP_ID,
                          flags=Gio.ApplicationFlags.NON_UNIQUE)

    def on_activate(_app):
        n = len(paths)
        # Everything image-derived arrives after the window is on screen: the
        # first readable file's header, its byte size, the selection total and
        # the preview stills. On a slow mount that is what used to hold the
        # window back.
        source = {"path": paths[0], "size": None, "bytes": 0, "total": 0,
                  "probed": False}
        edited = {"dims": False}
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
        subtitle = (_("{} images selected").format(n) if n > 1
                    else _("Select the size and hit Resize"))
        title = Adw.WindowTitle(title=APP_NAME, subtitle=subtitle)
        header.set_title_widget(title)

        menu = Gio.Menu()
        menu.append(_("About {}").format(APP_NAME), "win.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic",
                                     menu_model=menu,
                                     tooltip_text=_("Main Menu"))
        menu_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Main Menu")])
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
                _("Dependencies"),
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

        def pixbuf_at(path, size):
            """Decode one image at preview size. Runs in a worker thread."""
            with Image.open(path) as im:
                if im.mode in ("RGB", "L"):
                    # Decode JPEG at DCT scale instead of shrinking afterwards.
                    im.draft(im.mode, (size, size))
                im.load()
                if exif_orientation(im) in EXIF_TRANSFORMS:
                    im = ImageOps.exif_transpose(im)
                if im.mode != "RGB":
                    alpha = "A" in im.getbands() or "transparency" in im.info
                    im = im.convert("RGBA" if alpha else "RGB")
                im.thumbnail((size, size), Image.Resampling.BILINEAR)
                channels = 4 if im.mode == "RGBA" else 3
                return GdkPixbuf.Pixbuf.new_from_bytes(
                    GLib.Bytes.new(im.tobytes()), GdkPixbuf.Colorspace.RGB,
                    channels == 4, 8, im.width, im.height, im.width * channels)

        def discover_source():
            """Everything the window waits on, off the main thread."""
            total = 0
            sizes = {}
            for path in paths:
                try:
                    nbytes = Path(path).stat().st_size
                except OSError:
                    nbytes = 0
                total += nbytes
                sizes[path] = nbytes
            # Headers are cheap, but only the cards that can be shown need one.
            infos = {}
            for path in paths[:5]:
                try:
                    size = header_size(path)
                except Exception:
                    size = None
                infos[path] = (size, sizes[path])
            # The front is the first readable file, as before. Its header is
            # all the caption and the crop need; the pixels come next.
            first_path = paths[0]
            for path in infos:
                if infos[path][0] is not None:
                    first_path = path
                    break
            first_size, first_bytes = infos[first_path]
            front = None
            if first_size is not None:
                try:
                    front = pixbuf_at(first_path, 900)
                except Exception:
                    front = None
            backs = []
            for path in paths[1:5]:
                if path == first_path:
                    continue
                try:
                    backs.append((path, pixbuf_at(path, 400)))
                except Exception:
                    continue
            return {"path": first_path, "size": first_size,
                    "bytes": first_bytes, "total": total, "front": front,
                    "backs": backs, "infos": infos}

        def to_texture(pixbuf):
            if pixbuf is None:
                return None
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                return Gdk.Texture.new_for_pixbuf(pixbuf)

        def apply_textures(front, backs):
            pic.set_images(
                [(source["path"], to_texture(front))]
                + [(path, to_texture(pixbuf)) for path, pixbuf in backs])

        class PreviewDeck(Gtk.Widget):
            """The selected photos as a scattered pile; the front card is active.

            The pile is `scatter_layout(seed, count)`: deterministic per seed,
            different on every window open. Back cards can be promoted to the
            front. The front card never rotates, because the crop overlay maps
            onto it with `image_geometry()`, which assumes an axis-aligned
            rectangle.
            """

            def __init__(self, label):
                super().__init__()
                self._entries = []      # [(path, texture)], front first
                self._source_size = None
                self._seed = time.time_ns()
                self._falling = False
                self._fell = False
                self._fall_started = None
                self._fall_elapsed = 0.0
                self.set_accessible_role(Gtk.AccessibleRole.IMG)
                self.update_property([Gtk.AccessibleProperty.LABEL], [label])

            def set_label(self, label):
                self.update_property([Gtk.AccessibleProperty.LABEL], [label])

            def front_path(self):
                return self._entries[0][0] if self._entries else None

            def set_images(self, entries):
                self._entries = list(entries)
                self.queue_resize()
                self.queue_draw()
                self._start_fall()

            def set_front_texture(self, texture):
                if self._entries:
                    self._entries[0] = (self._entries[0][0], texture)
                    self.queue_resize()
                    self.queue_draw()

            def set_source_size(self, size):
                self._source_size = size
                self.queue_resize()
                self.queue_draw()

            # -- geometry --------------------------------------------------- #

            def _aspects(self):
                aspects = []
                for index, (_path, texture) in enumerate(self._entries):
                    if index == 0 and self._source_size:
                        width, height = self._source_size
                    elif texture is not None:
                        width, height = texture.get_width(), texture.get_height()
                    else:
                        width, height = 4.0, 3.0
                    aspects.append(width / height)
                return aspects

            def _geometry(self):
                """Placed cards and the fit scale, in widget coordinates.

                Each placed card is (cx, cy, width, height, angle, scale):
                width and height are the card's own size, `scale` maps them
                into the widget. The pile is centred; cards that stick out
                past the old neat-stack box are fitted in, never clipped.
                """
                width, height = self.get_width(), self.get_height()
                if not self._entries or width <= 0 or height <= 0:
                    return (), 1.0
                aspects = self._aspects()
                cards = scatter_layout(self._seed, len(self._entries))
                bounds = scatter_bounds(cards, aspects, margin=2.0)
                fit = min(1.0, width / bounds[2], height / bounds[3])
                origin_x = width / 2 - (bounds[0] + bounds[2] / 2) * fit
                origin_y = height / 2 - (bounds[1] + bounds[3] / 2) * fit
                placed = []
                for index, card in enumerate(cards):
                    card_w, card_h = card_size(card, aspects[index])
                    scale = fit * (FRONT_SCALE if index == 0 else 1.0)
                    placed.append((origin_x + card.dx * fit,
                                   origin_y + card.dy * fit,
                                   card_w, card_h, card.angle, scale))
                return placed, fit

            def do_measure(self, orientation, _for_size):
                if not self._entries:
                    if orientation == Gtk.Orientation.VERTICAL:
                        return 120, 280, -1, -1
                    return 0, 0, -1, -1
                aspects = self._aspects()
                bounds = scatter_bounds(
                    scatter_layout(self._seed, len(self._entries)), aspects,
                    margin=2.0)
                if orientation == Gtk.Orientation.VERTICAL:
                    return 120, math.ceil(bounds[3]), -1, -1
                return 0, math.ceil(bounds[2]), -1, -1

            def top_card(self):
                """Rect of the front card, scale 1.03 and border included."""
                placed = self._geometry()[0]
                if not placed:
                    return (0.0, 0.0, 0.0, 0.0)
                cx, cy, width, height, _angle, scale = placed[0]
                width, height = width * scale, height * scale
                return (cx - width / 2, cy - height / 2, width, height)

            def image_geometry(self):
                """Scale and offset mapping the source image onto its photo."""
                if self._source_size is None:
                    return None
                placed = self._geometry()[0]
                if not placed:
                    return None
                x, y, width, _height = self.top_card()
                border = CARD_BORDER * placed[0][5]
                photo_w = width - 2 * border
                if photo_w <= 0:
                    return None
                return (photo_w / self._source_size[0], x + border, y + border)

            def card_at(self, x, y):
                """Index of the topmost card under the point, front first."""
                placed = self._geometry()[0]
                for index, card in enumerate(placed):
                    cx, cy, width, height, angle, scale = card
                    dx, dy = x - cx, y - cy
                    radians = math.radians(-angle)
                    cos, sin = math.cos(radians), math.sin(radians)
                    lx = dx * cos - dy * sin
                    ly = dx * sin + dy * cos
                    if (abs(lx) <= width * scale / 2
                            and abs(ly) <= height * scale / 2):
                        return index
                return -1

            def promote(self, index):
                """Swap a back card with the front; return the new front path."""
                if not 0 < index < len(self._entries):
                    return None
                if self._entries[index][1] is None:
                    return None
                entries = list(self._entries)
                entries[0], entries[index] = entries[index], entries[0]
                self._entries = entries
                self.queue_resize()
                self.queue_draw()
                return entries[0][0]

            # -- entrance --------------------------------------------------- #

            def _start_fall(self):
                if self._fell:
                    return
                if not any(texture is not None
                           for _path, texture in self._entries):
                    return
                self._fell = True
                self._falling = True
                self._fall_started = None
                self.add_tick_callback(self._tick)

            def _tick(self, _widget, clock):
                now = clock.get_frame_time() / 1000.0        # ms
                if self._fall_started is None:
                    self._fall_started = now
                self._fall_elapsed = now - self._fall_started
                total = (FALL_DELAY + FALL_STEP * max(0, len(self._entries) - 1)
                         + FALL_DURATION)
                if self._fall_elapsed >= total:
                    self._falling = False
                    self.queue_draw()
                    return False
                self.queue_draw()
                return True

            def _settle(self, draw_index):
                """0 at the start of a card's fall, 1 when it has landed."""
                if not self._falling:
                    return 1.0
                delay = FALL_DELAY + FALL_STEP * draw_index
                return ease_out((self._fall_elapsed - delay) / FALL_DURATION)

            # -- drawing ---------------------------------------------------- #

            def _draw_card(self, snapshot, texture, cx, cy, width, height,
                           angle, scale, alpha, front):
                if texture is None or alpha <= 0.0:
                    return
                snapshot.save()
                snapshot.transform(
                    Gsk.Transform.new()
                    .translate(Graphene.Point().init(cx, cy))
                    .rotate(angle)
                    .scale(scale, scale))
                snapshot.push_opacity(alpha)
                shadow = Gsk.Shadow()
                shadow.color = Gdk.RGBA(0, 0, 0, 0.28)
                shadow.dx = 0.0
                shadow.dy = 2.0
                shadow.radius = 6.0
                snapshot.push_shadow([shadow])
                left, top = -width / 2, -height / 2
                card = Graphene.Rect().init(left, top, width, height)
                rounded = Gsk.RoundedRect()
                rounded.init_from_rect(card, CARD_RADIUS)
                snapshot.push_rounded_clip(rounded)
                snapshot.append_color(Gdk.RGBA(1, 1, 1, 1), card)
                photo_x, photo_y = left + CARD_BORDER, top + CARD_BORDER
                photo_w = width - 2 * CARD_BORDER
                photo_h = height - 2 * CARD_BORDER
                photo = Graphene.Rect().init(photo_x, photo_y, photo_w, photo_h)
                photo_rounded = Gsk.RoundedRect()
                photo_rounded.init_from_rect(photo, PHOTO_RADIUS)
                snapshot.push_rounded_clip(photo_rounded)
                # Crop to fill the print instead of squashing the photo.
                texture_w, texture_h = texture.get_width(), texture.get_height()
                cover = max(photo_w / texture_w, photo_h / texture_h)
                draw_w, draw_h = texture_w * cover, texture_h * cover
                snapshot.append_texture(
                    texture,
                    Graphene.Rect().init(photo_x + (photo_w - draw_w) / 2,
                                         photo_y + (photo_h - draw_h) / 2,
                                         draw_w, draw_h))
                snapshot.pop()          # photo clip
                snapshot.pop()          # card clip
                snapshot.pop()          # shadow
                if front:
                    # Accent ring just inside the white border.
                    ring = Gsk.RoundedRect()
                    ring.init_from_rect(
                        Graphene.Rect().init(left + 1.0, top + 1.0,
                                             width - 2.0, height - 2.0),
                        CARD_RADIUS + 1.0)
                    accent = (Adw.StyleManager.get_default()
                              .get_accent_color_rgba())
                    snapshot.append_border(ring, [2.0] * 4, [accent] * 4)
                snapshot.pop()          # opacity
                snapshot.restore()

            def do_snapshot(self, snapshot):
                placed, fit = self._geometry()
                if not placed:
                    return
                snapshot.push_clip(
                    Graphene.Rect().init(0, 0, self.get_width(),
                                         self.get_height()))
                count = len(placed)
                for index in range(count - 1, -1, -1):
                    cx, cy, width, height, angle, scale = placed[index]
                    # Back to front: the backmost card starts first.
                    settle = self._settle(count - 1 - index)
                    drop = FALL_DISTANCE * fit * (1.0 - settle)
                    self._draw_card(
                        snapshot, self._entries[index][1],
                        cx, cy - drop, width, height,
                        angle + FALL_ANGLE * (1.0 - settle),
                        scale * (1.0 + FALL_SCALE * (1.0 - settle)),
                        settle, index == 0)
                snapshot.pop()

        pic = PreviewDeck(_("Image preview"))

        overlay = Gtk.Overlay()
        overlay.set_child(pic)
        crop_area = Gtk.DrawingArea()
        crop_area.set_can_target(False)
        crop_area.set_focusable(False)
        crop_area.set_accessible_role(Gtk.AccessibleRole.GROUP)
        crop_area.update_property([Gtk.AccessibleProperty.LABEL],
                                  [_("Crop selection area")])
        crop_area.update_property(
            [Gtk.AccessibleProperty.DESCRIPTION],
            [_("Arrow keys move the crop region; Shift+arrow keys resize it")])
        overlay.add_overlay(crop_area)
        preview.append(overlay)

        crop_state = {"crop": None, "dims": None, "origin": None, "live": None,
                      "on": False, "aspect": None, "preset": "free",
                      "portrait": False}

        def display_geometry():
            if not source["size"]:
                return None
            return pic.image_geometry()

        def to_source(x, y):
            geometry = display_geometry()
            if geometry is None:
                return 0.0, 0.0
            scale, offset_x, offset_y = geometry
            return (min(source["size"][0], max(0.0, (x - offset_x) / scale)),
                    min(source["size"][1], max(0.0, (y - offset_y) / scale)))

        def draw_crop(_area, cr, width, height):
            geometry = display_geometry()
            if geometry is None or source["size"] is None:
                return
            if crop_state["live"] is not None:
                x, y, cw, ch = crop_state["live"]
            elif crop_state["crop"] is not None:
                x, y, cw, ch = crop_box(source["size"], crop_state["crop"])
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
            # Double stroke reads on light and dark photos in any theme; the
            # wider pass marks keyboard focus on the region.
            stroke = 3 if crop_area.has_focus() else 2
            cr.rectangle(rx, ry, rw, rh)
            cr.set_source_rgba(0, 0, 0, 0.9)
            cr.set_line_width(stroke + 1.5)
            cr.stroke()
            cr.rectangle(rx, ry, rw, rh)
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.set_line_width(stroke)
            cr.stroke()

        crop_area.set_draw_func(draw_crop)

        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_a: crop_area.queue_draw())
        focus.connect("leave", lambda *_a: crop_area.queue_draw())
        crop_area.add_controller(focus)

        def current_aspect():
            if source["size"] is None:
                return None
            name = crop_state["preset"]
            if name == "free":
                return None
            base = ((source["size"][0] / source["size"][1]) if name == "original"
                    else CROP_PRESETS[name])
            if base is None:
                return None
            return (1.0 / base) if crop_state["portrait"] else base

        announce = {"pending": 0}

        def announce_crop():
            # Coalesce auto-repeat: only the settled geometry is spoken.
            announce["pending"] = 0
            if crop_state["on"]:
                crop_area.announce(crop_label.get_text(),
                                   Gtk.AccessibleAnnouncementPriority.LOW)
            return False

        def commit_crop(rect):
            previous = crop_state.get("dims") or source["size"]
            if rect is None:
                crop_state["crop"] = None
                crop_state["dims"] = source["size"]
            else:
                x, y, cw, ch = rect
                width, height = source["size"]
                crop_state["crop"] = {"fx": x / width, "fy": y / height,
                                      "fw": cw / width, "fh": ch / height}
                crop_state["dims"] = (round(cw), round(ch))
            resize_fields(previous, crop_state["dims"])
            update_crop_label()
            if crop_state["on"]:
                if announce["pending"]:
                    GLib.source_remove(announce["pending"])
                announce["pending"] = GLib.timeout_add(450, announce_crop)
            update_crop_controls()
            crop_area.queue_draw()
            queue_estimate()

        def reapply_aspect():
            crop_state["aspect"] = current_aspect()
            update_crop_controls()
            if not crop_state["on"] or not source["size"] or crop_state["aspect"] is None:
                queue_estimate()
                return
            if crop_state["crop"] is not None:
                rect = crop_refit_rect(crop_box(source["size"], crop_state["crop"]),
                                       crop_state["aspect"])
            else:
                rect = crop_fit_rect(source["size"], crop_state["aspect"])
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
            crop_state["live"] = crop_drag_rect(x0, y0, x1, y1, source["size"],
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

        def crop_step(length):
            return max(1, round(length / 100.0))

        def nudge_crop(keyval, shift):
            size = source["size"]
            if size is None:
                return False
            if crop_state["crop"] is None:
                half = (round(size[0] / 4), round(size[1] / 4),
                        round(size[0] / 2), round(size[1] / 2))
                rect = (crop_refit_rect(half, crop_state["aspect"])
                        if crop_state["aspect"] else half)
                commit_crop(rect)
            x, y, cw, ch = crop_box(size, crop_state["crop"])
            step_x = crop_step(size[0]) * (10 if shift else 1)
            step_y = crop_step(size[1]) * (10 if shift else 1)
            dx = (step_x if keyval == Gdk.KEY_Right
                  else -step_x if keyval == Gdk.KEY_Left else 0)
            dy = (step_y if keyval == Gdk.KEY_Down
                  else -step_y if keyval == Gdk.KEY_Up else 0)
            if shift:
                rect = crop_resize_rect((x, y, cw, ch), dx, dy, size,
                                        crop_state["aspect"])
            else:
                rect = crop_move_rect((x, y, cw, ch), dx, dy, size)
            commit_crop(rect)
            return True

        def on_crop_key(_controller, keyval, _keycode, state):
            if keyval not in (Gdk.KEY_Left, Gdk.KEY_Right,
                              Gdk.KEY_Up, Gdk.KEY_Down):
                return False
            return nudge_crop(keyval, bool(state & Gdk.ModifierType.SHIFT_MASK))

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", on_crop_key)
        crop_area.add_controller(keys)

        def update_caption():
            size = source["size"]
            if n > 1:
                details = [_human_size(source["total"])] if source["probed"] else []
                details.append(_("{} images selected").format(n))
            else:
                if size is None:
                    details = [_("reading…") if not source["probed"] else _("unreadable")]
                else:
                    details = [f"{size[0]}×{size[1]}"]
                if source["bytes"]:
                    details.append(_human_size(source["bytes"]))
            name_label.set_text(Path(source["path"]).name)
            detail_label.set_text(" · ".join(details))

        caption = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        detail_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        detail_label.add_css_class("dim-label")
        caption.append(name_label)
        caption.append(detail_label)
        update_caption()
        preview.append(caption)

        crop_label = Gtk.Label(label="", wrap=True, xalign=0.0)
        crop_label.add_css_class("dim-label")
        crop_label.set_visible(False)
        preview.append(crop_label)

        def update_crop_label():
            crop = crop_state["crop"]
            if crop is None:
                crop_label.set_visible(crop_state["on"])
                crop_label.set_text(_("Drag on the preview to select the crop region"))
                return
            x, y, cw, ch = crop_box(source["size"], crop)
            crop_label.set_visible(True)
            crop_label.set_text(_("Crop {} × {} px at {}, {}").format(cw, ch, x, y))

        def set_crop_enabled(active):
            if crop_state["on"] == active and crop_switch.get_active() == active:
                return
            crop_state["on"] = active
            crop_switch.set_active(active)
            crop_area.set_can_target(active)
            crop_area.set_focusable(active)
            if active:
                crop_area.set_cursor(Gdk.Cursor.new_from_name("crosshair"))
                crop_state["aspect"] = current_aspect()
                if crop_state["aspect"] is not None and source["size"]:
                    commit_crop(crop_fit_rect(source["size"], crop_state["aspect"]))
                else:
                    commit_crop(crop_state["crop"])
                if source["size"] is not None:
                    crop_area.grab_focus()
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
        pct_row.set_title(_("Scale (%)"))
        grp_pct.add(pct_row)
        pct_out = Adw.ActionRow(title=_("Output"))
        pct_out.set_subtitle("—")
        grp_pct.add(pct_out)
        page_pct.append(grp_pct)
        stack.add_titled(page_pct, "percent", _("Percentage"))
        stack.get_page(page_pct).set_icon_name("percent-symbolic")

        # Dimensions
        page_dim = new_page()
        grp_dim = Adw.PreferencesGroup()
        w0, h0 = 1920, 1080      # replaced by the real size once the header lands
        lock = True

        w_adj = Gtk.Adjustment(value=w0, lower=1, upper=30000, step_increment=1)
        h_adj = Gtk.Adjustment(value=h0, lower=1, upper=30000, step_increment=1)
        w_row = Adw.SpinRow(adjustment=w_adj, climb_rate=1, digits=0)
        w_row.set_title(_("Width (px)"))
        h_row = Adw.SpinRow(adjustment=h_adj, climb_rate=1, digits=0)
        h_row.set_title(_("Height (px)"))

        chain = Gtk.ToggleButton()
        chain.set_valign(Gtk.Align.CENTER)
        chain.set_active(lock)
        chain.set_sensitive(False)
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
            if source["size"] is None:
                return None
            if crop_state["crop"]:
                _x, _y, cw, ch = crop_box(source["size"], crop_state["crop"])
            else:
                cw, ch = source["size"]
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

        chain.update_property([Gtk.AccessibleProperty.LABEL],
                              [_("Lock aspect ratio")])
        chain.update_property([Gtk.AccessibleProperty.DESCRIPTION],
                              [_("Keeps the width-to-height ratio while resizing")])

        def set_chain_state(active: bool):
            chain_icon.set_from_icon_name(
                "changes-prevent-symbolic" if active else "changes-allow-symbolic")
            chain.set_tooltip_text(_("Aspect ratio locked") if active
                                   else _("Aspect ratio unlocked"))

        set_chain_state(lock)

        def on_chain_toggled(btn):
            set_chain_state(btn.get_active())

        chain.connect("toggled", on_chain_toggled)

        def on_w_changed(row, _pspec):
            if syncing["active"]:
                return
            edited["dims"] = True      # the user owns the fields now
            ratio = current_ratio()
            if not chain.get_active() or ratio is None:
                return
            syncing["active"] = True
            h_adj.set_value(min(30000, max(1, round(row.get_value() / ratio))))
            syncing["active"] = False

        def on_h_changed(row, _pspec):
            if syncing["active"]:
                return
            edited["dims"] = True      # the user owns the fields now
            ratio = current_ratio()
            if not chain.get_active() or ratio is None:
                return
            syncing["active"] = True
            w_adj.set_value(min(30000, max(1, round(row.get_value() * ratio))))
            syncing["active"] = False

        w_row.connect("notify::value", on_w_changed)
        h_row.connect("notify::value", on_h_changed)

        grp_dim.add(w_row)
        grp_dim.add(h_row)
        dim_out = Adw.ActionRow(title=_("Output"))
        dim_out.set_subtitle("—")
        grp_dim.add(dim_out)
        page_dim.append(grp_dim)
        stack.add_titled(page_dim, "dimensions", _("Dimensions"))
        stack.get_page(page_dim).set_icon_name("zoom-fit-best-symbolic")

        # File size
        page_size = new_page()
        grp_size = Adw.PreferencesGroup()
        size_adj = Gtk.Adjustment(value=500, lower=1, upper=102400,
                                  step_increment=1)
        size_row = Adw.SpinRow(adjustment=size_adj, climb_rate=1, digits=0)
        size_row.set_title(_("Target maximum size (KB)"))
        grp_size.add(size_row)
        size_out = Adw.ActionRow(title=_("Output"))
        size_out.set_subtitle("—")
        grp_size.add(size_out)
        page_size.append(grp_size)
        stack.add_titled(page_size, "filesize", _("File Size"))
        stack.get_page(page_size).set_icon_name("file-size-symbolic")

        stack.set_visible_child_name("dimensions")
        box.append(stack)

        # --- crop settings -------------------------------------------------- #
        crop_group = Adw.PreferencesGroup()
        crop_expander = Adw.ExpanderRow(title=_("Crop"), subtitle=_("Off"))
        crop_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        crop_switch.set_sensitive(False)
        crop_switch.update_property([Gtk.AccessibleProperty.LABEL],
                                    [_("Enable crop")])
        crop_expander.add_suffix(crop_switch)
        crop_group.add(crop_expander)

        def crop_subtitle():
            if not crop_state["on"]:
                return _("Off")
            if crop_state["crop"] is None:
                return _("Drag on the preview to pick a region")
            x, y, cw, ch = crop_box(source["size"], crop_state["crop"])
            return _("{} × {} px at {}, {}").format(cw, ch, x, y)

        def update_crop_controls():
            enabled = crop_state["on"]
            preset_row.set_sensitive(enabled)
            orient_row.set_sensitive(enabled)
            portrait.set_sensitive(enabled and crop_state["preset"] not in ("free", "1:1"))
            crop_expander.set_subtitle(crop_subtitle())

        def on_preset_selected(button, name):
            if not button.get_active():
                return
            crop_state["preset"] = name
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
        preset_caption = Gtk.Label(label=_("Ratio"), xalign=0.0)
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
        original_aspect = 4 / 3          # redrawn once the real size is known
        original_glyph = None
        for name, aspect in CROP_PRESETS.items():
            label = {"free": _("Free"), "original": _("Original")}.get(name, name)
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
            if name == "original":
                original_glyph = glyph
            text = Gtk.Label(label=label)
            text.add_css_class("caption")
            content.append(glyph)
            content.append(text)
            button.set_child(content)
            button.set_size_request(64, 48)
            button.connect("toggled", on_preset_selected, name)
            if preset_buttons:
                button.set_group(preset_buttons["free"])
            preset_flow.append(button)
            preset_buttons[name] = button
        crop_expander.add_row(preset_row)

        orient_row = Adw.ActionRow(title=_("Orientation"))
        portrait = Gtk.ToggleButton(label=_("Portrait"))
        portrait.set_valign(Gtk.Align.CENTER)
        portrait.set_tooltip_text(_("Swap the crop ratio to portrait"))
        portrait.connect("toggled", on_portrait_toggled)
        orient_row.add_suffix(portrait)
        crop_expander.add_row(orient_row)
        box.append(crop_group)

        crop_switch.connect("notify::active",
                            lambda row, _p: set_crop_enabled(row.get_active()))

        update_crop_controls()

        # --- output ------------------------------------------------------- #
        out_group = Adw.PreferencesGroup(title=_("Output"))
        overwrite_row = Adw.SwitchRow(title=_("Overwrite original files"))
        out_group.add(overwrite_row)

        suffix_row = Adw.EntryRow(title=_("New-file suffix"))
        suffix_row.set_text("_resized")
        out_group.add(suffix_row)
        box.append(out_group)

        # --- buttons ------------------------------------------------------ #
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label=_("Cancel"))
        resize = Gtk.Button(label=_("Resize"))
        resize.add_css_class("suggested-action")
        resize.add_css_class("pill")
        cancel.add_css_class("pill")
        reset = Gtk.Button(label=_("Reset"))
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
        win.set_default_widget(resize)

        close_action = Gio.SimpleAction.new("close", None)
        close_action.connect("activate", lambda *_: win.close())
        win.add_action(close_action)

        def on_window_key(_controller, keyval, _keycode, _state):
            if keyval == Gdk.KEY_Escape:
                win.close()
                return True
            return False

        window_keys = Gtk.EventControllerKey()
        window_keys.connect("key-pressed", on_window_key)
        win.add_controller(window_keys)

        def update_sensitive(*_a):
            ready = source["probed"] and (overwrite_row.get_active()
                                          or bool(suffix_row.get_text().strip()))
            resize.set_sensitive(ready)

        def on_switch(*_a):
            suffix_row.set_visible(not overwrite_row.get_active())
            update_sensitive()

        overwrite_row.connect("notify::active", on_switch)
        suffix_row.connect("notify::text", update_sensitive)
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
            return _("{} × {} px").format(size[0], size[1])

        def bytes_line(nbytes):
            line = _("≈ {:,.0f} KB").format(nbytes / 1024)
            if nbytes >= 1048576:
                line += _(" ({:.2f} MB)").format(nbytes / 1048576)
            return line

        def pct_line(size):
            if not source["size"]:
                return ""
            base = source["size"]
            if crop_state["crop"]:
                _x, _y, cw, ch = crop_box(source["size"], crop_state["crop"])
                base = (cw, ch)
            area = (size[0] * size[1]) / (base[0] * base[1])
            return _("{:.0f} %").format(100.0 * math.sqrt(area))

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
            if busy:
                outputs[active].announce(
                    text, Gtk.AccessibleAnnouncementPriority.LOW)

        def stop_spinner(mode):
            spinners[mode].set_visible(False)

        def apply_estimate(token, mode, results, skipped, error):
            if token != state["token"]:
                return False
            row = outputs[mode]
            stop_spinner(mode)
            if not results:
                text = _("estimate failed: {}").format(error)
                row.set_subtitle(text)
                row.announce(text, Gtk.AccessibleAnnouncementPriority.LOW)
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
                    parts = [_("{:.0f} %").format(pct_adj.get_value())]
                elif len(sizes) == 1:
                    parts = [px_line(results[0]["size"])]
                elif mode == "dimensions":
                    parts = [_("sizes vary")]
                else:
                    parts = []
                parts.append(_("total {}").format(bytes_line(total_bytes)))
                parts.append(_("{} images").format(len(results)))
            if skipped:
                parts.append(_("{} skipped").format(skipped))
            text = " · ".join(part for part in parts if part)
            row.set_subtitle(text)
            row.announce(text, Gtk.AccessibleAnnouncementPriority.LOW)
            return False

        def run_estimate(token, mode, params):
            results, skipped, error = estimate_files(
                paths, mode, params,
                should_abort=lambda: token != state["token"])
            GLib.idle_add(apply_estimate, token, mode, results, skipped, error)

        def queue_estimate(*_a):
            state["token"] += 1
            token = state["token"]
            if state["pending"]:
                GLib.source_remove(state["pending"])
                state["pending"] = 0
            if source["size"] is None:
                show_status("—")
                return

            def fire():
                # Read the page here, not at signal time: AdwViewStack notifies
                # before get_visible_child_name() reports the new page.
                state["pending"] = 0
                mode = stack.get_visible_child_name()
                params = params_for(mode)
                show_status(_("Calculating…"), busy=True)
                threading.Thread(target=run_estimate,
                                 args=(token, mode, params), daemon=True).start()
                return False

            state["pending"] = GLib.timeout_add(220, fire)

        stack.connect("notify::visible-child", queue_estimate)
        for adj in (pct_adj, w_adj, h_adj, size_adj):
            adj.connect("value-changed", queue_estimate)
        chain.connect("toggled", queue_estimate)

        def finish(results):
            failed = [result for result in results if not result.ok]
            if not failed:
                # The window just goes away when the work is done.
                win.close()
                return
            cancel.set_sensitive(True)
            reset.set_sensitive(True)
            resize.set_label(_("Resize"))
            update_sensitive()
            dialog = Gtk.AlertDialog(
                message=_("{} of {} images could not be resized").format(
                    len(failed), len(results)),
                detail="; ".join(_("{} — {}").format(Path(result.src).name,
                                                    result.note)
                                 for result in failed),
                modal=True)
            dialog.show(win)

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
            crop_state["dims"] = source["size"]
            pct_adj.set_value(50)
            dims = source["size"] or (w0, h0)
            syncing["active"] = True
            w_adj.set_value(dims[0])
            h_adj.set_value(dims[1])
            syncing["active"] = False
            chain.set_active(lock)
            size_adj.set_value(500)
            overwrite_row.set_active(False)
            suffix_row.set_text("_resized")
            stack.set_visible_child_name("dimensions")
            update_crop_controls()
            queue_estimate()

        def on_resize(_btn):
            mode = stack.get_visible_child_name()
            params = params_for(mode)

            overwrite = overwrite_row.get_active()
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
            resize.set_label(_("Resizing…"))
            threading.Thread(target=worker, args=(jobs,), daemon=True).start()

        files = {}
        fitted = {"done": False}

        def preview_label(path):
            if source["size"] is None:
                return _("Image preview (unavailable)")
            label = _("Image preview: {}").format(Path(path).name)
            if n > 1:
                label += _(", plus {} more images").format(n - 1)
            return label

        def apply_source(data):
            """Fill in everything that needed a header or a decode."""
            source.update(path=data["path"], size=data["size"],
                          bytes=data["bytes"], total=data["total"],
                          probed=True)
            files.update(data["infos"])
            size = data["size"]
            if size is not None:
                if not edited["dims"]:
                    # Fill the prefill the user has not taken over yet; the
                    # aspect lock would otherwise rewrite one of the two.
                    syncing["active"] = True
                    w_adj.set_value(size[0])
                    h_adj.set_value(size[1])
                    syncing["active"] = False
                chain.set_sensitive(True)
                crop_switch.set_sensitive(True)
                crop_state["dims"] = size
                pic.set_source_size(size)
                original_glyph.set_draw_func(ratio_icon(size[0] / size[1], False))
            else:
                chain.set_active(False)   # toggled() flips the icon too
            update_caption()
            if size is not None and data["front"] is not None:
                pic.set_label(preview_label(data["path"]))
            else:
                pic.set_label(_("Image preview (unavailable)"))
            update_crop_controls()
            apply_textures(data["front"], data["backs"])
            update_sensitive()
            queue_estimate()
            GLib.idle_add(fit_preview)
            return False

        def show_front(path, size, nbytes):
            """A tapped card is the front: caption and crop preview follow.

            The resize settings and the output estimate do not. They describe
            the whole selection, so switching which photo is previewed leaves
            them alone and nothing needs recalculating.
            """
            source.update(path=path, size=size, bytes=nbytes)
            if size is not None:
                pic.set_source_size(size)
                chain.set_sensitive(True)
                crop_switch.set_sensitive(True)
            else:
                # A header that would not read cannot be cropped.
                chain.set_sensitive(False)
                crop_switch.set_sensitive(False)
                if crop_state["on"]:
                    set_crop_enabled(False)
            update_caption()
            pic.set_label(preview_label(path))
            update_crop_label()
            update_crop_controls()
            crop_area.queue_draw()

        def apply_front_texture(path, pixbuf):
            if pic.front_path() == path:
                pic.set_front_texture(to_texture(pixbuf))
            return False

        def upgrade_front(path):
            """Re-decode a promoted card at the front's 900 px."""
            try:
                pixbuf = pixbuf_at(path, 900)
            except Exception:
                return
            GLib.idle_add(apply_front_texture, path, pixbuf)

        def on_deck_pressed(gesture, _n_press, x, y):
            index = pic.card_at(x, y)
            if index <= 0:
                return
            # Claim the press so a tap on a back card is not also a crop drag.
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            path = pic.promote(index)
            if path is None or path == source["path"]:
                return
            size, nbytes = files.get(path, (None, 0))
            show_front(path, size, nbytes)
            threading.Thread(target=upgrade_front, args=(path,),
                             daemon=True).start()

        def fit_preview():
            """Give the pile the height its cards ask for, once per open."""
            if fitted["done"]:
                return False
            height = paned.get_height()
            if height <= 0:
                return False
            fitted["done"] = True
            natural = preview.measure(Gtk.Orientation.VERTICAL, -1)[1]
            paned.set_position(max(280, min(natural, int(height * 0.55))))
            return False

        deck_click = Gtk.GestureClick()
        deck_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        deck_click.connect("pressed", on_deck_pressed)
        overlay.add_controller(deck_click)

        def load_source():
            GLib.idle_add(apply_source, discover_source())

        cancel.connect("clicked", lambda *_: win.close())
        reset.connect("clicked", on_reset)
        resize.connect("clicked", on_resize)
        queue_estimate()
        threading.Thread(target=load_source, daemon=True).start()
        win.present()

    app.connect("activate", on_activate)
    app.set_accels_for_action("win.close", ["<Control>w"])
    app.run([])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
