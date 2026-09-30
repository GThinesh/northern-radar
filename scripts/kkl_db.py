#!/usr/bin/env python3
"""KKL radar -> places -> SQLite pipeline.

Reads an IMD Karaikal frame, maps dBZ onto every place in
data/places.json (the single source of truth for locations), stores the
result in SQLite. One primary name per place; OSM spelling variants are
only a match-time concern and are never stored.

Schema: places | frames | readings | hourly_rain
        (+ place_timeseries / latest_per_place / hourly_timeseries views)

Usage:
  python scripts/kkl_db.py init [--db data/kkl.db]
  python scripts/kkl_db.py ingest --src docs/archive/.../HHMMSS_NNN.png \
      --frame-utc "2026-09-14 16:34:02Z" --frame-ist "2026-09-14 22:04 IST"
   python scripts/kkl_db.py backfill   # every PNG in docs/archive (idempotent)
   python scripts/kkl_db.py fill-missing  # readings for places added
                                          # after their frames (idempotent)
  python scripts/kkl_db.py hourly     # rebuild hourly_rain from readings
  python scripts/kkl_db.py export     # data/latest.csv + data/timeseries.csv
                                       # + data/frames.csv + docs/data/rain.json
  python scripts/kkl_db.py rainjson   # docs/data/rain.json only
  python scripts/kkl_db.py latest [--db ...]
  python scripts/kkl_db.py series --place Navaly [--db ...]
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import math
import re
import sqlite3
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "kkl.db"
ARCHIVE_DIR = ROOT / "docs" / "archive"
MASTER_JSON = ROOT / "data" / "places.json"
# Legacy reference only (kept for history, never read at runtime):
#   samples/kkl-jaffna/master_places.json + master_places_dbz.json
# were merged into data/places.json. Add/update a place there and nothing
# else needs to change; init upserts it into SQLite and backfill/export
# refresh the rain table + CSVs.
LAST_RE = re.compile(r"(\d{4})-UTC_(\d{4})-IST_last\.png$")

RADAR_LAT, RADAR_LON = 10.9327, 79.8319
R_EARTH = 6371.0

# IMD KKL MAX_Z 16-step dBZ colorbar, sampled from a full-resolution PNG
# frame's own legend (right-hand colorbar). Used to color the rain-table
# cells exactly like the radar image. Sorted high -> low.
DBZ_LUT: list[tuple[float, str]] = [
    (60.0, "#c80039"),
    (57.3, "#d30d07"),
    (54.7, "#ff3e1f"),
    (52.0, "#ff3a00"),
    (49.3, "#ff4000"),
    (46.7, "#ff7d00"),
    (44.0, "#ffb600"),
    (41.3, "#ffde00"),
    (38.7, "#fff6a6"),
    (36.0, "#c3efff"),
    (33.3, "#40c1ff"),
    (30.7, "#1499ff"),
    (28.0, "#006dff"),
    (25.3, "#002ef8"),
    (22.7, "#0301c7"),
    (20.0, "#3600a2"),
]


def dbz_to_color(max_dbz: float | None) -> str | None:
    """Nearest radar-legend color for a dBZ value; None when no echo."""
    if max_dbz is None:
        return None
    return min(DBZ_LUT, key=lambda kv: abs(kv[0] - max_dbz))[1]

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS places (
  place_id    INTEGER PRIMARY KEY,
  name_en     TEXT NOT NULL,
  name_ta     TEXT DEFAULT '',
  district    TEXT NOT NULL,
  place_type  TEXT,
  lat         REAL NOT NULL,
  lon         REAL NOT NULL,
  coord_src   TEXT NOT NULL DEFAULT 'master',
  osm_id      INTEGER,
  UNIQUE(name_en, district)
);
CREATE TABLE IF NOT EXISTS frames (
  frame_id      INTEGER PRIMARY KEY,
  frame_utc     TEXT NOT NULL UNIQUE,
  frame_ist     TEXT NOT NULL,
  src_path      TEXT NOT NULL,
  blob_sha      TEXT,
  radar_lat     REAL NOT NULL,
  radar_lon     REAL NOT NULL,
  center_px_x   REAL NOT NULL,
  center_px_y   REAL NOT NULL,
  km_per_px     REAL NOT NULL,
  calib_method  TEXT NOT NULL DEFAULT 'ring-labels',
  echo_pixels   INTEGER,
  max_dbz_frame REAL,
  ingested_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_frames_utc ON frames(frame_utc);
CREATE TABLE IF NOT EXISTS readings (
  reading_id      INTEGER PRIMARY KEY,
  frame_id        INTEGER NOT NULL REFERENCES frames(frame_id),
  place_id        INTEGER NOT NULL REFERENCES places(place_id),
  dist_km         REAL NOT NULL,
  max_dbz         REAL,
  cover_pct       REAL NOT NULL,
  nearest_echo_km REAL NOT NULL,
  category        TEXT NOT NULL,
  window_px       INTEGER NOT NULL DEFAULT 5,
  UNIQUE(frame_id, place_id)
);
CREATE INDEX IF NOT EXISTS idx_readings_place ON readings(place_id);
CREATE INDEX IF NOT EXISTS idx_readings_frame ON readings(frame_id);
CREATE TABLE IF NOT EXISTS hourly_rain (
  hour_id       INTEGER PRIMARY KEY,
  place_id      INTEGER NOT NULL REFERENCES places(place_id),
  date_ist      TEXT NOT NULL,
  hour_ist      INTEGER NOT NULL CHECK (hour_ist BETWEEN 0 AND 23),
  max_dbz       REAL,
  max_cover_pct REAL,
  n_frames      INTEGER NOT NULL,
  category      TEXT NOT NULL,
  updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE(place_id, date_ist, hour_ist)
);
CREATE INDEX IF NOT EXISTS idx_hourly_place_day ON hourly_rain(place_id, date_ist);
CREATE VIEW IF NOT EXISTS place_timeseries AS
  SELECT p.name_en, p.district, f.frame_utc, f.frame_ist,
         r.max_dbz, r.cover_pct, r.nearest_echo_km, r.category
  FROM readings r JOIN frames f ON f.frame_id = r.frame_id
                  JOIN places p ON p.place_id = r.place_id;
CREATE VIEW IF NOT EXISTS latest_per_place AS
  SELECT p.name_en, p.district, f.frame_ist, r.max_dbz, r.category
  FROM readings r JOIN frames f ON f.frame_id = r.frame_id
                  JOIN places p ON p.place_id = r.place_id
  WHERE f.frame_utc = (SELECT MAX(frame_utc) FROM frames);
CREATE VIEW IF NOT EXISTS hourly_timeseries AS
  SELECT p.name_en, p.district, h.date_ist, h.hour_ist,
         h.max_dbz, h.max_cover_pct, h.n_frames, h.category
  FROM hourly_rain h JOIN places p ON p.place_id = h.place_id;
"""


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db, timeout=30)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def migrate(con: sqlite3.Connection) -> None:
    """Additive migrations for DBs created by older script versions."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(frames)")]
    if "calib_method" not in cols:
        con.execute("ALTER TABLE frames ADD COLUMN "
                    "calib_method TEXT NOT NULL DEFAULT 'ring-labels'")
    # hourly_rain was added after readings: create it on old DBs.
    for stmt in SCHEMA.split(";"):
        if "CREATE TABLE IF NOT EXISTS hourly_rain" in stmt:
            con.execute(stmt)
        if "CREATE INDEX IF NOT EXISTS idx_hourly_place_day" in stmt:
            con.execute(stmt)
    # Views are derived state: rebuild so old DBs pick up column changes.
    views = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='view'")]
    if "place_timeseries" in views:
        con.execute("DROP VIEW place_timeseries")
    if "latest_per_place" in views:
        con.execute("DROP VIEW latest_per_place")
    if "hourly_timeseries" in views:
        con.execute("DROP VIEW hourly_timeseries")
    for stmt in SCHEMA.split(";"):
        if "CREATE VIEW" in stmt:
            con.execute(stmt)
    con.commit()


def init_db(db: Path) -> None:
    con = connect(db)
    con.executescript(SCHEMA)
    migrate(con)
    places = json.loads(MASTER_JSON.read_text())
    # Support both the canonical list format and (transitional) the legacy
    # dict format keyed by "Name, X District, ..." so an old checkout still
    # seeds instead of crashing.
    if isinstance(places, dict):
        tamil: dict[str, str] = {}
        try:
            tamil = {r["place"]: r.get("name_ta", "") for r in
                     json.loads((ROOT / "samples" / "kkl-jaffna" /
                                 "master_places_dbz.json").read_text())}
        except (OSError, ValueError):
            pass
        items = []
        for key, v in places.items():
            parts = key.split(",")
            name = parts[0].strip()
            if name.endswith("_dup"):
                name = name[:-4]
            district = parts[1].strip().replace(" District", "") if len(parts) > 1 else ""
            items.append({"name_en": name, "district": district,
                          "lat": v["lat"], "lon": v["lon"],
                          "name_ta": tamil.get(name, ""),
                          "place_type": (v.get("place") or "").split("/")[-1]})
        places = items
    seen: set[str] = set()
    n = 0
    for p in places:
        name, district = p["name_en"], p["district"]
        if (name, district) in seen:
            continue
        seen.add((name, district))
        # Upsert (not INSERT OR IGNORE): coordinate/name fixes in
        # data/places.json must propagate to cached DBs (CI restores
        # data/kkl.db via actions/cache), while place_id stays stable so
        # existing readings keep their join.
        con.execute(
            "INSERT INTO places "
            "(name_en, name_ta, district, place_type, lat, lon, coord_src) "
            "VALUES (?,?,?,?,?,?, 'master') "
            "ON CONFLICT(name_en, district) DO UPDATE SET "
            "name_ta=excluded.name_ta, place_type=excluded.place_type, "
            "lat=excluded.lat, lon=excluded.lon",
            (name, p.get("name_ta", ""), district, p.get("place_type", ""),
             p["lat"], p["lon"]))
        n += 1
    con.commit()
    print(f"init {db}: schema ready, {n} places")


def latlon_to_km(lat, lon, lat0, lon0) -> tuple[float, float]:
    la0, lo0, la, lo = map(math.radians, (lat0, lon0, lat, lon))
    dlo = lo - lo0
    cosc = math.sin(la0) * math.sin(la) + math.cos(la0) * math.cos(la) * math.cos(dlo)
    c = math.acos(min(1.0, max(-1.0, cosc)))
    k = 1.0 if c == 0 else c / math.sin(c)
    return (k * math.cos(la) * math.sin(dlo) * R_EARTH,
            k * (math.cos(la0) * math.sin(la)
                 - math.sin(la0) * math.cos(la) * math.cos(dlo)) * R_EARTH)


# Verified 2026-09-14 sample geometry; used only when a frame's ring labels
# are obscured AND the DB has no prior frames to borrow from.
NOMINAL_CX, NOMINAL_CY, NOMINAL_KM_PER_PX = 259.70, 259.38, 0.9614


def calibrate(im: Image.Image, fallback: tuple[float, float, float] | None = None):
    """Fit PPI center + km/px from the six orange 200km labels.

    Returns (crop, cx, cy, km_per_px, method). Heavy echoes can bury a label;
    then reuse caller-supplied fallback geometry instead of failing the run.
    """
    import numpy as np
    W, H = im.size
    x0, y0 = 0, int(round(H * 200 / 720))
    x1, y1 = int(round(W * 519 / 880)), int(round(H * 719 / 720))
    A = np.array(im.crop((x0, y0, x1, y1)).convert("RGB")).astype(np.int16)
    R, G, B = A[:, :, 0], A[:, :, 1], A[:, :, 2]
    ys, xs = np.where((R > 200) & (G > 100) & (G < 200) & (B < 100))
    clusters: list[list[tuple[int, int]]] = []
    for x, y in sorted(zip(xs, ys)):
        for c in clusters:
            mx = sum(p[0] for p in c) / len(c)
            my = sum(p[1] for p in c) / len(c)
            if abs(x - mx) < 30 and abs(y - my) < 20:
                c.append((x, y))
                break
        else:
            clusters.append([(x, y)])
    centers = [(sum(p[0] for p in c) / len(c), sum(p[1] for p in c) / len(c))
               for c in clusters if len(c) >= 50]
    if len(centers) == 6:
        cx = sum(c[0] for c in centers) / len(centers)
        cy = sum(c[1] for c in centers) / len(centers)
        r200 = sum(math.hypot(x - cx, y - cy) for x, y in centers) / len(centers)
        return (x0, y0, x1, y1), cx, cy, 200.0 / r200, "ring-labels"
    if fallback is None:
        raise ValueError(f"only {len(centers)} of 6 '200' labels visible "
                         "and no fallback geometry available")
    print(f"warn: only {len(centers)}/6 ring labels visible, "
          f"reusing fallback geometry {fallback}")
    return (x0, y0, x1, y1), *fallback, "ring-labels-fallback"


def recent_geometry(con: sqlite3.Connection,
                    ) -> tuple[float, float, float] | None:
    """Median (cx, cy, km/px) of the last 10 label-calibrated frames."""
    rows = con.execute(
        "SELECT center_px_x, center_px_y, km_per_px FROM frames "
        "WHERE calib_method = 'ring-labels' "
        "ORDER BY frame_utc DESC LIMIT 10").fetchall()
    if not rows:
        return None
    import statistics
    return (statistics.median(r[0] for r in rows),
            statistics.median(r[1] for r in rows),
            statistics.median(r[2] for r in rows))


def segment(im: Image.Image, cx: float, cy: float, km_per_px: float):
    """Extract echo overlay (RGBA) + per-pixel dBZ using the frame's legend."""
    import numpy as np
    W, H = im.size
    cbx = int(round(W * 790 / 880))
    band_ys = [379, 394, 409, 424, 439, 454, 469, 484, 497, 509,
               524, 539, 554, 569, 584, 599]
    dbzs = [60.0, 57.3, 54.7, 52.0, 49.3, 46.7, 44.0, 41.3,
            38.7, 36.0, 33.3, 30.7, 28.0, 25.3, 22.7, 20.0]
    full = np.array(im.convert("RGB")).astype(int)
    LUT = []
    for yref, dbz in zip(band_ys, dbzs):
        y = int(round(H * yref / 720))
        patch = full[max(0, y - 2):y + 3, cbx - 3:cbx + 4, :]
        LUT.append((dbz, tuple(int(v) for v in patch.reshape(-1, 3).mean(axis=0))))
    LUT_RGB = np.array([c for _, c in LUT])

    x0, y0 = 0, int(round(H * 200 / 720))
    x1, y1 = int(round(W * 519 / 880)), int(round(H * 719 / 720))
    A = np.array(im.crop((x0, y0, x1, y1)).convert("RGB")).astype(np.int16)
    d2 = ((A[:, :, None, :] - LUT_RGB[None, None, :, :]) ** 2).sum(axis=3)
    best = d2.argmin(axis=2)
    echo = np.sqrt(d2.min(axis=2)) < 55.0
    ph, pw = echo.shape
    YY, XX = np.mgrid[0:ph, 0:pw]
    inside = np.sqrt((XX - cx) ** 2 + (YY - cy) ** 2) <= (250.0 / km_per_px + 2)
    echo &= inside
    R, G, B = A[:, :, 0], A[:, :, 1], A[:, :, 2]
    e = echo.astype(np.int8)
    pad = np.pad(e, 1)
    nb = (pad[:-2, :-2] + pad[:-2, 1:-1] + pad[:-2, 2:] + pad[1:-1, :-2]
          + pad[1:-1, 2:] + pad[2:, :-2] + pad[2:, 1:-1] + pad[2:, 2:])
    echo |= ((R < 30) & (G < 30) & (B < 30)) & (nb >= 5) & inside

    dbz_arr = np.array([d for d, _ in LUT])[best]
    return echo, dbz_arr, {tuple(map(int, c)): d for d, c in LUT}, (x0, y0, x1, y1)


