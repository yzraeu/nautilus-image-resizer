"""Headless tests for the engine and the preview layout.

No GUI is imported: `resize_engine` is Pillow only, and the preview geometry in
`image_resizer` is pure module-level code. Run with:
python3 -m unittest discover -s tests -v (or directly: python3 tests/test_engine.py).
"""

import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nautilus-image-resizer"))

from PIL import Image  # noqa: E402

import image_resizer as preview  # noqa: E402
import resize_engine as engine  # noqa: E402


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

    def test_render_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "photo.png", fmt="PNG")
            im, fmt, exif, icc = engine.load_image(str(src))
            out = engine.render(im, fmt, exif, icc, "percent", {"percent": 50})
            self.assertEqual(out.size, (400, 300))
            self.assertEqual(sorted(Path(tmp).iterdir()), [src])

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


class CropKeyboardTests(unittest.TestCase):
    """Arrow-key crop manipulation: rect math, same conventions as crop_drag_rect."""

    SIZE = (800, 600)

    def test_move_translates_the_rect(self):
        self.assertEqual(engine.crop_move_rect((100, 100, 200, 150), 50, -20, self.SIZE),
                         (150, 80, 200, 150))

    def test_move_clamps_to_the_image(self):
        self.assertEqual(engine.crop_move_rect((100, 100, 200, 150), 900, -900, self.SIZE),
                         (600, 0, 200, 150))

    def test_resize_without_aspect_grows_the_rect(self):
        self.assertEqual(engine.crop_resize_rect((10, 20, 100, 80), 40, 30, self.SIZE),
                         (10, 20, 140, 110))

    def test_resize_keeps_a_locked_aspect(self):
        x, y, w, h = engine.crop_resize_rect((0, 0, 100, 100), 40, 0, self.SIZE, aspect=1.5)
        self.assertEqual((x, y), (0, 0))
        self.assertAlmostEqual(w / h, 1.5, delta=0.02)

    def test_resize_from_the_height_when_the_width_step_is_zero(self):
        _x, _y, w, h = engine.crop_resize_rect((0, 0, 100, 100), 0, 30, self.SIZE, aspect=2.0)
        self.assertEqual(h, 130)
        self.assertEqual(w, 260)

    def test_resize_clamps_to_the_image(self):
        x, y, w, h = engine.crop_resize_rect((700, 500, 100, 100), 500, 500, self.SIZE)
        self.assertEqual((x + w, y + h), self.SIZE)

    def test_resize_keeps_the_anchor_at_the_image_edge(self):
        # No room to grow: the rect stops growing instead of sliding left.
        self.assertEqual(engine.crop_resize_rect((700, 500, 100, 100), 80, 0, self.SIZE),
                         (700, 500, 100, 100))

    def test_aspect_survives_the_image_edge(self):
        _x, _y, w, h = engine.crop_resize_rect((0, 0, 500, 500), 200, 0,
                                              (800, 600), aspect=1.0)
        self.assertEqual((w, h), (600, 600))

    def test_resize_never_shrinks_below_the_minimum(self):
        x, y, w, h = engine.crop_resize_rect((0, 0, 20, 20), -500, -500, self.SIZE)
        self.assertGreaterEqual(w, 16)
        self.assertGreaterEqual(h, 16)


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
            predicted, skipped, error = engine.estimate_files(
                [str(src)], "percent", params)
            result = engine.process_file(str(src), str(dst), "percent", params)
            self.assertTrue(result.ok, result.note)
            self.assertEqual((skipped, error), (0, None))
            self.assertEqual(predicted[0]["size"], (288, 192))
            self.assertEqual(predicted[0]["bytes"], result.out_bytes)
            self.assertEqual(result.out_bytes, dst.stat().st_size)

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
            out = engine.render(im, fmt, exif, icc, "percent",
                                {"percent": 40}, source)
            self.assertEqual(out.size, (480, 320))


