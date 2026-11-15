# Nautilus Image Resizer

Right-click an image in GNOME Files (Nautilus), choose **Resize Image**, and pick a size or crop. That is the whole app.

This is a Nautilus plugin plus a small GTK4 / libadwaita app. It is not a GNOME Shell extension, and it is not distributed through extensions.gnome.org.

## What it does

- Adds a `Resize Image…` entry to the right-click menu for image files in Files.
- Opens a dialog with a preview, live output estimate, and crop controls.
- Resize modes: percent, exact dimensions (with aspect lock), and target file size.
- Crop presets: free, original, 1:1, 5:4, 4:3, 3:2, 16:10, 16:9, 21:9, 19:6, each with a portrait toggle.
- Output: overwrite the original, or save a copy with a suffix (default `_resized`).
- Formats: JPEG, PNG, WEBP, BMP, TIFF. Animated and multi-frame images are refused.
- Engine: Pillow, with EXIF orientation handling so the output is never rotated twice.

## Requirements

- GNOME Files (Nautilus) 43 or later. Tested on Nautilus 51.1.
- `nautilus-python` 4.x (Fedora: `nautilus-python`, Debian/Ubuntu: `python3-nautilus`).
- Python 3, `python3-pillow`, `python3-gobject`, GTK 4, libadwaita.
- The plugin is loaded by Nautilus, so restart Files after install (`nautilus -q`).

Fedora:

```sh
sudo dnf install nautilus-python python3-pillow python3-gobject gtk4 libadwaita
```

Debian / Ubuntu:

```sh
sudo apt install python3-nautilus python3-pil python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```

## Install

From a checkout of this repo:

```sh
make install
nautilus -q
```

That installs for your user only:

- App: `~/.local/share/nautilus-image-resizer/image_resizer.py`
- Plugin: `~/.local/share/nautilus-python/extensions/image_resizer_extension.py`
- Desktop file: `~/.local/share/applications/dev.izzy.image-resizer.desktop`
- App metadata: `~/.local/share/metainfo/dev.izzy.image-resizer.metainfo.xml`

Uninstall:

```sh
make uninstall
nautilus -q
```

## Use

1. In Files, right-click one or more images.
2. Choose **Resize Image…**
3. Pick Percent, Dimensions, or File Size, adjust crop if you want it, and choose overwrite or a suffix.
4. Hit **Resize**.

The app can also be run directly, for testing:

```sh
python3 ~/.local/share/nautilus-image-resizer/image_resizer.py photo.jpg
```

## Layout

```text
nautilus-image-resizer/image_resizer.py   # GTK app + Pillow engine
nautilus/image_resizer_extension.py       # Nautilus MenuProvider, launches the app
data/dev.izzy.image-resizer.desktop.in    # desktop file template
data/dev.izzy.image-resizer.metainfo.xml  # AppStream metadata
tests/test_engine.py                      # headless engine tests, no GUI needed
Makefile                                  # install / uninstall / test
```

The plugin launches the app with the selected paths. It looks for the app at `~/.local/share/nautilus-image-resizer/image_resizer.py`, which is where `make install` puts it. A system-wide package will need to point it at the packaged path instead.

## Development

```sh
make test
```

That byte-compiles both Python files and runs the engine tests in `tests/`. The GUI is only imported when the app runs, so the engine tests run headless with Pillow only.

## License

GPL-3.0-or-later. See `LICENSE`.
