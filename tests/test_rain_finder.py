#!/usr/bin/env python3
"""Unit tests for scripts/rain_finder.py. Stdlib unittest, no fixtures."""
import datetime
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np
from PIL import Image

from rain_finder import (
    CATEGORIES,
    DRY_LABEL,
    LEGEND_BAND_YS_720,
    OUT_OF_RANGE_LABEL,
    RADAR_LAT,
    RADAR_LON,
    DBZ_COLORS,
    DBZ_STEPS,
    FrameGeom,
    RainFinder,
    calibrate,
    category_of,
    dbz_to_color,
    latlon_to_km,
    ov_win,
)


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


class TestCategory(unittest.TestCase):
    def test_none_splits_on_range(self):
        self.assertEqual(category_of(None, False), OUT_OF_RANGE_LABEL)
        self.assertEqual(category_of(None, True), DRY_LABEL)

    def test_threshold_edges(self):
        self.assertEqual(category_of(19.9, True), "light (20-30 dBZ)")
        self.assertEqual(category_of(20.0, True), "light (20-30 dBZ)")
        self.assertEqual(category_of(29.9, True), "light (20-30 dBZ)")
        self.assertEqual(category_of(30.0, True), "moderate (30-40 dBZ)")
        self.assertEqual(category_of(40.0, True), "heavy (40-50 dBZ)")
        self.assertEqual(category_of(50.0, True), "very heavy (>50 dBZ)")
        self.assertEqual(category_of(60.0, True), "very heavy (>50 dBZ)")

    def test_matches_declared_table(self):
        self.assertEqual([t for t, _ in CATEGORIES], [50.0, 40.0, 30.0, 20.0])


class TestDbzColor(unittest.TestCase):
    def test_none_is_none(self):
        self.assertIsNone(dbz_to_color(None))

    def test_exact_steps_map_to_own_color(self):
        for dbz, color in zip(DBZ_STEPS, DBZ_COLORS):
            self.assertEqual(dbz_to_color(dbz), color)

    def test_midpoint_picks_nearest(self):
        self.assertEqual(dbz_to_color(45.0), "#ffb600")


class TestLatLon(unittest.TestCase):
    def test_center_is_zero(self):
        self.assertEqual(latlon_to_km(RADAR_LAT, RADAR_LON,
                                      RADAR_LAT, RADAR_LON), (0.0, 0.0))

    def test_north_is_positive_nk(self):
        ek, nk = latlon_to_km(RADAR_LAT + 0.01, RADAR_LON,
                              RADAR_LAT, RADAR_LON)
        self.assertAlmostEqual(ek, 0.0, places=6)
        self.assertAlmostEqual(nk, 1.11195, places=3)


class TestOvWin(unittest.TestCase):
    def test_dry_window(self):
        max_dbz, cover = ov_win(np.zeros((5, 5), bool),
                                np.full((5, 5), 25.0), 2, 2, 2)
        self.assertIsNone(max_dbz)
        self.assertEqual(cover, 0.0)

    def test_single_pixel_window(self):
        echo = np.zeros((5, 5), bool)
        echo[2, 2] = True
        max_dbz, cover = ov_win(echo, np.full((5, 5), 42.0), 2, 2, 0)
        self.assertEqual(max_dbz, 42.0)
        self.assertEqual(cover, 1.0)


def tiny_finder(window_px: int = 5) -> RainFinder:
    geom = FrameGeom(0, 0, 7, 7, 3.0, 3.0, 1.0, "test")
    echo = np.zeros((7, 7), bool)
    echo[3, 3] = True
    dbz = np.full((7, 7), 25.0)
    dbz[3, 3] = 45.0
    return RainFinder(geom, echo, dbz, window_px)


class TestRainFinder(unittest.TestCase):
    def test_center_reading(self):
        r = tiny_finder().reading_for(RADAR_LAT, RADAR_LON)
        self.assertEqual(r.dist_km, 0.0)
        self.assertEqual(r.max_dbz, 45.0)
        self.assertEqual(r.cover_pct, 4.0)
        self.assertEqual(r.nearest_echo_km, 0.0)
        self.assertEqual(r.category, "heavy (40-50 dBZ)")

    def test_frame_stats(self):
        f = tiny_finder()
        self.assertEqual(f.echo_pixels, 1)
        self.assertEqual(f.max_dbz_frame, 45.0)

    def test_echo_free(self):
        geom = FrameGeom(0, 0, 7, 7, 3.0, 3.0, 1.0, "test")
        f = RainFinder.echo_free(geom)
        self.assertEqual(f.echo_pixels, 0)
        self.assertIsNone(f.max_dbz_frame)
        r = f.reading_for(RADAR_LAT, RADAR_LON)
        self.assertEqual(
            (r.dist_km, r.max_dbz, r.cover_pct, r.nearest_echo_km,
             r.category),
            (0.0, None, 0.0, 999.9, DRY_LABEL))


def synthetic_frame() -> Image.Image:
    """880x720 frame with an exact legend and one 52 dBZ echo pixel."""
    im = Image.new("RGB", (880, 720), (0, 0, 0))
    px = im.load()
    for yref, color in zip(LEGEND_BAND_YS_720, DBZ_COLORS):
        r, g, b = hex_rgb(color)
        for yy in range(yref - 2, yref + 3):
            for xx in range(787, 795):
                px[xx, yy] = (r, g, b)
    r, g, b = hex_rgb("#ff3a00")
    px[260, 460] = (r, g, b)
    return im


class TestFromImage(unittest.TestCase):
    def test_synthetic_frame_finds_echo(self):
        geom = FrameGeom(0, 200, 519, 719, 259.7, 259.38, 0.9614, "test")
        f = RainFinder.from_image(synthetic_frame(), geom)
        self.assertGreaterEqual(f.echo_pixels, 1)
        self.assertEqual(f.max_dbz_frame, 52.0)
        r = f.reading_for(RADAR_LAT, RADAR_LON)
        self.assertEqual(r.max_dbz, 52.0)
        self.assertEqual(r.category, "very heavy (>50 dBZ)")


class TestCalibrate(unittest.TestCase):
    def test_blank_uses_fallback(self):
        im = Image.new("RGB", (880, 720), (0, 0, 0))
        g = calibrate(im, fallback=(1.0, 2.0, 3.0))
        self.assertEqual((g.cx, g.cy, g.km_per_px), (1.0, 2.0, 3.0))
        self.assertEqual(g.method, "ring-labels-fallback")

    def test_blank_without_fallback_raises(self):
        im = Image.new("RGB", (880, 720), (0, 0, 0))
        with self.assertRaises(ValueError):
            calibrate(im, fallback=None)


if __name__ == "__main__":
    unittest.main()
