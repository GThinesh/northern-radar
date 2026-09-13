#!/usr/bin/env python3
"""KKL (Karaikal) IMD radar archiver.

Downloads https://mausam.imd.gov.in/Radar/animation/Converted/KKL_MAXZ.gif
(full ~3h animated GIF), extracts unique frames to frames/HHMMSS_NNN.jpg,
rebuilds per-day summary (daily.gif + strip.jpg), updates docs/data/index.json.

Raw snapshots are NOT kept: the downloaded GIF is decoded in memory and
only per-snapshot _last.jpg + extracted frames are committed.

Layout (all under docs/ so GitHub Pages can serve it):
  docs/archive/YYYY-MM-DD/HHMM-UTC_HHMM-IST_last.jpg
  docs/archive/YYYY-MM-DD/frames/HHMMSS_NNN.jpg
  docs/archive/YYYY-MM-DD/daily.gif
  docs/archive/YYYY-MM-DD/strip.jpg
  docs/data/index.json

Usage:
  python scripts/fetch_kkl.py
  python scripts/fetch_kkl.py --test   # dry run, no index rewrite, saves to /tmp/opencode
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image

URL = "https://mausam.imd.gov.in/Radar/animation/Converted/KKL_MAXZ.gif"
IST = ZoneInfo("Asia/Kolkata")

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
ARCHIVE = DOCS / "archive"
DATA_JSON = DOCS / "data" / "index.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (KKL-radar-archiver; GitHub Actions) AppleWebKit/537.36",
    "Accept": "image/gif,image/*,*/*",
    "Referer": "https://mausam.imd.gov.in/",
}


def now_ist() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).astimezone(IST)


def download(retries: int = 3) -> bytes:
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(URL, headers=HEADERS, timeout=60)
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "")
            if "gif" not in ct.lower() and not r.content[:6] in (b"GIF87a", b"GIF89a"):
                raise ValueError(f"Unexpected content type/body: {ct}")
            if len(r.content) < 50_000:
                raise ValueError(f"Suspiciously small GIF: {len(r.content)} bytes")
            if r.content[:6] not in (b"GIF87a", b"GIF89a"):
                raise ValueError("Body is not a GIF")
            return r.content
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"download attempt {i+1}/{retries} failed: {e}", file=sys.stderr)
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"download failed after {retries} tries: {last_err}")


def sha_short(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:12]


def extract_last_frame(gif_bytes: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(gif_bytes))
    try:
        n = getattr(im, "n_frames", 1)
        im.seek(n - 1)
    except EOFError:
        im.seek(0)
    return im.convert("RGB")


def frame_count(gif_bytes: bytes) -> int:
    """True decodable frame count (seeks each frame; n_frames can mislead)."""
    try:
        im = Image.open(io.BytesIO(gif_bytes))
        n = 0
        while True:
            try:
                im.seek(n)
                n += 1
            except EOFError:
                break
        return max(n, 1)
    except Exception:
        return 1


# Clock overlay region (fractions of W/H): big HH:MM:SSZ + date line.
# Auto-tightened at runtime (see clock_crop), so this is a generous box.
CLOCK_BOX = (0.685, 0.29, 1.00, 0.40)
# Dedup mask: clock + date + IST line (frames differing only by clock hash equal)
TS_BOX = (0.68, 0.10, 1.00, 0.50)

# Tolerant patterns for OCR text, e.g. "06:44:54Z 12 SEP 2026 UTC".
# (Tesseract often renders trailing Z as "2": "06:55:252".)
# TIME_Z_RE first: the UTC clock carries a Z suffix; the IST line below it
# ("12:14:54 IST") must never match as UTC (+5:30 error), so the fallback
# explicitly rejects IST-suffixed times.
TIME_Z_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})\s*[Zz2]\b")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})(?!\s*IST)")
DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Z]{3})\s+(\d{4})\b", re.IGNORECASE)


def masked_hash(rgb: Image.Image) -> str:
    """sha256 of frame with timestamp box blacked out (dedup key)."""
    w, h = rgb.size
    x0, y0, x1, y1 = (int(w * TS_BOX[0]), int(h * TS_BOX[1]),
                      int(w * TS_BOX[2]), int(h * TS_BOX[3]))
    small = rgb.copy()
    small.paste((0, 0, 0), (x0, y0, x1, y1))
    return hashlib.sha256(small.tobytes()).hexdigest()[:16]


def iter_unique_frames(gif_bytes: bytes) -> tuple[list[tuple[str, Image.Image]], int]:
    """Decode GIF, drop consecutive frames with identical masked hash.

    Returns (ordered [(hash, RGB), ...], decoded_attempts). Corrupt input
    -> ([], 0). decoded_attempts counts successfully decoded frames
    including in-GIF repeats, so callers can compute dupes exactly.
    """
    out: list[tuple[str, Image.Image]] = []
    try:
        im = Image.open(io.BytesIO(gif_bytes))
        n = getattr(im, "n_frames", 1)
    except Exception as e:  # noqa: BLE001
        print(f"decode failed: {e}", file=sys.stderr)
        return out, 0
    prev_h = None
    decoded = 0
    for i in range(n):
        try:
            im.seek(i)
            rgb = im.convert("RGB")
        except EOFError:
            break
        decoded += 1
        h = masked_hash(rgb)
        if h == prev_h:
            continue  # in-GIF filler repeat
        prev_h = h
        out.append((h, rgb))
    return out, decoded


def clock_crop(rgb: Image.Image) -> Image.Image:
    """Crop clock overlay, auto-tighten to text via bounding box, upscale."""
    from PIL import ImageOps
    w, h = rgb.size
    c = rgb.crop((int(w * CLOCK_BOX[0]), int(h * CLOCK_BOX[1]),
                  int(w * CLOCK_BOX[2]), int(h * CLOCK_BOX[3])))
    g = c.convert("L").point(lambda v: 255 if v > 140 else 0)
    box = ImageOps.invert(g).getbbox()
    if box:
        pad = 4
        x0, y0, x1, y1 = box
        g = g.crop((max(0, x0 - pad), max(0, y0 - pad),
                    min(g.width, x1 + pad), min(g.height, y1 + pad)))
    scale = max(1, 120 // max(1, g.height))
    return g.resize((g.width * scale, g.height * scale))


def ocr_text(img: Image.Image) -> str:
    """Tesseract read of prepared clock crop; tries PSM 4 then 6."""
    import os
    import pytesseract  # type: ignore
    pytesseract.pytesseract.tesseract_cmd = os.environ.get(
        "TESSERACT_CMD", "tesseract")
    last = ""
    for psm in (4, 6):
        try:
            last = " ".join(
                pytesseract.image_to_string(img, config=f"--psm {psm}").split())
        except Exception:  # noqa: BLE001
            continue
        if TIME_Z_RE.search(last.upper()) or TIME_RE.search(last):
            return last
    return last


def ocr_timestamp(rgb: Image.Image) -> str | None:
    """Read the clock overlay via Tesseract (free, local). None if unavailable."""
    try:
        txt = ocr_text(clock_crop(rgb))
        return txt or None
    except ImportError:
        return None
    except Exception:  # noqa: BLE001
        return None


def parse_frame_time(txt: str, fallback_date: datetime.date,
                     ) -> tuple[datetime.datetime | None, bool]:
    """Extract (UTC datetime, date_is_fallback) from OCR text.

    Time-of-day must parse or returns (None, True). Date uses OCR when clean,
    else the snapshot's IST date (OCR day digits glitch; time is what matters
    for 30-min slots).
    """
    if not txt:
        return None, True
    t = txt.upper()
    m = TIME_Z_RE.search(t) or TIME_RE.search(t)
    if not m:
        return None, True
    hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if hh > 23 or mm > 59 or ss > 61:
        return None, True
    date, date_fb = fallback_date, True
    dm = DATE_RE.search(txt.upper())
    if dm:
        try:
            d = datetime.datetime.strptime(
                f"{dm.group(1)} {dm.group(2)} {dm.group(3)}", "%d %b %Y").date()
            if abs((d - fallback_date).days) <= 1 and 2020 <= d.year <= 2030:
                date, date_fb = d, False
        except ValueError:
            pass
    return datetime.datetime(date.year, date.month, date.day,
                             hh, mm, ss, tzinfo=datetime.timezone.utc), date_fb


def parse_timestamp_dt(txt: str) -> datetime.datetime | None:
    """Legacy wrapper: full datetime from text, date must parse."""
    dt, date_fb = parse_frame_time(
        txt, datetime.datetime.now(datetime.timezone.utc).date())
    return dt if dt and not date_fb else None


def parse_timestamp(txt: str) -> str | None:
    """'06:44:54Z 12 SEP 2026' -> '2026-09-12 06:44:54Z'. None if unparseable."""
    dt = parse_timestamp_dt(txt)
    return dt.strftime("%Y-%m-%d %H:%M:%SZ") if dt else None


# Typical IMD scan cadence vs snapshot download lag (fallback time estimates
# when OCR is unavailable: last unique frame ~ SNAP_LAG before download).
FRAME_STEP = datetime.timedelta(minutes=10)
SNAP_LAG = datetime.timedelta(minutes=15)

# Physical invariant for a radar frame clock read: a frame can never be from
# the future, and the IMD GIF holds ~3h of history. OCR day digits glitch, so
# a fallback-date mis-pick can shift a frame a full day forward (e.g. a
# 22:32Z frame read against the post-midnight snapshot date lands ~22h in the
# future, surfacing as 04:02 IST "tomorrow"). Clamp those back; old frames
# are left alone (a long GIF / delayed cron can legitimately lag hours).
FUTURE_TOL = datetime.timedelta(minutes=15)


def clamp_frame_utc(dt: datetime.datetime,
                    snap_utc: datetime.datetime) -> datetime.datetime:
    """Pull an OCR-parsed UTC time at or before snap_utc + FUTURE_TOL.

    Day-boundary fallback picks (and OCR day-digit glitches) shift by whole
    days, so correct with whole-day steps only; intra-day times are trusted.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    for _ in range(2):
        if dt > snap_utc + FUTURE_TOL:
            dt -= datetime.timedelta(days=1)
        else:
            break
    return dt

