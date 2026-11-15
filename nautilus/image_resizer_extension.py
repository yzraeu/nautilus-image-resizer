"""Nautilus menu provider: "Resize Image…" for selected image files."""

import subprocess
from pathlib import Path

import gi

gi.require_version("Nautilus", "4.1")
from gi.repository import GObject, Nautilus  # noqa: E402

GUI = str(Path.home() / ".local/share/nautilus-image-resizer/image_resizer.py")


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
                                 label="Resize Image…")
        item.connect("activate", self._on_activate, paths)
        return [item]

    def _on_activate(self, _item, paths):
        subprocess.Popen(["/usr/bin/python3", GUI, *paths], start_new_session=True)
