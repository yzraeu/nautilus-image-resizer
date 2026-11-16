#!/usr/bin/python3
"""Right-click image resizer for GNOME Files (Nautilus).

GUI: GTK4 + libadwaita. The Pillow engine lives in `resize_engine`, which
imports no GTK. Run with image paths as arguments:

    image_resizer.py photo.jpg [more.png ...]

Importing this module has no side effects beyond defining the dialog.
"""

from __future__ import annotations

import math
import sys
import threading
import warnings
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
    oriented_size,
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
            for path in paths:
                try:
                    total += Path(path).stat().st_size
                except OSError:
                    continue
            first_path, first_size, first_bytes = paths[0], None, 0
            for path in paths:
                try:
                    with Image.open(path) as probe:
                        first_size = oriented_size(probe)
                except Exception:
                    continue
                first_path = path
                try:
                    first_bytes = Path(path).stat().st_size
                except OSError:
                    first_bytes = 0
                break
            front = None
            if first_size is not None:
                try:
                    front = pixbuf_at(first_path, 900)
                except Exception:
                    front = None
            backs = []
            for path in paths[1:5]:
                try:
                    backs.append(pixbuf_at(path, 400))
                except Exception:
                    continue
            return (first_path, first_size, first_bytes, total,
                    front, backs)

        def apply_textures(front, backs):
            def to_texture(pixbuf):
                if pixbuf is None:
                    return None
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    return Gdk.Texture.new_for_pixbuf(pixbuf)

            pic.set_textures(to_texture(front),
                             [to_texture(pixbuf) for pixbuf in backs])
            return False

        class PreviewDeck(Gtk.Widget):
            """The first image on top; the rest peek out as cards behind it."""

            PAD = 10  # px each back card is offset from the one in front of it

            def __init__(self, front, backs, source_size, label):
                super().__init__()
                self._front = front
                self._backs = backs        # nearest card first
                self._source_size = source_size
                self.set_accessible_role(Gtk.AccessibleRole.IMG)
                self.update_property([Gtk.AccessibleProperty.LABEL], [label])

            def set_label(self, label):
                self.update_property([Gtk.AccessibleProperty.LABEL], [label])

            def do_measure(self, orientation, _for_size):
                if orientation == Gtk.Orientation.VERTICAL:
                    return 120, 280, -1, -1
                return 0, 0, -1, -1

            def set_textures(self, front, backs):
                self._front = front
                self._backs = backs
                self.queue_draw()

            def set_source_size(self, size):
                self._source_size = size
                self.queue_draw()

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

        pic = PreviewDeck(None, [], source["size"], _("Image preview"))

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

        def apply_source(path, size, nbytes, total, front, backs):
            """Fill in everything that needed a header or a decode."""
            source.update(path=path, size=size, bytes=nbytes, total=total,
                          probed=True)
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
            if size is not None and front is not None:
                preview_label = _("Image preview: {}").format(Path(path).name)
                if n > 1:
                    preview_label += _(", plus {} more images").format(n - 1)
            else:
                preview_label = _("Image preview (unavailable)")
            pic.set_label(preview_label)
            update_crop_controls()
            apply_textures(front, backs)
            update_sensitive()
            queue_estimate()
            return False

        def load_source():
            GLib.idle_add(apply_source, *discover_source())

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