# OCR cache version: bump when CLOCK_BOX/TS_BOX/parse logic changes so stale
# cached times are ignored instead of silently reused.
OCR_CACHE_V = 3
OCR_TS_FMT = "%Y-%m-%d %H:%M:%SZ"

# Strip overview: most recent per-snapshot JPGs only, so hourly snapshots
# don't grow it unbounded (24+/day). Older frames remain in frames/.
STRIP_MAX = 8

# Raw snapshot filename: HHMM-UTC_HHMM-IST (e.g. 0704-UTC_1234-IST).
SNAP_NAME_RE = re.compile(r"(\d{4})-UTC_(\d{4})-IST")


def snapshot_utc_time(path: Path,
                      day_ist_date: datetime.date | None = None) -> datetime.datetime:
    """Stable snapshot time parsed from filename, not file mtime.

    mtime resets on every fresh CI checkout, so mtime-based estimates shift
    on rebuild and rename all frames. The filename already encodes capture
    time; combine the IST HHMM with the day dir's IST date (or the parent
    dir name) and convert to UTC. Falls back to mtime only if unparseable.
    """
    m = SNAP_NAME_RE.search(path.stem)
    if m:
        try:
            if day_ist_date is None:
                try:
                    day_ist_date = datetime.date.fromisoformat(path.parent.name)
                except ValueError:
                    day_ist_date = None
            if day_ist_date is not None:
                ist_hhmm = m.group(2)
                hh, mm = int(ist_hhmm[:2]), int(ist_hhmm[2:])
                if 0 <= hh <= 23 and 0 <= mm <= 59:
                    ist_dt = datetime.datetime(
                        day_ist_date.year, day_ist_date.month, day_ist_date.day,
                        hh, mm, tzinfo=IST)
                    return ist_dt.astimezone(datetime.timezone.utc)
        except (ValueError, OverflowError):
            pass
    return datetime.datetime.fromtimestamp(
        path.stat().st_mtime, datetime.timezone.utc)