def category_of(max_dbz, in_range: bool) -> str:
    if max_dbz is None:
        return "out of range" if not in_range else "no echo (<20 dBZ)"
    if max_dbz >= 50:
        return "very heavy (>50 dBZ)"
    if max_dbz >= 40:
        return "heavy (40-50 dBZ)"
    if max_dbz >= 30:
        return "moderate (30-40 dBZ)"
    return "light (20-30 dBZ)"


def ingest(db: Path, src: Path, frame_utc: str, frame_ist: str,
           window_px: int = 5) -> int:
    import numpy as np
    con = connect(db)
    migrate(con)
    if con.execute("SELECT 1 FROM frames WHERE frame_utc = ?",
                   (frame_utc,)).fetchone():
        print(f"frame {frame_utc} already ingested, skipping")
        con.commit()
        con.close()
        return 0
    src_path = Path(src)
    if not src_path.is_absolute():
        src_path = ROOT / src_path
    im = Image.open(src_path).convert("RGB")
    fb = recent_geometry(con) or (NOMINAL_CX, NOMINAL_CY, NOMINAL_KM_PER_PX)
    (x0, y0, x1, y1), cx, cy, km_per_px, method = calibrate(im, fallback=fb)
    echo, dbz_arr, lut, _ = segment(im, cx, cy, km_per_px)
    ph, pw = echo.shape
    R250 = 250.0 / km_per_px

    # lat/lon grids (azimuthal equidistant about radar)
    YY, XX = np.mgrid[0:ph, 0:pw]
    dx_km = (XX - cx) * km_per_px
    dy_km = (cy - YY) * km_per_px
    rho = np.sqrt(dx_km ** 2 + dy_km ** 2)
    c = rho / R_EARTH
    la0 = math.radians(RADAR_LAT)
    LAT = np.degrees(np.arcsin(np.cos(c) * math.sin(la0)
                               + np.where(rho == 0, 0,
                                          dy_km * np.sin(c) * math.cos(la0) / rho)))
    LON = np.degrees(math.radians(RADAR_LON) + np.arctan2(
        dx_km * np.sin(c),
        rho * math.cos(la0) * np.cos(c) - dy_km * math.sin(la0) * np.sin(c)))
    eys, exs = np.where(echo)

    cur = con.execute(
        "INSERT OR IGNORE INTO frames (frame_utc, frame_ist, src_path, radar_lat, "
        "radar_lon, center_px_x, center_px_y, km_per_px, calib_method, "
        "echo_pixels, max_dbz_frame) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (frame_utc, frame_ist, str(src), RADAR_LAT, RADAR_LON, cx, cy, km_per_px,
         method, int(echo.sum()),
         float(dbz_arr[echo].max()) if echo.any() else None))
    if cur.rowcount == 0:
        print(f"frame {frame_utc} already ingested, skipping")
        con.commit()
        con.close()
        return 0
    frame_id = cur.lastrowid

    h = window_px // 2
    n = 0
    for pid, name, lat, lon in con.execute("SELECT place_id, name_en, lat, lon FROM places"):
        ek, nk = latlon_to_km(lat, lon, RADAR_LAT, RADAR_LON)
        dist_km = math.hypot(ek, nk)
        px, py = cx + ek / km_per_px, cy - nk / km_per_px
        in_range = math.hypot(px - cx, py - cy) <= R250 + 2
        in_panel = 0 <= int(round(px)) < pw and 0 <= int(round(py)) < ph
        max_dbz, cover = None, 0.0
        if in_range and in_panel:
            ix, iy = int(round(px)), int(round(py))
            max_dbz, cover = ov_win(echo, dbz_arr, ix, iy, h)
        drow = np.hypot((LAT[eys, exs] - lat) * 111.0,
                        (LON[eys, exs] - lon) * 111.0 * math.cos(math.radians(lat)))
        # Fully echo-free frames have no echo pixels: nearest echo undefined.
        nearest = round(float(drow.min()), 1) if drow.size else 999.9
        con.execute(
            "INSERT INTO readings (frame_id, place_id, dist_km, max_dbz, cover_pct, "
            "nearest_echo_km, category, window_px) VALUES (?,?,?,?,?,?,?,?)",
            (frame_id, pid, round(dist_km, 1), max_dbz, round(cover * 100, 1),
             nearest, category_of(max_dbz, in_range), window_px))
        n += 1
    con.commit()
    con.close()
    print(f"ingested {frame_utc}: {n} readings "
          f"(echo px={int(echo.sum())}, km/px={km_per_px:.4f})")
    return n


