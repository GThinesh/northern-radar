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
import re
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image

from rain_finder import (
    CATEGORIES,
    DBZ_LUT,
    DRY_LABEL,
    NOMINAL_CX,
    NOMINAL_CY,
    NOMINAL_KM_PER_PX,
    OUT_OF_RANGE_LABEL,
    RADAR_LAT,
    RADAR_LON,
    FrameGeom,
    RainFinder,
    calibrate,
    category_of,
    dbz_to_color,
)

IST = ZoneInfo("Asia/Kolkata")

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
IST_FRAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})")


def category_case(col: str = "MAX(r.max_dbz)") -> str:
    """SQL CASE mirroring category_of(), for rebuild_hourly()."""
    whens = "\n".join(
        f"                  WHEN {col} >= {th:g} THEN '{label}'"
        for th, label in CATEGORIES[:-1])
    else_label = CATEGORIES[-1][1]
    return (f"CASE\n"
            f"                  WHEN {col} IS NULL\n"
            f"                       AND SUM(CASE WHEN r.category = '{OUT_OF_RANGE_LABEL}'\n"
            f"                                    THEN 1 ELSE 0 END) = COUNT(*)\n"
            f"                    THEN '{OUT_OF_RANGE_LABEL}'\n"
            f"                  WHEN {col} IS NULL THEN '{DRY_LABEL}'\n"
            f"{whens}\n"
            f"                  ELSE '{else_label}'\n"
            f"                END")


def parse_frame_ist(frame_ist: str | None) -> tuple[str, int] | None:
    """(date, hour) from an IST label; None when unparseable."""
    m = IST_FRAME_RE.search(frame_ist or "")
    if not m:
        return None
    return m.group(1), int(m.group(2))


def ist_label(date: str, hour: int, minute: int) -> str:
    return f"{date} {hour:02d}:{minute:02d} IST"


def utc_str_from_ist(ist_dt: datetime.datetime) -> str:
    """UTC label for a naive IST datetime, via ZoneInfo, not arithmetic."""
    aware = ist_dt.replace(tzinfo=IST)
    return aware.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

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
    # Every object is CREATE ... IF NOT EXISTS, so a full executescript is
    # both the installer and the migrator. Views are derived state: drop
    # them first so old DBs pick up column changes.
    for v in ("place_timeseries", "latest_per_place", "hourly_timeseries"):
        con.execute(f"DROP VIEW IF EXISTS {v}")
    con.executescript(SCHEMA)
    con.commit()


def init_db(db: Path) -> None:
    con = connect(db)
    con.executescript(SCHEMA)
    migrate(con)
    places = json.loads(MASTER_JSON.read_text())
    if isinstance(places, dict):
        raise ValueError("data/places.json must be a list of places")
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


def insert_reading(con: sqlite3.Connection, frame_id: int, place_id: int,
                   dist_km: float, max_dbz, cover_pct: float,
                   nearest: float, category: str, window_px: int) -> None:
    con.execute(
        "INSERT OR IGNORE INTO readings (frame_id, place_id, dist_km, "
        "max_dbz, cover_pct, nearest_echo_km, category, window_px) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (frame_id, place_id, dist_km, max_dbz, cover_pct,
         nearest, category, window_px))


