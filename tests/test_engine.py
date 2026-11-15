"""Headless tests for the Pillow engine in image_resizer.py.

No GUI is imported. Run with: python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nautilus-image-resizer"))

from PIL import Image  # noqa: E402

import image_resizer as engine  # noqa: E402


def make_image(path, size=(800, 600), fmt="JPEG"):
    im = Image.new("RGB", size, color=(200, 40, 40))
    im.save(path, format=fmt)
    return path


class ComputeSizeTests(unittest.TestCase):
    def test_percent(self):
        self.assertEqual(engine.compute_size(800, 600, "percent", percent=50), (400, 300))

    def test_dimensions_locked(self):
        # Fits inside the box, keeps aspect ratio.
        self.assertEqual(
            engine.compute_size(800, 600, "dimensions", box=(400, 400), lock=True),
            (400, 300),
        )

    def test_dimensions_unlocked(self):
        self.assertEqual(
            engine.compute_size(800, 600, "dimensions", box=(400, 200), lock=False),
            (400, 200),
        )

    def test_clamps_to_at_least_one_pixel(self):
        self.assertEqual(engine.compute_size(10, 10, "percent", percent=1), (1, 1))


class ProcessFileTests(unittest.TestCase):
    def test_percent_resize_writes_smaller_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "photo.jpg")
            dst = Path(tmp) / "photo_resized.jpg"
            result = engine.process_file(str(src), str(dst), "percent", {"percent": 50})
            self.assertTrue(result.ok, result.note)
            with Image.open(dst) as out:
                self.assertEqual(out.size, (400, 300))

    def test_unreadable_file_is_a_failure_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "not-an-image.jpg"
            src.write_text("hello")
            dst = Path(tmp) / "out.jpg"
            result = engine.process_file(str(src), str(dst), "percent", {"percent": 50})
            self.assertFalse(result.ok)
            self.assertFalse(dst.exists())

    def test_estimate_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "photo.png", fmt="PNG")
            im, fmt, exif, icc = engine.load_image(str(src))
            out = engine.estimate(im, fmt, exif, icc, "percent", {"percent": 50})
            self.assertEqual(out["size"], (400, 300))
            self.assertEqual(sorted(Path(tmp).iterdir()), [src])


if __name__ == "__main__":
    unittest.main()