def ov_win(echo, dbz_arr, ix: int, iy: int, h: int):
    m = echo[max(0, iy - h):iy + h + 1, max(0, ix - h):ix + h + 1]
    cover = float(m.mean())
    if not m.any():
        return None, cover
    return float(dbz_arr[max(0, iy - h):iy + h + 1,
                         max(0, ix - h):ix + h + 1][m].max()), cover


def last_snapshots() -> list[tuple[datetime.datetime, Path, str, str]]:
    """Every day-dir *_last.png with (ist_dt, path, frame_utc, frame_ist).

    PNG only: legacy *_last.jpg snapshots predate the full-resolution
    pipeline and are skipped. Timestamps come from the day dir + filename
    pair that fetch_kkl.py already validated (UTC vs IST agree within
    2 min); frame_utc is derived as IST-5:30.
    """
    out = []
    for day in sorted(ARCHIVE_DIR.glob("????-??-??")):
        try:
            day_d = datetime.date.fromisoformat(day.name)
        except ValueError:
            continue
        for p in sorted(day.glob("*_last.png")):
            m = LAST_RE.search(p.name)
            if not m:
                continue
            utc_hm, ist_hm = m.group(1), m.group(2)
            ist_dt = datetime.datetime.combine(
                day_d, datetime.time(int(ist_hm[:2]), int(ist_hm[2:])))
            utc_dt = ist_dt - datetime.timedelta(hours=5, minutes=30)
            if utc_dt.strftime("%H%M") != utc_hm:
                print(f"warn: {p}: filename UTC {utc_hm} != IST-5:30 "
                      f"({utc_dt:%H%M}), trusting IST")
            out.append((ist_dt, p,
                        utc_dt.strftime("%Y-%m-%d %H:%M:%SZ"),
                        ist_dt.strftime("%Y-%m-%d %H:%M IST")))
    out.sort()
    return out