def ingest(db: Path, src: Path, frame_utc: str, frame_ist: str,
           window_px: int = 5) -> int:
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
    finder = RainFinder.from_image(
        im, calibrate(im, fallback=fb), window_px=window_px)
    geom = finder.geom

    cur = con.execute(
        "INSERT OR IGNORE INTO frames (frame_utc, frame_ist, src_path, radar_lat, "
        "radar_lon, center_px_x, center_px_y, km_per_px, calib_method, "
        "echo_pixels, max_dbz_frame) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (frame_utc, frame_ist, str(src), RADAR_LAT, RADAR_LON,
         geom.cx, geom.cy, geom.km_per_px, geom.method, finder.echo_pixels,
         finder.max_dbz_frame))
    if cur.rowcount == 0:
        print(f"frame {frame_utc} already ingested, skipping")
        con.commit()
        con.close()
        return 0
    frame_id = cur.lastrowid

    n = 0
    for pid, name, lat, lon in con.execute("SELECT place_id, name_en, lat, lon FROM places"):
        insert_reading(con, frame_id, pid,
                       *finder.reading_for(lat, lon), window_px)
        n += 1
    con.commit()
    con.close()
    print(f"ingested {frame_utc}: {n} readings "
          f"(echo px={finder.echo_pixels}, km/px={geom.km_per_px:.4f})")
    return n


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
            frame_utc = utc_str_from_ist(ist_dt)
            utc_dt = datetime.datetime.strptime(frame_utc, "%Y-%m-%d %H:%M:%SZ")
            if utc_dt.strftime("%H%M") != utc_hm:
                print(f"warn: {p}: filename UTC {utc_hm} != IST-5:30 "
                      f"({utc_dt:%H%M}), trusting IST")
            out.append((ist_dt, p, frame_utc,
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
            parsed = parse_frame_ist(t_ist)
            if not parsed:
                continue
            date, hour = parsed
            m = IST_FRAME_RE.search(t_ist)
            assert m is not None
            frame_ist = ist_label(date, hour, int(m.group(3)))
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
        geom = FrameGeom(0, 0, 0, 0, cx, cy, km_per_px, "stored")
        if echo_pixels is not None and not echo_pixels:
            # Echo-free frame: dry rows without touching the image.
            finder = RainFinder.echo_free(geom, window_px)
            for pid, lat, lon in miss:
                insert_reading(con, fid, pid,
                               *finder.reading_for(lat, lon), window_px)
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
            finder = RainFinder.from_image(im, geom, window_px)
        finally:
            if is_temp:
                try:
                    src_path.unlink()
                except OSError:
                    pass
        for pid, lat, lon in miss:
            insert_reading(con, fid, pid,
                           *finder.reading_for(lat, lon), window_px)
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
    con.execute(f"""
        INSERT INTO hourly_rain
            (place_id, date_ist, hour_ist, max_dbz, max_cover_pct,
             n_frames, category)
        SELECT r.place_id,
               substr(f.frame_ist, 1, 10) AS date_ist,
               CAST(substr(f.frame_ist, 12, 2) AS INTEGER) AS hour_ist,
               MAX(r.max_dbz),
               MAX(r.cover_pct),
               COUNT(*),
               {category_case()}
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
        parsed = parse_frame_ist(frame_ist)
        if not parsed:
            continue
        date, hour = parsed
        m = IST_FRAME_RE.search(frame_ist or "")
        assert m is not None
        lst = day_frames.setdefault(date, [])
        try:
            utc_dt = datetime.datetime.strptime(
                (frame_utc or "").strip(), "%Y-%m-%d %H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc)
        except ValueError:
            utc_dt = None
        if utc_dt is not None:
            ist = utc_dt.astimezone(IST)
            minute = ist.hour * 60 + ist.minute + ist.second / 60.0
            short = f"{ist.hour:02d}:{ist.minute:02d}"
        else:
            minute = hour * 60 + int(m.group(3))
            short = f"{hour:02d}:{m.group(3)}"
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
        parsed = parse_frame_ist(frame_ist)
        if not parsed:
            continue
        date, _ = parsed
        if date not in day_frames:
            continue
        j = frame_idx.get((frame_utc or "").strip())
        if j is None:
            continue
        echo.setdefault((date, (name_en, district)), {})[j] = max_dbz

    per_day: dict[str, dict] = {}
    for name_en, district, frame_utc, frame_ist, max_dbz in rows:
        parsed = parse_frame_ist(frame_ist)
        if not parsed:
            continue
        date, hour = parsed
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
                                  "cat": DRY_LABEL})
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
        "updated_ist": datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
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