class EstimateFilesTests(unittest.TestCase):
    def test_results_follow_input_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [str(make_image(Path(tmp) / f"img{i}.jpg", size=size))
                     for i, size in enumerate(((200, 100), (400, 100), (800, 100)))]
            results, skipped, error = engine.estimate_files(
                paths, "percent", {"percent": 50})
            self.assertEqual([r["size"] for r in results],
                             [(100, 50), (200, 50), (400, 50)])
            self.assertEqual(skipped, 0)
            self.assertIsNone(error)

    def test_failures_are_skipped_and_last_error_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = str(make_image(Path(tmp) / "good.jpg", size=(200, 100)))
            text = Path(tmp) / "text-file.jpg"
            text.write_text("hello")
            missing = str(Path(tmp) / "missing.jpg")
            results, skipped, error = engine.estimate_files(
                [good, str(text), missing], "percent", {"percent": 50})
            self.assertEqual([r["size"] for r in results], [(100, 50)])
            self.assertEqual(skipped, 2)
            self.assertTrue(error.startswith("FileNotFoundError"), error)
            # A single bad file still reports its own failure.
            _results, text_skipped, text_error = engine.estimate_files(
                [str(text)], "percent", {"percent": 50})
            self.assertEqual(text_skipped, 1)
            self.assertTrue(text_error.startswith("UnidentifiedImageError"),
                            text_error)

    def test_abort_short_circuits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(make_image(Path(tmp) / "img.jpg"))
            self.assertEqual(
                engine.estimate_files([path], "percent", {"percent": 50},
                                      should_abort=lambda: True),
                ([], 0, None))

class DecodeHintTests(unittest.TestCase):
    """The hint decides which pixels both adapters see, so it is load-bearing."""

    HEADER = (1200, 800)

    def test_no_crop_asks_for_the_target_only(self):
        self.assertEqual(engine.decode_hint("percent", {"percent": 40}, self.HEADER),
                         (480, 320))

    def test_crop_scales_the_request_back_up_to_source_pixels(self):
        # Region is 60% of the image, so the decoded raster must be larger than
        # the target by 1/0.6 or the crop would have to be upscaled.
        params = {"percent": 40, "crop": {"fx": 0.1, "fy": 0.1, "fw": 0.6, "fh": 0.6}}
        self.assertEqual(engine.decode_hint("percent", params, self.HEADER),
                         (480, 320))

    def test_full_percent_under_a_quarter_crop_needs_the_whole_raster(self):
        params = {"percent": 100, "crop": {"fx": 0, "fy": 0, "fw": 0.25, "fh": 0.25}}
        self.assertEqual(engine.decode_hint("percent", params, self.HEADER),
                         self.HEADER)

    def test_dimensions_never_ask_for_more_than_the_region(self):
        params = {"width": 4000, "height": 4000, "lock": True}
        self.assertEqual(engine.decode_hint("dimensions", params, self.HEADER),
                         self.HEADER)

    def test_filesize_gets_no_hint(self):
        self.assertIsNone(engine.decode_hint("filesize", {"max_bytes": 50000},
                                             self.HEADER))

    def test_the_cropped_region_always_covers_the_target(self):
        params = {"percent": 40, "crop": {"fx": 0.1, "fy": 0.1, "fw": 0.6, "fh": 0.6}}
        bound = engine.decode_hint("percent", params, self.HEADER)
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "photo.jpg", size=self.HEADER)
            im, _fmt, _exif, _icc = engine.load_image(str(src), bound)
            _x, _y, cw, ch = engine.crop_box(im.size, params["crop"])
            self.assertGreaterEqual(cw, 288)
            self.assertGreaterEqual(ch, 192)