def png_frame_sources() -> list[tuple[datetime.datetime, Path, str, str]]:
    """Every full-resolution PNG source: *_last.png + frames/*.png.

    Frame timestamps come from each day's frames.json manifest (which
    fetch_kkl.py wrote from the OCR clock overlay); only entries whose
    file exists on disk and ends in .png are returned. Legacy .jpg frames
    (half-resolution 440x360) and *_last.jpg snapshots are skipped.
    Returns (sort_key, path, frame_utc, frame_ist) sorted chronologically.
    """
    out: list[tuple[datetime.datetime, Path, str, str]] = []
    for day in sorted(ARCHIVE_DIR.glob("????-??-??")):
        try:
            datetime.date.fromisoformat(day.name)
        except ValueError:
            continue
        mf = day / "frames.json"
        try:
            manifest = json.loads(mf.read_text())
        except (OSError, ValueError):
            manifest = {}
        for e in manifest.get("frames", []) if isinstance(manifest, dict) else []:
            if not isinstance(e, dict):
                continue
            img = e.get("img", "")
            if not isinstance(img, str) or not img.endswith(".png"):
                continue  # legacy low-res .jpg frames: skip
            src = day / img
            if not src.is_file():
                continue
            t_utc = e.get("t_utc", "")
            t_ist = e.get("t_ist", "")
            if not isinstance(t_utc, str) or not isinstance(t_ist, str):
                continue
            try:
                sort_key = datetime.datetime.strptime(
                    t_utc.strip(), "%Y-%m-%d %H:%M:%SZ")
            except ValueError:
                continue
            # Normalize IST label to minute precision ("YYYY-MM-DD HH:MM IST").
            m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})", t_ist)
            if not m:
                continue
            frame_ist = f"{m.group(1)} {int(m.group(2)):02d}:{m.group(3)} IST"
            out.append((sort_key, src, t_utc.strip(), frame_ist))
    # Snapshot PNGs carry minute-precision UTC; frame PNGs carry seconds, so
    # no key collision between the two sets.
    for ist_dt, p, frame_utc, frame_ist in last_snapshots():
        try:
            sort_key = datetime.datetime.strptime(frame_utc, "%Y-%m-%d %H:%M:%SZ")
        except ValueError:
            continue
        out.append((sort_key, p, frame_utc, frame_ist))
    out.sort(key=lambda r: (r[0], str(r[1])))
    return out


