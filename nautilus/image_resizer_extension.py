"""Nautilus menu provider: "Resize Image…" for selected image files."""

import subprocess
from pathlib import Path

import gi

gi.require_version("Nautilus", "4.1")
from gi.repository import GObject, Nautilus  # noqa: E402

# Where the GUI app lives. System install first, then a user install.
# A system package puts it in /usr/share, `make install` puts it in ~/.local.
GUI_CANDIDATES = (
    "/usr/share/nautilus-image-resizer/image_resizer.py",
    "/usr/local/share/nautilus-image-resizer/image_resizer.py",
    str(Path.home() / ".local/share/nautilus-image-resizer/image_resizer.py"),
)


def _menu_label(count):
    if count == 1:
        return "Resize Image…"
    return f"Resize {count} Images…"


def _find_gui():
    for candidate in GUI_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return GUI_CANDIDATES[-1]


class ImageResizerExtension(GObject.GObject, Nautilus.MenuProvider):
    def get_file_items(self, files):
        paths = []
        for f in files:
            if f.get_uri_scheme() != "file":
                continue
            p = f.get_location().get_path()
            if p and (f.get_mime_type() or "").startswith("image/"):
                paths.append(p)
        if not paths:
            return []
        item = Nautilus.MenuItem(name="ImageResizerExtension::resize",
                                 label=_menu_label(len(paths)))
        item.connect("activate", self._on_activate, paths)
        return [item]

    def _on_activate(self, _item, paths):
        subprocess.Popen(["/usr/bin/python3", _find_gui(), *paths],
                         start_new_session=True)