class ParityTests(unittest.TestCase):
    """The subtitle's byte count is the written file's length, not a guess."""

    SIZE = (3000, 2000)      # large enough that the decode hint actually bites

    def _parity(self, mode, params):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "photo.jpg", size=self.SIZE)
            dst = Path(tmp) / "out.jpg"
            predicted, skipped, error = engine.estimate_files([str(src)], mode, params)
            result = engine.process_file(str(src), str(dst), mode, params)
            self.assertTrue(result.ok, result.note)
            self.assertEqual((skipped, error), (0, None))
            self.assertEqual(predicted[0]["bytes"], result.out_bytes)
            self.assertEqual(result.out_bytes, dst.stat().st_size)
            with Image.open(dst) as out:
                self.assertEqual(out.size, predicted[0]["size"])
            return predicted[0]["size"]

    def test_percent_parity_on_a_reduced_decode(self):
        bound = engine.decode_bound(self.SIZE, engine.decode_hint(
            "percent", {"percent": 40}, self.SIZE))
        self.assertLess(bound[0], self.SIZE[0])      # the hint really is coarse
        self.assertEqual(self._parity("percent", {"percent": 40}), (1200, 800))

    def test_dimensions_parity_on_a_reduced_decode(self):
        self.assertEqual(
            self._parity("dimensions", {"width": 900, "height": 600, "lock": True}),
            (900, 600))

    def test_crop_plus_percent_parity(self):
        params = {"percent": 40, "crop": {"fx": 0.1, "fy": 0.1, "fw": 0.6, "fh": 0.6}}
        self.assertEqual(self._parity("percent", params), (720, 480))

    def test_filesize_parity(self):
        size = self._parity("filesize", {"max_bytes": 30000})
        self.assertLessEqual(size[0], self.SIZE[0])
        self.assertLessEqual(size[1], self.SIZE[1])


class FilesizeModeTests(unittest.TestCase):
    def test_a_reachable_target_is_met(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "photo.jpg", size=(1200, 800))
            dst = Path(tmp) / "out.jpg"
            result = engine.process_file(str(src), str(dst), "filesize",
                                         {"max_bytes": 20000})
            self.assertTrue(result.ok, result.note)
            self.assertIsNone(result.note)
            self.assertLessEqual(dst.stat().st_size, 20000)

    def test_an_unreachable_target_still_writes_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "photo.jpg", size=(1200, 800))
            dst = Path(tmp) / "out.jpg"
            result = engine.process_file(str(src), str(dst), "filesize",
                                         {"max_bytes": 10})
            self.assertTrue(result.ok)
            self.assertIn("not reached", result.note or "")
            self.assertGreater(dst.stat().st_size, 10)

    def test_a_lossless_format_shrinks_to_the_floor_and_says_so(self):
        # PNG has no quality ladder, so the only lever is resolution: the loop
        # must stop at MIN_EDGE rather than spin.
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "photo.png", size=(900, 600), fmt="PNG")
            dst = Path(tmp) / "out.png"
            result = engine.process_file(str(src), str(dst), "filesize",
                                         {"max_bytes": 10})
            self.assertTrue(result.ok)
            self.assertIn("not reached", result.note or "")
            with Image.open(dst) as out:
                self.assertEqual(out.format, "PNG")
                self.assertLessEqual(max(out.size), engine.MIN_EDGE)


class JobContractTests(unittest.TestCase):
    """process_file promises a JobResult for every input. Make it keep that."""

    def test_an_oversized_raster_is_a_failed_result_not_a_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "pano.jpg", size=(120, 80))
            dst = Path(tmp) / "out.jpg"
            saved = Image.MAX_IMAGE_PIXELS
            Image.MAX_IMAGE_PIXELS = 100      # stands in for a >2x-limit photo
            try:
                result = engine.process_file(str(src), str(dst), "percent",
                                             {"percent": 50})
            finally:
                Image.MAX_IMAGE_PIXELS = saved
            self.assertFalse(result.ok)
            self.assertIn("DecompressionBombError", result.note)
            self.assertFalse(dst.exists())

    def test_the_same_file_is_a_skipped_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "pano.jpg", size=(120, 80))
            saved = Image.MAX_IMAGE_PIXELS
            Image.MAX_IMAGE_PIXELS = 100
            try:
                results, skipped, error = engine.estimate_files(
                    [str(src)], "percent", {"percent": 50})
            finally:
                Image.MAX_IMAGE_PIXELS = saved
            self.assertEqual((results, skipped), ([], 1))
            self.assertIn("DecompressionBombError", error)

    def test_an_unwritable_destination_is_a_failed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_image(Path(tmp) / "photo.jpg", size=(200, 100))
            dst = Path(tmp) / "missing-dir" / "out.jpg"
            result = engine.process_file(str(src), str(dst), "percent",
                                         {"percent": 50})
            self.assertFalse(result.ok)
            self.assertIn("FileNotFoundError", result.note)