def backfill(db: Path, window_px: int = 5) -> int:
    """Ingest every archived PNG source; already-seen frames skip."""
    snaps = png_frame_sources()
    if not snaps:
        print("backfill: no PNG sources in docs/archive")
        return 0
    new = 0
    for _, src, frame_utc, frame_ist in snaps:
        try:
            rel = src.relative_to(ROOT)
        except ValueError:
            rel = src
        try:
            if ingest(db, rel, frame_utc, frame_ist, window_px) > 0:
                new += 1
        except Exception as e:
            print(f"backfill: FAILED {src.name} ({frame_utc}): {e}")
    print(f"backfill: {len(snaps)} PNG sources, {new} new frames ingested")
    rebuild_hourly(db)
    return new


def _resolve_src(src: str) -> tuple[Path, bool]:
    """Usable image path for a frame src; falls back to git history.

    Legacy `*_last.png` snapshots were deleted from HEAD (frames-only
    archive) but their blobs survive in history — recoverable via
    `git log --diff-filter=A`. Returns (path, is_temp); callers must
    unlink temp paths. Raises FileNotFoundError when unresolvable
    (shallow clone, git missing, path never committed).
    """
    import subprocess
    import tempfile
    p = Path(src)
    if not p.is_absolute():
        p = ROOT / p
    if p.is_file():
        return p, False
    rel = str(p.relative_to(ROOT)) if p.is_absolute() else src
    try:
        r = subprocess.run(
            ["git", "log", "--all", "--format=%H", "--diff-filter=A", "--", rel],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        revs = r.stdout.split()
        if r.returncode == 0 and revs:
            show = subprocess.run(
                ["git", "show", f"{revs[0]}:{rel}"],
                cwd=ROOT, capture_output=True, timeout=120)
            if show.returncode == 0 and show.stdout[:6] in (b"GIF87a", b"GIF89a",
                    b"\x89PNG\r\n"):
                tmp = tempfile.NamedTemporaryFile(
                    suffix=Path(rel).suffix or ".png", delete=False)
                tmp.write(show.stdout)
                tmp.close()
                return Path(tmp.name), True
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    raise FileNotFoundError(f"src gone and not in git history: {src}")


def fill_missing(db: Path, window_px: int = 5,
                 limit: int | None = None) -> int:
    """Insert readings for (frame, place) pairs missing from readings.

    Covers places added after their frames were ingested (and any other
    gap): old frames get proper readings for new places instead of
    rendering as implied-dry. Reuses each frame's stored center/km_per_px
    (no recalibration, so geometry matches the original ingest);
    echo-free frames skip image work entirely. Idempotent: pairs already
    present are never touched.
    """
    import numpy as np
    con = connect(db)
    migrate(con)
    q = ("SELECT f.frame_id FROM frames f WHERE EXISTS "
         "(SELECT 1 FROM places p LEFT JOIN readings r "
         "ON r.frame_id = f.frame_id AND r.place_id = p.place_id "
         "WHERE r.reading_id IS NULL) ORDER BY f.frame_utc")
    if limit is not None:
        q += f" LIMIT {int(limit)}"
    frame_ids = [r[0] for r in con.execute(q)]
    if not frame_ids:
        print("fill-missing: no gaps")
        con.close()
        return 0
    h = window_px // 2
    done = 0
    for fid in frame_ids:
        fr = con.execute(
            "SELECT frame_utc, src_path, center_px_x, center_px_y, km_per_px, "
            "echo_pixels FROM frames WHERE frame_id = ?", (fid,)).fetchone()
        miss = con.execute(
            "SELECT p.place_id, p.lat, p.lon FROM places p "
            "LEFT JOIN readings r ON r.frame_id = ? AND r.place_id = p.place_id "
            "WHERE r.reading_id IS NULL", (fid,)).fetchall()
        if not miss:
            continue
        frame_utc, src, cx, cy, km_per_px, echo_pixels = fr
        R250 = 250.0 / km_per_px
        if echo_pixels is not None and not echo_pixels:
            # Echo-free frame: dry rows without touching the image.
            for pid, lat, lon in miss:
                ek, nk = latlon_to_km(lat, lon, RADAR_LAT, RADAR_LON)
                dist_km = math.hypot(ek, nk)
                px, py = cx + ek / km_per_px, cy - nk / km_per_px
                in_range = math.hypot(px - cx, py - cy) <= R250 + 2
                con.execute(
                    "INSERT OR IGNORE INTO readings (frame_id, place_id, dist_km, "
                    "max_dbz, cover_pct, nearest_echo_km, category, window_px) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (fid, pid, round(dist_km, 1), None, 0.0, 999.9,
                     category_of(None, in_range), window_px))
                done += 1
            con.commit()
            continue
        src_path = Path(src)
        if not src_path.is_absolute():
            src_path = ROOT / src_path
        try:
            src_path, is_temp = _resolve_src(str(src_path))
        except FileNotFoundError:
            print(f"fill-missing: {frame_utc}: src gone ({src}), skipping")
            continue
        try:
            im = Image.open(src_path).convert("RGB")
            echo, dbz_arr, lut, _ = segment(im, cx, cy, km_per_px)
        finally:
            if is_temp:
                try:
                    src_path.unlink()
                except OSError:
                    pass
        ph, pw = echo.shape
        YY, XX = np.mgrid[0:ph, 0:pw]
        dx_km = (XX - cx) * km_per_px
        dy_km = (cy - YY) * km_per_px
        rho = np.sqrt(dx_km ** 2 + dy_km ** 2)
        ang = rho / R_EARTH
        la0 = math.radians(RADAR_LAT)
        LAT = np.degrees(np.arcsin(np.cos(ang) * math.sin(la0)
                                   + np.where(rho == 0, 0,
                                              dy_km * np.sin(ang) * math.cos(la0) / rho)))
        LON = np.degrees(math.radians(RADAR_LON) + np.arctan2(
            dx_km * np.sin(ang),
            rho * math.cos(la0) * np.cos(ang) - dy_km * math.sin(la0) * np.sin(ang)))
        eys, exs = np.where(echo)
        for pid, lat, lon in miss:
            ek, nk = latlon_to_km(lat, lon, RADAR_LAT, RADAR_LON)
            dist_km = math.hypot(ek, nk)
            px, py = cx + ek / km_per_px, cy - nk / km_per_px
            in_range = math.hypot(px - cx, py - cy) <= R250 + 2
            in_panel = 0 <= int(round(px)) < pw and 0 <= int(round(py)) < ph
            max_dbz, cover = None, 0.0
            if in_range and in_panel:
                max_dbz, cover = ov_win(echo, dbz_arr,
                                        int(round(px)), int(round(py)), h)
            drow = np.hypot((LAT[eys, exs] - lat) * 111.0,
                            (LON[eys, exs] - lon) * 111.0 * math.cos(math.radians(lat)))
            nearest = round(float(drow.min()), 1) if drow.size else 999.9
            con.execute(
                "INSERT OR IGNORE INTO readings (frame_id, place_id, dist_km, "
                "max_dbz, cover_pct, nearest_echo_km, category, window_px) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (fid, pid, round(dist_km, 1), max_dbz, round(cover * 100, 1),
                 nearest, category_of(max_dbz, in_range), window_px))
            done += 1
        con.commit()
    con.close()
    print(f"fill-missing: {done} readings filled across {len(frame_ids)} frames")
    return done


