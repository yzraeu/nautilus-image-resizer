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

class CropGeometryTests(unittest.TestCase):
    def test_crop_box_uses_fractions(self):
        crop = {"fx": 0.25, "fy": 0.5, "fw": 0.5, "fh": 0.5}
        self.assertEqual(engine.crop_box((1200, 800), crop), (300, 400, 600, 400))

    def test_crop_box_clamps_to_one_pixel(self):
        crop = {"fx": 0.99, "fy": 0.99, "fw": 0.99, "fh": 0.99}
        self.assertEqual(engine.crop_box((100, 100), crop), (99, 99, 1, 1))

    def test_fit_rect_is_centred(self):
        self.assertEqual(engine.crop_fit_rect((1200, 800), 16 / 9), (0, 62, 1200, 675))

    def test_refit_rect_keeps_the_centre(self):
        self.assertEqual(engine.crop_refit_rect((100, 100, 400, 400), 1.5),
                         (100, 167, 400, 267))

    def test_drag_without_aspect(self):
        self.assertEqual(engine.crop_drag_rect(100, 100, 400, 300, (1200, 800)),
                         (100.0, 100.0, 300.0, 200.0))

    def test_drag_keeps_the_locked_aspect(self):
        _x, _y, w, h = engine.crop_drag_rect(100, 100, 900, 300, (1200, 800), 16 / 9)
        self.assertAlmostEqual(w / h, 16 / 9)

    def test_drag_is_clamped_to_the_image(self):
        x, y, w, h = engine.crop_drag_rect(1100, 700, 1300, 900, (1200, 800), 16 / 9)
        self.assertLessEqual(x + w, 1200)
        self.assertLessEqual(y + h, 800)


class CropProcessingTests(unittest.TestCase):
    def test_crop_then_resize(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "photo.jpg", size=(1200, 800))
            dst = Path(tmp) / "photo_resized.jpg"
            crop = {"fx": 0.25, "fy": 0.5, "fw": 0.5, "fh": 0.5}
            result = engine.process_file(str(src), str(dst), "percent",
                                         {"percent": 50, "crop": crop})
            self.assertTrue(result.ok, result.note)
            with Image.open(dst) as out:
                self.assertEqual(out.size, (300, 200))

    def test_full_frame_crop_returns_the_same_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "photo.jpg")
            im, _fmt, _exif, _icc = engine.load_image(str(src))
            self.assertIs(engine.apply_crop(im, {"fx": 0, "fy": 0, "fw": 1, "fh": 1}), im)

    def test_crop_estimate_matches_the_written_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "photo.jpg", size=(1200, 800))
            dst = Path(tmp) / "out.jpg"
            crop = {"fx": 0.1, "fy": 0.1, "fw": 0.6, "fh": 0.6}
            params = {"percent": 40, "crop": crop}
            result = engine.process_file(str(src), str(dst), "percent", params)
            im, fmt, exif, icc = engine.load_image(str(src))
            predicted = engine.estimate(im, fmt, exif, icc, "percent", params)
            self.assertEqual(predicted["size"], (288, 192))
            self.assertEqual(predicted["bytes"], result.out_bytes)

class PresetTests(unittest.TestCase):
    def test_presets_cover_the_expected_ratios(self):
        self.assertEqual(
            list(engine.CROP_PRESETS),
            ["free", "original", "1:1", "5:4", "4:3", "3:2", "16:10", "16:9", "21:9", "19:6"],
        )

    def test_named_ratios_match_their_aspect(self):
        self.assertEqual(engine.CROP_PRESETS["19:6"], 19 / 6)
        self.assertEqual(engine.CROP_PRESETS["21:9"], 21 / 9)
        self.assertAlmostEqual(engine.CROP_PRESETS["16:10"], 1.6)

    def test_every_fixed_preset_fits_the_image(self):
        for name, aspect in engine.CROP_PRESETS.items():
            if aspect is None:
                continue
            _x, _y, w, h = engine.crop_fit_rect((1200, 800), aspect)
            self.assertLessEqual(w, 1200, name)
            self.assertLessEqual(h, 800, name)


class ExifOrientationTests(unittest.TestCase):
    def _rotated_photo(self, tmp):
        path = Path(tmp) / "rotated.jpg"
        exif = Image.Exif()
        exif[0x0112] = 6           # displayed rotated 90° clockwise
        Image.new("RGB", (80, 40), color=(0, 90, 200)).save(
            path, format="JPEG", exif=exif)
        return path

    def test_header_reports_the_displayed_size_without_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._rotated_photo(tmp)
            with Image.open(path) as probe:
                self.assertEqual(engine.oriented_size(probe), (40, 80))

    def test_load_transposes_and_drops_the_orientation_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._rotated_photo(tmp)
            im, _fmt, exif, _icc = engine.load_image(str(path))
            self.assertEqual(im.size, (40, 80))
            saved = Image.Exif()
            saved.load(exif)
            self.assertNotIn(0x0112, saved)

    def test_resize_uses_displayed_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._rotated_photo(tmp)
            dst = Path(tmp) / "out.jpg"
            result = engine.process_file(str(path), str(dst), "percent",
                                         {"percent": 50})
            self.assertTrue(result.ok, result.note)
            with Image.open(dst) as out:
                self.assertEqual(out.size, (20, 40))


class EstimateOnReducedDecodeTests(unittest.TestCase):
    def test_estimate_uses_the_source_size_not_the_decode_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "photo.jpg", size=(1200, 800))
            with Image.open(src) as probe:
                source = engine.oriented_size(probe)
            bound = engine.decode_bound(source, (480, 320))
            im, fmt, exif, icc = engine.load_image(str(src), bound)
            self.assertLess(im.width, source[0])    # decoded coarser than source
            out = engine.estimate(im, fmt, exif, icc, "percent",
                                  {"percent": 40}, source)
            self.assertEqual(out["size"], (480, 320))