class ScatterLayoutTests(unittest.TestCase):
    """The pile must be deterministic, inside the spread, and aspect-true."""

    def test_same_seed_gives_the_same_pile(self):
        self.assertEqual(preview.scatter_layout(7, 5),
                         preview.scatter_layout(7, 5))

    def test_different_seeds_give_different_piles(self):
        self.assertNotEqual(preview.scatter_layout(1, 5),
                            preview.scatter_layout(2, 5))

    def test_front_card_is_unrotated_and_centred(self):
        # The crop overlay maps to its rect with an axis-aligned assumption.
        for seed in range(20):
            front = preview.scatter_layout(seed, 5)[0]
            self.assertEqual(front.angle, 0.0)
            self.assertEqual((front.dx, front.dy), (0.0, 0.0))

    def test_backs_stay_inside_the_big_spread(self):
        for seed in range(20):
            cards = preview.scatter_layout(seed, 5)
            self.assertEqual(len(cards), 5)
            for card in cards[1:]:
                self.assertLessEqual(abs(card.angle), preview.CARD_ANGLE)
                self.assertLessEqual(abs(card.dx), preview.CARD_DX)
                self.assertLessEqual(abs(card.dy), preview.CARD_DY)
                self.assertGreaterEqual(card.width, preview.CARD_WIDTH[0])
                self.assertLessEqual(card.width, preview.CARD_WIDTH[1])

    def test_a_zero_spread_is_a_neat_pile(self):
        for card in preview.scatter_layout(3, 4, spread=0)[1:]:
            self.assertEqual((card.angle, card.dx, card.dy),
                             (0.0, 0.0, 0.0))

    def test_card_keeps_the_photo_aspect_inside_the_border(self):
        card = preview.ScatterCard(width=300.0, angle=0.0, dx=0.0, dy=0.0)
        for aspect in (4 / 3, 3 / 4, 16 / 9, 1.0):
            width, height = preview.card_size(card, aspect)
            photo_w = width - 2 * preview.CARD_BORDER
            photo_h = height - 2 * preview.CARD_BORDER
            self.assertAlmostEqual(photo_w / photo_h, aspect)

    def test_the_front_card_is_drawn_above_its_base_size(self):
        card = preview.ScatterCard(width=300.0, angle=0.0, dx=0.0, dy=0.0)
        base = preview.card_size(card, 4 / 3)
        front = preview.card_size(card, 4 / 3, preview.FRONT_SCALE)
        self.assertAlmostEqual(front[0] / base[0], preview.FRONT_SCALE)
        self.assertGreater(front[1], base[1])

    def test_bounds_cover_every_rotated_card(self):
        cards = preview.scatter_layout(11, 5)
        aspects = [4 / 3, 3 / 4, 16 / 9, 1.0, 4 / 3]
        x, y, width, height = preview.scatter_bounds(cards, aspects)
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        for index, card in enumerate(cards):
            card_w, card_h = preview.card_size(
                card, aspects[index],
                preview.FRONT_SCALE if index == 0 else 1.0)
            radians = math.radians(card.angle)
            cos, sin = abs(math.cos(radians)), abs(math.sin(radians))
            half_w = (card_w * cos + card_h * sin) / 2
            half_h = (card_w * sin + card_h * cos) / 2
            self.assertLessEqual(x, card.dx - half_w + 1e-9)
            self.assertGreaterEqual(x + width, card.dx + half_w - 1e-9)
            self.assertLessEqual(y, card.dy - half_h + 1e-9)
            self.assertGreaterEqual(y + height, card.dy + half_h - 1e-9)


class FallEasingTests(unittest.TestCase):
    def test_starts_at_zero_and_lands_at_one(self):
        self.assertEqual(preview.ease_out(0.0), 0.0)
        self.assertEqual(preview.ease_out(1.0), 1.0)

    def test_clamps_outside_the_progress_range(self):
        self.assertEqual(preview.ease_out(-1.0), 0.0)
        self.assertEqual(preview.ease_out(2.0), 1.0)

    def test_is_monotonic_and_finishes_ahead_of_linear(self):
        previous = 0.0
        for step in range(1, 100):
            value = preview.ease_out(step / 100)
            self.assertGreaterEqual(value, previous)
            previous = value
        self.assertGreater(preview.ease_out(0.5), 0.5)


if __name__ == "__main__":
    unittest.main()