def rebuild_hourly(db: Path) -> int:
    """Rebuild hourly_rain from readings (idempotent full refresh).

    One row per (place, IST date, IST hour): the hour's max dBZ / cover
    over all frames in that hour, with the same category thresholds as
    per-frame readings. Hours with frames but no echo keep max_dbz NULL
    (dry); a place outside radar range stays 'out of range'.
    """
    con = connect(db)
    migrate(con)
    con.execute("DELETE FROM hourly_rain")
    con.execute("""
        INSERT INTO hourly_rain
            (place_id, date_ist, hour_ist, max_dbz, max_cover_pct,
             n_frames, category)
        SELECT r.place_id,
               substr(f.frame_ist, 1, 10) AS date_ist,
               CAST(substr(f.frame_ist, 12, 2) AS INTEGER) AS hour_ist,
               MAX(r.max_dbz),
               MAX(r.cover_pct),
               COUNT(*),
               CASE
                 WHEN MAX(r.max_dbz) IS NULL
                      AND SUM(CASE WHEN r.category = 'out of range'
                                   THEN 1 ELSE 0 END) = COUNT(*)
                   THEN 'out of range'
                 WHEN MAX(r.max_dbz) IS NULL THEN 'no echo (<20 dBZ)'
                 WHEN MAX(r.max_dbz) >= 50 THEN 'very heavy (>50 dBZ)'
                 WHEN MAX(r.max_dbz) >= 40 THEN 'heavy (40-50 dBZ)'
                 WHEN MAX(r.max_dbz) >= 30 THEN 'moderate (30-40 dBZ)'
                 ELSE 'light (20-30 dBZ)'
               END
        FROM readings r JOIN frames f ON f.frame_id = r.frame_id
        WHERE f.frame_ist GLOB '????-??-?? ??:?? IST'
        GROUP BY r.place_id, date_ist, hour_ist
    """)
    n = con.execute("SELECT COUNT(*) FROM hourly_rain").fetchone()[0]
    con.commit()
    con.close()
    print(f"hourly: {n} place-hour rows rebuilt")
    return n


