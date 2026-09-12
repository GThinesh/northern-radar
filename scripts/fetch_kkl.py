#!/usr/bin/env python3
"""KKL (Karaikal) IMD radar archiver.

Downloads https://mausam.imd.gov.in/Radar/animation/Converted/KKL_MAXZ.gif
(full ~3h animated GIF), stores timestamped raw snapshot + last-frame JPG,
rebuilds per-day summary (daily.gif + strip.jpg), updates docs/data/index.json.

Layout (all under docs/ so GitHub Pages can serve it):
  docs/archive/YYYY-MM-DD/HHMM-UTC_HHMM-IST.gif
  docs/archive/YYYY-MM-DD/HHMM-UTC_HHMM-IST_last.jpg
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
import shutil
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
    try:
        return getattr(Image.open(io.BytesIO(gif_bytes)), "n_frames", 1)
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


def iter_unique_frames(gif_bytes: bytes) -> list[tuple[str, Image.Image]]:
    """Decode GIF, drop consecutive frames with identical masked hash.

    Returns ordered [(hash, RGB), ...]. Corrupt input -> [].
    """
    out: list[tuple[str, Image.Image]] = []
    try:
        im = Image.open(io.BytesIO(gif_bytes))
        n = getattr(im, "n_frames", 1)
    except Exception as e:  # noqa: BLE001
        print(f"decode failed: {e}", file=sys.stderr)
        return out
    prev_h = None
    for i in range(n):
        try:
            im.seek(i)
            rgb = im.convert("RGB")
        except EOFError:
            break
        h = masked_hash(rgb)
        if h == prev_h:
            continue  # in-GIF filler repeat
        prev_h = h
        out.append((h, rgb))
    return out


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
        if TIME_RE.search(last):
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

# OCR cache version: bump when CLOCK_BOX/TS_BOX/parse logic changes so stale
# cached times are ignored instead of silently reused.
OCR_CACHE_V = 1
OCR_TS_FMT = "%Y-%m-%d %H:%M:%SZ"


def snapshot_utc_time(path: Path) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(
        path.stat().st_mtime, datetime.timezone.utc)


def slot_key(dt_ist: datetime.datetime) -> str:
    return f"{dt_ist.hour:02d}:{(dt_ist.minute // 30) * 30:02d}"


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


def build_daily(day_dir: Path) -> dict:
    """Build day summary from all raw snapshots.

    1. Decode every raw GIF, drop repeats (masked hash ignores clock text):
       in-GIF filler, cross-snapshot 3h-window overlap, prev-day tail.
    2. Timestamp each unique frame: cached OCR times first (keyed by masked
       hash, current + previous day), OCR of the clock overlay for unseen
       frames, else estimate from snapshot download time (last frame ~15min
       before, 10min steps).
    3. Save each unique frame to frames/HHMMSS_NNN.jpg (IST).
    4. Slots: from 00:00, every 30min — first frame in that slot wins;
       empty slots omitted.
    5. daily.gif: all unique frames in time order + strip.jpg overview.
    Writes frames.json manifest {hashes, frames, slots, stats, ocr_v, ocr}.
    """
    raws = sorted(day_dir.glob("*-IST.gif"))
    seen: set[str] = prev_day_hashes(day_dir)
    ocr_cache = load_ocr_cache(day_dir)
    frames_dir = day_dir / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    slots: dict[str, dict] = {}
    ordered: list[Image.Image] = []
    thumbs: list[Image.Image] = []
    raw_frames = in_gif_dupes = cross_dupes = 0
    n_saved = 0
    for g in raws:
        try:
            blob = g.read_bytes()
            n = frame_count(blob)
            frames = iter_unique_frames(blob)
        except Exception as e:  # noqa: BLE001
            print(f"skip corrupt {g.name}: {e}", file=sys.stderr)
            continue
        raw_frames += n
        in_gif_dupes += n - len(frames)
        snap_utc = snapshot_utc_time(g)
        try:
            thumbs.append(extract_last_frame(blob))
        except Exception:  # noqa: BLE001
            pass
        k = len(frames)
        snap_date = snap_utc.astimezone(IST).date()
        # Pass 1: timestamp every frame — cached OCR hits first (also shared
        # across snapshots within this run), else OCR the clock overlay.
        # Only successful OCR reads enter the cache; misses are interpolated
        # fresh in Pass 2 each rebuild.
        ocr_times: list[datetime.datetime | None] = []
        for h, img in frames:
            dt = ocr_cache.get(h)
            if dt is None:
                dt, _ = parse_frame_time(ocr_timestamp(img) or "", snap_date)
                if dt is not None:
                    ocr_cache[h] = dt
            ocr_times.append(dt)
        # Pass 2: fill unreadable frames by interpolation between OCR anchors
        # in the same GIF (IMD cadence ~10min); last resort: download time.
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
        for j, (h, img) in enumerate(frames):
            t_utc, estimated = times[j]
            if h in seen:
                cross_dupes += 1
                continue
            seen.add(h)
            t_ist = t_utc.astimezone(IST)
            name = f"{t_ist.strftime('%H%M%S')}_{n_saved:03d}.jpg"
            half = img.resize((img.width // 2, img.height // 2))
            half.save(frames_dir / name, quality=72)
            ordered.append(half)
            sk = slot_key(t_ist)
            entries.append({"h": h, "t_utc": t_utc.strftime("%Y-%m-%d %H:%M:%SZ"),
                            "t_ist": t_ist.strftime("%Y-%m-%d %H:%M IST"),
                            "img": f"frames/{name}", "slot": sk,
                            "estimated": estimated})
            if sk not in slots:
                slots[sk] = {"slot": sk,
                             "time": t_ist.strftime("%H:%M"),
                             "img": f"frames/{name}",
                             "estimated": estimated}
            n_saved += 1
    info: dict = {"raw_count": len(raws), "raw_frames": raw_frames,
                  "unique_frames": len(ordered),
                  "dupes_dropped": in_gif_dupes + cross_dupes,
                  "daily_gif": None, "strip": None}
    if ordered:
        daily_path = day_dir / "daily.gif"
        ordered[0].save(daily_path, save_all=True, append_images=ordered[1:],
                        duration=600, loop=0, optimize=True)
        info["daily_gif"] = daily_path.name
    if thumbs:
        cols = 4 if len(thumbs) > 4 else len(thumbs)
        rows = (len(thumbs) + cols - 1) // cols
        tw, th = 320, int(320 * thumbs[0].height / thumbs[0].width)
        strip = Image.new("RGB", (cols * tw, rows * th), "white")
        for i, t in enumerate(thumbs):
            strip.paste(t.resize((tw, th)), ((i % cols) * tw, (i // cols) * th))
        strip_path = day_dir / "strip.jpg"
        strip.save(strip_path, quality=72)
        info["strip"] = strip_path.name
    slot_list = [slots[k] for k in sorted(slots)]
    (day_dir / "frames.json").write_text(json.dumps(
        {"hashes": [e["h"] for e in entries], "frames": entries,
         "slots": slot_list, "stats": {
            k: v for k, v in info.items() if k not in ("daily_gif", "strip")},
         "ocr_v": OCR_CACHE_V,
         "ocr": {h: {"t_utc": dt.strftime(OCR_TS_FMT)}
                 for h, dt in ocr_cache.items()}},
        indent=1))
    return info


def rebuild_index() -> dict:
    days = []
    if ARCHIVE.exists():
        for day_dir in sorted(ARCHIVE.iterdir()):
            if not day_dir.is_dir():
                continue
            raws = sorted(day_dir.glob("*-IST.gif"))
            snaps = []
            for g in raws:
                jpg = g.with_name(g.stem + "_last.jpg")
                snaps.append({
                    "gif": f"archive/{day_dir.name}/{g.name}",
                    "jpg": f"archive/{day_dir.name}/{jpg.name}" if jpg.exists() else None,
                    "label": g.stem,  # e.g. 0130-UTC_0700-IST
                    "bytes": g.stat().st_size,
                })
            has_daily = (day_dir / "daily.gif").exists()
            has_strip = (day_dir / "strip.jpg").exists()
            try:
                manifest = json.loads((day_dir / "frames.json").read_text())
                stats = manifest.get("stats", {})
                raw_slots = manifest.get("slots", [])
                raw_frames = manifest.get("frames", [])
            except (OSError, ValueError):
                stats, raw_slots, raw_frames = {}, [], []
            slots = []
            for s in raw_slots:
                img = day_dir / s["img"]
                if img.exists():
                    slots.append({
                        "slot": s["slot"], "time": s["time"],
                        "estimated": s.get("estimated", False),
                        "img": f"archive/{day_dir.name}/{s['img']}",
                    })
            frames = []
            for e in raw_frames:
                img = day_dir / e["img"]
                if img.exists():
                    t = (e.get("t_ist", "") or "")
                    frames.append({
                        "slot": e.get("slot"), "time": t[11:16] or t,
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
    stamp = ts.strftime("%H%M-UTC")  # placeholder, corrected below
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

    # dedupe: skip if identical to newest raw
    existing = sorted(day_dir.glob("*-IST.gif"))
    if existing and not args.force:
        newest = existing[-1].read_bytes()
        if hashlib.sha256(newest).digest() == hashlib.sha256(blob).digest():
            print("identical to latest snapshot, still rebuilding daily/index")
            build_daily(day_dir)
            rebuild_index()
            print("noop-save (duplicate)")
            return 0

    raw_path = day_dir / f"{stamp}.gif"
    raw_path.write_bytes(blob)
    print(f"saved {raw_path} ({len(blob)} bytes)")

    jpg_path = raw_path.with_name(raw_path.stem + "_last.jpg")
    extract_last_frame(blob).save(jpg_path, quality=80)
    print(f"saved {jpg_path}")

    dinfo = build_daily(day_dir)
    print(f"daily rebuilt: {dinfo}")
    payload = rebuild_index()
    print(f"index updated: {len(payload['days'])} day(s), current={day} count={dinfo['raw_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