def slot_key(dt_ist: datetime.datetime) -> str:
    return f"{dt_ist.hour:02d}:{(dt_ist.minute // 30) * 30:02d}"


def _day_date(day_dir: Path) -> datetime.date | None:
    """IST date from a day-dir name, else None."""
    try:
        return datetime.date.fromisoformat(day_dir.name)
    except ValueError:
        return None


def prev_day_hashes(day_dir: Path, tail: int = 40) -> set[str]:
    """Hashes from previous calendar day's manifest (handles midnight overlap)."""
    try:
        prev = str(datetime.date.fromisoformat(day_dir.name) - datetime.timedelta(days=1))
    except ValueError:
        return set()
    mf = day_dir.parent / prev / "frames.json"
    try:
        hashes = json.loads(mf.read_text()).get("hashes", [])
        return set(hashes[-tail:])
    except (OSError, ValueError):
        return set()


def _ocr_from_manifest(mf: Path) -> dict[str, datetime.datetime]:
    """Successful OCR times from one frames.json manifest, else {}."""
    try:
        m = json.loads(mf.read_text())
    except (OSError, ValueError):
        return {}
    if m.get("ocr_v") != OCR_CACHE_V:
        return {}
    out: dict[str, datetime.datetime] = {}
    ocr = m.get("ocr")
    if not isinstance(ocr, dict):
        return {}
    for h, v in ocr.items():
        ts = v.get("t_utc") if isinstance(v, dict) else None
        if not isinstance(ts, str):
            continue
        try:
            out[h] = datetime.datetime.strptime(
                ts, OCR_TS_FMT).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return out