def export(db: Path) -> None:
    """Diff-friendly CSVs the cron commits alongside the DB."""
    rebuild_hourly(db)
    con = connect(db)
    migrate(con)
    latest_p = ROOT / "data" / "latest.csv"
    with open(latest_p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name_en", "district", "frame_ist", "max_dbz", "category"])
        w.writerows(con.execute("SELECT * FROM latest_per_place"))
    ts_p = ROOT / "data" / "timeseries.csv"
    with open(ts_p, "w", newline="") as f:
        w = csv.writer(f)
        # Echo readings only: 90%+ of per-frame readings are dry, so dry
        # cells are implied, not stored. A (place, frame) pair with no row
        # here means no echo (<20 dBZ) at that place in that frame; join
        # frames.csv for the full frame list. frame_ist is omitted
        # (frame_utc + 5:30).
        w.writerow(["name_en", "district", "frame_utc",
                    "max_dbz", "cover_pct", "nearest_echo_km", "category"])
        w.writerows(con.execute(
            "SELECT name_en, district, frame_utc, max_dbz, cover_pct, "
            "nearest_echo_km, category FROM place_timeseries "
            "WHERE max_dbz IS NOT NULL ORDER BY name_en, frame_utc"))
    fr_p = ROOT / "data" / "frames.csv"
    with open(fr_p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame_utc", "frame_ist", "echo_pixels",
                    "max_dbz_frame", "n_echo_places"])
        w.writerows(con.execute(
            "SELECT f.frame_utc, f.frame_ist, f.echo_pixels, "
            "f.max_dbz_frame, COUNT(r.reading_id) FROM frames f "
            "LEFT JOIN readings r ON r.frame_id = f.frame_id "
            "AND r.max_dbz IS NOT NULL "
            "GROUP BY f.frame_id ORDER BY f.frame_utc"))
    print(f"export: {latest_p} + {ts_p} + {fr_p}")
    con.close()
    export_rain_json(db)


