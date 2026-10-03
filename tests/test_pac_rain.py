#!/usr/bin/env python3
"""PAC pipeline tests. No network. Uses the checked-in sample frame."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pac_rain as P


class TestLut(unittest.TestCase):
    def test_steps_and_colors_paired(self):
        self.assertEqual(len(P.PAC_MM_STEPS), 16)
        self.assertEqual(len(P.PAC_COLORS), 16)
        self.assertEqual(P.PAC_MM_STEPS[0], 100)
        self.assertEqual(P.PAC_MM_STEPS[-1], 1.0)

    def test_mm_to_color(self):
        self.assertIsNone(P.mm_to_color(None))
        self.assertEqual(P.mm_to_color(100), P.PAC_COLORS[0])
        self.assertEqual(P.mm_to_color(1.0), P.PAC_COLORS[-1])

    def test_calibrate_pac_rings(self):
        from PIL import Image

        src = ROOT / "docs" / "archive" / "2026-10-03" / "pac.png"
        self.assertTrue(src.is_file(), "need frozen PAC frame")
        geom = P.calibrate_pac(Image.open(src))
        self.assertEqual(geom.method, "pac-rings")
        # Radar center in full-frame pixels; scale from the
        # 100:150:200:250 ring-ratio fit. Tight: misplacing Jaffna by
        # ~90px (the old MAX_Z-nominal bug) must fail here.
        self.assertAlmostEqual(geom.x0 + geom.cx, 360, delta=3)
        self.assertAlmostEqual(geom.y0 + geom.cy, 359, delta=3)
        self.assertAlmostEqual(geom.km_per_px, 0.697, delta=0.02)

    def test_jaffna_wet_on_sample_frame(self):
        from PIL import Image

        src = ROOT / "docs" / "archive" / "2026-10-03" / "pac.png"
        self.assertTrue(src.is_file(), "need frozen PAC frame")
        readings = P.parse_pac_image(Image.open(src))
        self.assertEqual(readings[("Jaffna", "Jaffna")], 1.0)

    def test_pac_target_date_follows_issue(self):
        import datetime
        from zoneinfo import ZoneInfo

        ist = ZoneInfo("Asia/Kolkata")

        def at(day, hm):
            h, m = hm
            return datetime.datetime(2026, 10, day, h, m, tzinfo=ist)

        # Before the ~08:30 IST issue the live product is still yesterday's.
        self.assertEqual(P.pac_target_date(at(3, (0, 35))), "2026-10-02")
        self.assertEqual(P.pac_target_date(at(3, (8, 29))), "2026-10-02")
        # After the cutoff the issue belongs to today.
        self.assertEqual(P.pac_target_date(at(3, (9, 30))), "2026-10-03")
        self.assertEqual(P.pac_target_date(at(3, (12, 0))), "2026-10-03")

    def test_ocr_pac_issue_date(self):
        try:
            import pytesseract  # noqa: F401
        except ImportError:
            self.skipTest("tesseract not installed")
            return
        from PIL import Image

        src = ROOT / "docs" / "archive" / "2026-10-03" / "pac.png"
        self.assertTrue(src.is_file(), "need frozen PAC frame")
        # Panel stamp "03:00:00Z 3 OCT 2026 UTC" -> IST issue date.
        self.assertEqual(P.ocr_pac_issue_date(Image.open(src)),
                         "2026-10-03")

    def test_export_shape(self):
        out = P.export_pac_json()
        data = json.loads(out.read_text())
        self.assertIn(data["radar"], "KKL_PAC (Karaikal 24H accumulation)")
        self.assertEqual(len(data["lut"]), 16)
        for d in data["days"]:
            self.assertRegex(d["date"], r"^\d{4}-\d{2}-\d{2}$")
            for r in d["rows"]:
                if r["mm"] is None:
                    self.assertIsNone(r["color"])
                else:
                    self.assertIn(r["mm"], P.PAC_MM_STEPS)


if __name__ == "__main__":
    unittest.main()