def load_ocr_cache(day_dir: Path) -> dict[str, datetime.datetime]:
    """Cached OCR times keyed by masked hash, scoped to current + prev day.

    Only successful OCR reads are cached (interpolated/estimated times are
    recomputed fresh each rebuild from anchors). Corrupt manifests or
    version mismatches fall back to {} (full OCR, current behavior).
    """
    cache = _ocr_from_manifest(day_dir / "frames.json")
    try:
        prev = str(datetime.date.fromisoformat(day_dir.name) - datetime.timedelta(days=1))
    except ValueError:
        return cache
    prev_cache = _ocr_from_manifest(day_dir.parent / prev / "frames.json")
    for h, dt in prev_cache.items():
        cache.setdefault(h, dt)
    return cache


def _load_manifest(day_dir: Path) -> dict:
    """frames.json as dict, {} when missing/corrupt."""
    try:
        m = json.loads((day_dir / "frames.json").read_text())
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def _timestamp_decoded(frames: list[tuple[str, Image.Image]],
                       ocr_cache: dict[str, datetime.datetime],
                       snap_utc: datetime.datetime,
                       ) -> list[tuple[datetime.datetime, bool]]:
    """Timestamp decoded unique frames: OCR cache, clock overlay, estimates."""
    # OCR clock reads a UTC time; the fallback date must be the UTC
    # calendar date (not the IST date) or overnight frames shift a day.
    fallback_date = snap_utc.date()
    ocr_times: list[datetime.datetime | None] = []
    for h, img in frames:
        dt = ocr_cache.get(h)
        if dt is None:
            dt, _ = parse_frame_time(ocr_timestamp(img) or "", fallback_date)
            if dt is not None:
                dt = clamp_frame_utc(dt, snap_utc)
                ocr_cache[h] = dt
        else:
            # Cached times predate the clamp fix (or a newer snapshot gives a
            # tighter bound): re-clamp so stale future dates self-heal.
            dt = clamp_frame_utc(dt, snap_utc)
            ocr_cache[h] = dt
        ocr_times.append(dt)
    # Fill unreadable frames by interpolation between OCR anchors
    # in the same GIF (IMD cadence ~10min); last resort: snapshot time.
    k = len(frames)
    known = {j: dt for j, dt in enumerate(ocr_times) if dt is not None}
    times: list[tuple[datetime.datetime, bool]] = []
    for j in range(k):
        if j in known:
            times.append((known[j], False))
            continue
        prev = next((jp for jp in range(j - 1, -1, -1) if jp in known), None)
        nxt = next((jn for jn in range(j + 1, k) if jn in known), None)
        if prev is not None and nxt is not None:
            step = (known[nxt] - known[prev]) / (nxt - prev)
            times.append((known[prev] + step * (j - prev), True))
        elif prev is not None:
            times.append((known[prev] + (j - prev) * FRAME_STEP, True))
        elif nxt is not None:
            times.append((known[nxt] - (nxt - j) * FRAME_STEP, True))
        else:
            times.append((snap_utc - SNAP_LAG - (k - 1 - j) * FRAME_STEP, True))
    return times