def export_rain_json(db: Path,
                     out: Path | None = None) -> Path:
    """Hourly rain table for the website (docs/data/rain.json).

    Rows = every master place, columns = IST hours 0-23. Each cell holds
    the hour's max dBZ for that place (max over all PNG frames in the
    hour) plus the radar-legend color for the cell background. Hours with
    no PNG frame that day are null (no data); hours with frames but no
    echo hold {"dbz": null, ...} (dry).

    Frame-grain spectrum (for 4H/1H/30M/15M modes): each day also carries
    `frames` (one entry per PNG frame, IST minute-of-day for bucketing)
    and each row carries sparse `spec` ([frame_idx, dbz] echo-only pairs;
    a frame missing from `spec` means dry at that place). The frontend
    buckets frames client-side and paints each bucket cell as a
    time-proportional gradient (wet = LUT color, dry = white) instead of
    a single max/average.
    """
    if out is None:
        out = ROOT / "docs" / "data" / "rain.json"
    con = connect(db)
    migrate(con)
    places = [
        {"name_en": r[0], "district": r[1], "lat": r[2], "lon": r[3]}
        for r in con.execute(
            "SELECT name_en, district, lat, lon FROM places "
            "ORDER BY district, name_en")]
    rows = con.execute(
        "SELECT p.name_en, p.district, f.frame_utc, f.frame_ist, r.max_dbz "
        "FROM readings r JOIN frames f ON f.frame_id = r.frame_id "
        "JOIN places p ON p.place_id = r.place_id").fetchall()
    # Frame-grain axis: one entry per frame per IST day. frame_utc carries
    # seconds (ordering within a 15-min bucket); frame_ist is minute
    # precision ("YYYY-MM-DD HH:MM IST").
    frame_rows = con.execute(
        "SELECT frame_utc, frame_ist FROM frames ORDER BY frame_utc").fetchall()
    con.close()

    day_frames: dict[str, list[dict]] = {}
    frame_idx: dict[str, int] = {}  # frame_utc -> index within its day
    for frame_utc, frame_ist in frame_rows:
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})",
                      frame_ist or "")
        if not m:
            continue
        date = m.group(1)
        lst = day_frames.setdefault(date, [])
        try:
            utc_dt = datetime.datetime.strptime(
                (frame_utc or "").strip(), "%Y-%m-%d %H:%M:%SZ")
        except ValueError:
            utc_dt = None
        if utc_dt is not None:
            ist = utc_dt + datetime.timedelta(hours=5, minutes=30)
            minute = ist.hour * 60 + ist.minute + ist.second / 60.0
            short = f"{ist.hour:02d}:{ist.minute:02d}"
        else:
            minute = int(m.group(2)) * 60 + int(m.group(3))
            short = f"{int(m.group(2)):02d}:{m.group(3)}"
        frame_idx[(frame_utc or "").strip()] = len(lst)
        lst.append({"utc": (frame_utc or "").strip(),
                    "min": round(minute, 2), "t": short})

    # Sparse echo map: (date, place_key) -> {frame_idx: dbz}.
    # Keyed by exact frame_utc (frame grain, seconds precision) so two
    # frames sharing one IST minute label never misattribute.
    echo: dict[tuple[str, tuple[str, str]], dict[int, float]] = {}
    for name_en, district, frame_utc, frame_ist, max_dbz in rows:
        if max_dbz is None:
            continue  # dry is implied, not stored
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})",
                      frame_ist or "")
        if not m:
            continue
        date = m.group(1)
        if date not in day_frames:
            continue
        j = frame_idx.get((frame_utc or "").strip())
        if j is None:
            continue
        echo.setdefault((date, (name_en, district)), {})[j] = max_dbz

    per_day: dict[str, dict] = {}
    for name_en, district, frame_utc, frame_ist, max_dbz in rows:
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})", frame_ist or "")
        if not m:
            continue
        date, hour = m.group(1), int(m.group(2))
        day = per_day.setdefault(date, {})
        key = (name_en, district)
        cell = day.setdefault(key, {})
        prev = cell.get(hour)
        if prev is None or (max_dbz is not None
                             and (prev is None or max_dbz > prev)):
            cell[hour] = max_dbz

    days = []
    for date in sorted(per_day):
        hours_with_data = [False] * 24
        for cell in per_day[date].values():
            for h in cell:
                if 0 <= h <= 23:
                    hours_with_data[h] = True
        rain_rows = []
        for p in places:
            cell = per_day[date].get((p["name_en"], p["district"]), {})
            cells: list[dict | None] = []
            day_max = None
            for h in range(24):
                if not hours_with_data[h]:
                    cells.append(None)  # no PNG frame that hour
                    continue
                if h not in cell:
                    cells.append({"dbz": None, "color": None,
                                  "cat": "no echo (<20 dBZ)"})
                    continue
                v = cell[h]
                if v is not None and (day_max is None or v > day_max):
                    day_max = v
                cells.append({"dbz": v, "color": dbz_to_color(v),
                              "cat": category_of(v, True)})
            rain_rows.append({"place": p["name_en"], "district": p["district"],
                              "cells": cells, "max": day_max,
                              "spec": [[j, echo[(date, (p["name_en"],
                                                       p["district"]))][j]]
                                       for j in sorted(
                                           echo.get((date, (p["name_en"],
                                                             p["district"])),
                                                    {}))]})
        rain_rows.sort(key=lambda r: ((r["max"] is None), -(r["max"] or 0),
                                      r["district"], r["place"]))
        days.append({"date": date, "hours_with_data": hours_with_data,
                     "n_frames_hours": sum(hours_with_data),
                     "frames": day_frames.get(date, []),
                     "rows": rain_rows})
    days.sort(key=lambda d: d["date"], reverse=True)

    payload = {
        "radar": "KKL_MAXZ (Karaikal)",
        "source": "https://mausam.imd.gov.in/Radar/animation/Converted/KKL_MAXZ.gif",
        "updated_ist": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        ).strftime("%Y-%m-%d %H:%M IST"),
        "hours": list(range(24)),
        "resolutions": [240, 60, 30, 15],
        "lut": [{"dbz": dbz, "color": color} for dbz, color in DBZ_LUT],
        "places": places,
        "days": days,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1))
    print(f"export: {out} ({len(days)} day(s), {len(places)} places)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("ingest")
    p.add_argument("--src", type=Path, required=True)
    p.add_argument("--frame-utc", required=True)
    p.add_argument("--frame-ist", required=True)
    p.add_argument("--window", type=int, default=5)
    p = sub.add_parser("backfill")
    p.add_argument("--window", type=int, default=5)
    p = sub.add_parser("fill-missing")
    p.add_argument("--window", type=int, default=5)
    p.add_argument("--limit", type=int, default=None)
    sub.add_parser("hourly")
    sub.add_parser("export")
    sub.add_parser("rainjson")
    p = sub.add_parser("latest")
    p = sub.add_parser("series")
    p.add_argument("--place", required=True)
    args = ap.parse_args()

    if args.cmd == "init":
        init_db(args.db)
    elif args.cmd == "ingest":
        ingest(args.db, args.src, args.frame_utc, args.frame_ist, args.window)
    elif args.cmd == "backfill":
        backfill(args.db, args.window)
    elif args.cmd == "fill-missing":
        fill_missing(args.db, args.window, args.limit)
    elif args.cmd == "hourly":
        rebuild_hourly(args.db)
    elif args.cmd == "export":
        export(args.db)
    elif args.cmd == "rainjson":
        export_rain_json(args.db)
    elif args.cmd == "latest":
        con = connect(args.db)
        for r in con.execute("SELECT * FROM latest_per_place"):
            print(r)
    elif args.cmd == "series":
        con = connect(args.db)
        for r in con.execute(
                "SELECT frame_utc, frame_ist, max_dbz, cover_pct, nearest_echo_km, category"
                " FROM place_timeseries WHERE name_en=? ORDER BY frame_utc",
                (args.place,)):
            print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
