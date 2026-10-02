#!/usr/bin/env python3
"""DB-layer tests for scripts/kkl_db.py. Temp databases only."""
import datetime
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import kkl_db as K


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        K.init_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db, timeout=30)
        con.execute("PRAGMA foreign_keys=ON")
        return con


class TestInit(TempDb):
    def test_seeds_all_places(self):
        con = self.connect()
        n = con.execute("SELECT COUNT(*) FROM places").fetchone()[0]
        master = __import__("json").loads(K.MASTER_JSON.read_text())
        self.assertEqual(n, len(master))
        self.assertGreater(n, 0)
        con.close()

    def test_init_is_idempotent(self):
        K.init_db(self.db)
        con = self.connect()
        n = con.execute("SELECT COUNT(*) FROM places").fetchone()[0]
        master = __import__("json").loads(K.MASTER_JSON.read_text())
        self.assertEqual(n, len(master))
        con.close()

    def test_migrate_is_idempotent(self):
        con = self.connect()
        K.migrate(con)
        K.migrate(con)
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"places", "frames", "readings", "hourly_rain"} <= tables)
        con.close()


class TestHourly(TempDb):
    def _seed_frame(self, con, frame_utc="2026-09-30 17:32:23Z"):
        cur = con.execute(
            "INSERT INTO frames (frame_utc, frame_ist, src_path, radar_lat, "
            "radar_lon, center_px_x, center_px_y, km_per_px, calib_method) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (frame_utc, "2026-09-30 23:02 IST", "x.png",
             K.RADAR_LAT, K.RADAR_LON, 259.7, 259.38, 0.9614, "ring-labels"))
        return cur.lastrowid

    def test_rebuild_labels_wet_and_dry(self):
        con = self.connect()
        fid = self._seed_frame(con)
        pids = [r[0] for r in con.execute(
            "SELECT place_id FROM places ORDER BY place_id LIMIT 2")]
        K.insert_reading(con, fid, pids[0], 10.0, 45.0, 50.0, 5.0,
                         "heavy (40-50 dBZ)", 5)
        K.insert_reading(con, fid, pids[1], 20.0, None, 0.0, 999.9,
                         "no echo (<20 dBZ)", 5)
        con.commit()
        con.close()
        self.assertEqual(K.rebuild_hourly(self.db), 2)
        con = self.connect()
        cats = dict(con.execute(
            "SELECT place_id, category FROM hourly_rain"))
        self.assertEqual(cats[pids[0]], "heavy (40-50 dBZ)")
        self.assertEqual(cats[pids[1]], "no echo (<20 dBZ)")
        con.close()


class TestTimestamps(unittest.TestCase):
    def test_parse_frame_ist(self):
        self.assertEqual(K.parse_frame_ist("2026-09-30 23:02 IST"),
                         ("2026-09-30", 23))
        self.assertIsNone(K.parse_frame_ist("not a timestamp"))

    def test_utc_from_ist(self):
        self.assertEqual(
            K.utc_str_from_ist(datetime.datetime(2026, 9, 30, 23, 2)),
            "2026-09-30 17:32:00Z")


if __name__ == "__main__":
    unittest.main()