def _rebuild_artifacts(day_dir: Path,
                       entries: list[dict],
                       ocr_cache: dict[str, datetime.datetime],
                       blob_shas: list[str],
                       decoded_total: int,
                       dupes: int) -> dict:
    """Sort entries, rebuild slots/daily.gif/strip.jpg, rewrite manifest.

    All times are IST (`t_ist`); `t_utc` is only the sort key (same order).
    Every list written here is chronological so the gallery never shows
    e.g. 22:xx after 06:xx within one IST day.
    """
    entries.sort(key=lambda e: (e.get("t_utc", ""), e.get("img", "")))
    slots: dict[str, dict] = {}
    for e in entries:
        sk = e.get("slot")
        if not sk:
            continue
        if sk not in slots:
            slots[sk] = {"slot": sk,
                         "time": _short_time(e.get("t_ist", "")),
                         "img": e["img"],
                         "estimated": e.get("estimated", False)}
    info: dict = {"snapshots": len(blob_shas), "decoded_total": decoded_total,
                  "unique_frames": len(entries), "dupes_dropped": dupes,
                  "daily_gif": None, "strip": None}
    ordered: list[Image.Image] = []
    for e in entries:
        try:
            ordered.append(Image.open(day_dir / e["img"]).convert("RGB"))
        except (OSError, KeyError):
            continue
    if ordered:
        daily_path = day_dir / "daily.gif"
        ordered[0].save(daily_path, save_all=True, append_images=ordered[1:],
                        duration=600, loop=0, optimize=True)
        info["daily_gif"] = daily_path.name
    lasts = sorted(day_dir.glob("*_last.jpg"),
                   key=lambda p: snapshot_utc_time(p, _day_date(day_dir)))
    # Cap the strip at the most recent snapshots (see STRIP_MAX); older
    # frames remain available in frames/.
    lasts = lasts[-STRIP_MAX:]
    thumbs: list[Image.Image] = []
    for p in lasts:
        try:
            thumbs.append(Image.open(p).convert("RGB"))
        except OSError:
            continue
    if not thumbs:
        # No per-snapshot JPGs: sample saved frames instead.
        for e in entries[:: max(1, len(entries) // 8)][:8]:
            try:
                thumbs.append(Image.open(day_dir / e["img"]).convert("RGB"))
            except (OSError, KeyError):
                continue
    if thumbs:
        from PIL import ImageOps
        cols = 4 if len(thumbs) > 4 else len(thumbs)
        rows = (len(thumbs) + cols - 1) // cols
        tw, th = 320, 240
        strip = Image.new("RGB", (cols * tw, rows * th), "white")
        for i, t in enumerate(thumbs):
            # Aspect-preserving fit into the cell; never stretch mixed sizes.
            cell = ImageOps.fit(t, (tw, th), method=Image.BILINEAR)
            strip.paste(cell, ((i % cols) * tw, (i // cols) * th))
        strip_path = day_dir / "strip.jpg"
        strip.save(strip_path, quality=72)
        info["strip"] = strip_path.name
    slot_list = [slots[k] for k in sorted(slots)]
    # Persist OCR times only for frames saved in this day. Writing back the
    # merged prev-day cache would accumulate one extra stale day per rebuild.
    saved_hashes = {e["h"] for e in entries if "h" in e}
    (day_dir / "frames.json").write_text(json.dumps(
        {"hashes": [e["h"] for e in entries if "h" in e], "frames": entries,
         "slots": slot_list,
         "stats": {"snapshots": info["snapshots"],
                   "decoded_total": info["decoded_total"],
                   "unique_frames": info["unique_frames"],
                   "dupes_dropped": info["dupes_dropped"]},
         "blob_shas": blob_shas,
         "ocr_v": OCR_CACHE_V,
         "ocr": {h: {"t_utc": dt.strftime(OCR_TS_FMT)}
                 for h, dt in ocr_cache.items() if h in saved_hashes}},
        indent=1))
    return info


def _load_day_state(day_dir: Path) -> dict:
    """Manifest entries (files present), cache, shas and counters for one day."""
    m = _load_manifest(day_dir)
    old_entries = [e for e in m.get("frames", []) if isinstance(e, dict)]
    entries = [e for e in old_entries
               if isinstance(e.get("img"), str) and (day_dir / e["img"]).is_file()]
    blob_shas = [s for s in m.get("blob_shas", []) if isinstance(s, str)]
    old_stats = m.get("stats", {}) if isinstance(m.get("stats"), dict) else {}
    try:
        decoded_total = int(old_stats.get("decoded_total", 0))
    except (TypeError, ValueError):
        decoded_total = 0
    try:
        dupes = int(old_stats.get("dupes_dropped", 0))
    except (TypeError, ValueError):
        dupes = 0
    return {"entries": entries, "n_old_kept": len(entries),
            "blob_shas": blob_shas, "decoded_total": decoded_total,
            "dupes": dupes}


def ingest_day(day_dir: Path, blob: bytes,
               snap_utc: datetime.datetime, stamp: str) -> dict:
    """Decode one downloaded GIF and merge its frames by each frame's IST day.

    Raw GIF bytes are never written to disk: only frames/*.jpg,
    {stamp}_last.jpg, daily.gif, strip.jpg and frames.json are kept.
    Dedupes via masked hashes (in-GIF filler, 3h-window overlap across
    snapshots, prev-day tail) and via blob sha (identical re-downloads).

    The snapshot JPG stays in the snapshot's IST day (`day_dir`), but every
    radar frame is filed under its own clock-derived IST date — so 22:xx IST
    frames from a post-midnight snapshot land on the previous IST day instead
    of leaking into the next day's gallery.
    """
    snap_dir = day_dir
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Shared OCR cache across the snapshot day + neighbours (a 3h GIF always
    # straddles at most one midnight). Re-clamped inside _timestamp_decoded.
    ocr_cache = load_ocr_cache(snap_dir)

    frames, n_decoded = iter_unique_frames(blob)
    timed = _timestamp_decoded(frames, ocr_cache, snap_utc)

    # Group new frames by their own IST calendar date before touching disk.
    by_day: dict[str, list[dict]] = {}
    order: list[str] = []
    for (h, img), (t_utc, estimated) in zip(frames, timed):
        t_ist = t_utc.astimezone(IST)
        key = t_ist.strftime("%Y-%m-%d")
        if key not in by_day:
            by_day[key] = []
            order.append(key)
        by_day[key].append({"h": h, "img_obj": img, "t_utc": t_utc,
                            "t_ist": t_ist, "estimated": estimated})

    # Load state for the snapshot day + every frame day it touches.
    touched = [snap_dir.name] + [k for k in order if k != snap_dir.name]
    states: dict[str, dict] = {}
    for key in touched:
        d = ARCHIVE / key
        d.mkdir(parents=True, exist_ok=True)
        states[key] = _load_day_state(d)
        for h, dt in _ocr_from_manifest(d / "frames.json").items():
            ocr_cache.setdefault(h, dt)

    # Snapshot-day stats own the decode counters (one GIF == one download).
    snap_state = states[snap_dir.name]
    snap_state["decoded_total"] += n_decoded
    snap_state["dupes"] += max(0, n_decoded - len(frames))

    seen: set[str] = set()
    for key in touched:
        d = ARCHIVE / key
        seen |= {e["h"] for e in states[key]["entries"] if "h" in e}
        seen |= prev_day_hashes(d)

    sha = hashlib.sha256(blob).hexdigest()

    new_total = 0
    for key in touched:
        st = states[key]
        if sha not in st["blob_shas"]:
            st["blob_shas"].append(sha)
        for p in by_day.get(key, []):
            if p["h"] in seen:
                st["dupes"] += 1
                continue
            seen.add(p["h"])
            st.setdefault("pending", []).append(p)

    # Materialize each day's new frames with collision-safe names.
    for key in touched:
        st = states[key]
        target = ARCHIVE / key
        entries = st["entries"]
        used_names = {Path(e.get("img", "")).name for e in entries
                      if isinstance(e.get("img"), str)}
        n_next = len(entries)
        frames_dir = target / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        for p in st.get("pending", []):
            t_ist = p["t_ist"]
            while True:
                name = f"{t_ist.strftime('%H%M%S')}_{n_next:03d}.jpg"
                n_next += 1
                if name not in used_names and not (frames_dir / name).exists():
                    break
            used_names.add(name)
            img = p["img_obj"]
            img.resize((img.width // 2, img.height // 2)).save(
                frames_dir / name, quality=72)
            entries.append({"h": p["h"],
                            "t_utc": p["t_utc"].strftime("%Y-%m-%d %H:%M:%SZ"),
                            "t_ist": t_ist.strftime("%Y-%m-%d %H:%M IST"),
                            "img": f"frames/{name}",
                            "slot": slot_key(t_ist),
                            "estimated": p["estimated"]})
        new_total += len(st.get("pending", []))

    try:
        extract_last_frame(blob).save(snap_dir / f"{stamp}_last.jpg", quality=80)
    except Exception as exc:  # noqa: BLE001
        print(f"last-frame save failed: {exc}", file=sys.stderr)

    info: dict = {"new_frames": 0, "days": []}
    for key in touched:
        st = states[key]
        target = ARCHIVE / key
        day_info = _rebuild_artifacts(target, st["entries"], ocr_cache,
                                      st["blob_shas"], st["decoded_total"],
                                      st["dupes"])
        day_info["day"] = key
        day_info["new_frames"] = len(st.get("pending", []))
        info["days"].append(day_info)
    info["new_frames"] = sum(d["new_frames"] for d in info["days"])
    # Back-compat: top-level snapshots/new_frames describe the snapshot day.
    for d in info["days"]:
        if d["day"] == snap_dir.name:
            info.update({k: d[k] for k in
                         ("snapshots", "decoded_total", "unique_frames",
                          "dupes_dropped", "daily_gif", "strip") if k in d})
            break
    return info


def build_daily(day_dir: Path) -> dict:
    """Rebuild a day's artifacts from committed files (no raw GIFs).

    Regenerates daily.gif, strip.jpg, slots and frames.json from the
    frames/*.jpg + *_last.jpg already on disk. Used after prune or repair;
    the fetch path uses ingest_day() instead.
    """
    m = _load_manifest(day_dir)
    entries = [e for e in m.get("frames", []) if isinstance(e, dict)
               and isinstance(e.get("img"), str)
               and (day_dir / e["img"]).is_file()]
    ocr_cache = load_ocr_cache(day_dir)
    blob_shas = [s for s in m.get("blob_shas", []) if isinstance(s, str)]
    old_stats = m.get("stats", {}) if isinstance(m.get("stats"), dict) else {}
    try:
        decoded_total = int(old_stats.get("decoded_total",
                                          old_stats.get("raw_frames", 0)))
    except (TypeError, ValueError):
        decoded_total = 0
    try:
        dupes = int(old_stats.get("dupes_dropped", 0))
    except (TypeError, ValueError):
        dupes = 0
    return _rebuild_artifacts(day_dir, entries, ocr_cache,
                              blob_shas, decoded_total, dupes)


def _short_time(t_ist: str) -> str:
    """'2026-09-12 09:59 IST' -> '09:59'. Parses, never slices blindly."""
    if not t_ist:
        return ""
    try:
        dt = datetime.datetime.strptime(t_ist.strip(), "%Y-%m-%d %H:%M IST")
        return dt.strftime("%H:%M")
    except ValueError:
        pass
    m = re.search(r"(\d{1,2}):(\d{2})", t_ist)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return t_ist


def rebuild_index() -> dict:
    days = []
    if ARCHIVE.exists():
        for day_dir in sorted(ARCHIVE.iterdir()):
            if not day_dir.is_dir():
                continue
            # Snapshots are per-download _last.jpg files; raw GIFs are
            # deleted after frame extraction and never committed.
            # Sort by actual capture time (filename starts with UTC HHMM, so
            # plain lexicographic order misplaces overnight snapshots);
            # labels are IST-only for display.
            day_date = _day_date(day_dir)
            lasts = sorted(day_dir.glob("*_last.jpg"),
                           key=lambda p: snapshot_utc_time(p, day_date))
            snaps = []
            for jpg in lasts:
                ist = snapshot_utc_time(jpg, day_date).astimezone(IST)
                snaps.append({
                    "jpg": f"archive/{day_dir.name}/{jpg.name}",
                    "label": ist.strftime("%H:%M IST"),
                    "bytes": jpg.stat().st_size,
                })
            has_daily = (day_dir / "daily.gif").exists()
            has_strip = (day_dir / "strip.jpg").exists()
            try:
                manifest = json.loads((day_dir / "frames.json").read_text())
                stats = manifest.get("stats", {})
                raw_slots = manifest.get("slots", [])
                manifest_frames = manifest.get("frames", [])
            except (OSError, ValueError):
                stats, raw_slots, manifest_frames = {}, [], []
            slots = []
            for s in sorted(raw_slots,
                            key=lambda s: (s.get("slot", ""), s.get("time", ""),
                                           s.get("img", ""))):
                img = day_dir / s["img"]
                if img.exists():
                    slots.append({
                        "slot": s["slot"], "time": s["time"],
                        "estimated": s.get("estimated", False),
                        "img": f"archive/{day_dir.name}/{s['img']}",
                    })
            frames = []
            for e in sorted(manifest_frames,
                            key=lambda e: (e.get("t_ist", ""), e.get("img", ""))):
                img = day_dir / e["img"]
                if img.exists():
                    t = (e.get("t_ist", "") or "")
                    frames.append({
                        "slot": e.get("slot"), "time": _short_time(t),
                        "t_ist": t,
                        "estimated": e.get("estimated", False),
                        "img": f"archive/{day_dir.name}/{e['img']}",
                    })
            days.append({
                "date": day_dir.name,
                "count": len(snaps),
                "snaps": snaps,
                "slots": slots,
                "frames": frames,
                "daily_gif": f"archive/{day_dir.name}/daily.gif" if has_daily else None,
                "strip": f"archive/{day_dir.name}/strip.jpg" if has_strip else None,
                "daily_frames": stats.get("unique_frames"),
                "dupes_dropped": stats.get("dupes_dropped"),
            })
    days.sort(key=lambda d: d["date"], reverse=True)
    payload = {
        "radar": "KKL_MAXZ (Karaikal)",
        "source": URL,
        "updated_ist": now_ist().strftime("%Y-%m-%d %H:%M IST"),
        "days": days,
    }
    DATA_JSON.parent.mkdir(parents=True, exist_ok=True)
    DATA_JSON.write_text(json.dumps(payload, indent=1))
    return payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="dry run to /tmp, no index rewrite")
    ap.add_argument("--force", action="store_true", help="save even if identical to latest")
    args = ap.parse_args()

    print(f"fetching {URL}")
    blob = download()
    print(f"got {len(blob)} bytes, frames={frame_count(blob)}, sha={sha_short(blob)}")

    ts = now_ist()
    day = ts.strftime("%Y-%m-%d")
    utc = datetime.datetime.now(datetime.timezone.utc)
    stamp = f"{utc.strftime('%H%M')}-UTC_{ts.strftime('%H%M')}-IST"

    if args.test:
        out = Path("/tmp/opencode/kkl_test")
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{stamp}.gif").write_bytes(blob)
        extract_last_frame(blob).save(out / f"{stamp}_last.jpg", quality=80)
        print(f"TEST OK -> {out}")
        return 0

    day_dir = ARCHIVE / day
    day_dir.mkdir(parents=True, exist_ok=True)

    # dedupe without raw files: skip blobs already ingested (a 3h GIF
    # straddles midnight, so search every day manifest, not just today's).
    sha = hashlib.sha256(blob).hexdigest()
    seen_blob = sha in _load_manifest(day_dir).get("blob_shas", [])
    if not seen_blob and not args.force:
        for other in ARCHIVE.glob("????-??-??"):
            if other.name == day:
                continue
            if sha in _load_manifest(other).get("blob_shas", []):
                seen_blob = True
                break
    if not args.force and seen_blob:
        print("identical to an ingested snapshot, rebuilding index only")
        rebuild_index()
        print("noop-save (duplicate)")
        return 0

    dinfo = ingest_day(day_dir, blob, utc, stamp)
    print(f"ingested {stamp} (+{dinfo.get('new_frames', 0)} frames): {dinfo}")
    # Raw GIFs are never kept: frames are extracted above, so delete any
    # leftovers (legacy archives) to keep the repo small.
    for f in day_dir.glob("*-IST.gif"):
        try:
            f.unlink()
        except OSError as exc:  # noqa: BLE001
            print(f"raw cleanup failed for {f.name}: {exc}", file=sys.stderr)
    payload = rebuild_index()
    print(f"index updated: {len(payload['days'])} day(s), current={day} snapshots={dinfo.get('snapshots', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
