#!/usr/bin/env python3
"""PAC daily-accumulation pipeline for the KKL radar archive.

Reads the IMD Karaikal PAC product (24h rainfall accumulation):
  https://mausam.imd.gov.in/Radar/pac_kkl.gif
maps its mm legend onto every place in data/places.json, and stores
one frozen reading per IST date.

Freeze rule: IMD issues the 24h product at ~08:30 IST, so the first
successful fetch after 09:30 IST (PAC_CUTOFF_IST) freezes
docs/archive/YYYY-MM-DD/pac.png and its pac.json entry under the
issue date. Earlier runs refresh the previous issue in place; a live
frame identical to the previous frozen day is never frozen as a new
day (covers IMD delays). The frontend shows the live PAC URL for
today and the frozen pac.png for older dates.

Usage:
  python scripts/pac_rain.py fetch [--force]
  python scripts/pac_rain.py parse --src /tmp/pac.gif --date 2026-10-03
  python scripts/pac_rain.py export
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
ARCHIVE = DOCS / "archive"
PAC_JSON = DOCS / "data" / "pac.json"
MASTER_JSON = ROOT / "data" / "places.json"

PAC_URL = "https://mausam.imd.gov.in/Radar/pac_kkl.gif"
PAC_LIVE_URL = PAC_URL
HEADERS = {
    "User-Agent": "Mozilla/5.0 (KKL-radar-archiver; GitHub Actions) AppleWebKit/537.36",
    "Accept": "image/gif,image/*,*/*",
    "Referer": "https://mausam.imd.gov.in/",
}

# PAC legend, top band to bottom band. Values are the lower bound of
# each bin in mm. The top bin is open ended (>=100). Colors are sampled
# from a real 880x720 PAC frame at PAC_BAND_YS_720 and stored here so
# parsing never depends on per-frame sampling drift.
PAC_MM_STEPS: list[float] = [
    100, 93, 87, 80, 74, 67, 60, 54, 47, 41, 34, 27, 21, 14, 7.6, 1.0,
]
PAC_COLORS: list[str] = [
    "#e42112", "#ff3d18", "#ff3b00", "#ff4100", "#ff7900", "#ffaa00",
    "#ffd400", "#ffef5e", "#d4f3ff", "#51cdff", "#1ea6ff", "#0683ff",
    "#004fff", "#001ee7", "#1000bc", "#3900a0",
]
PAC_LUT: list[tuple[float, str]] = list(zip(PAC_MM_STEPS, PAC_COLORS))

# Band centers in a 720px-tall PAC frame, top to bottom.
PAC_BAND_YS_720 = (
    399, 412, 426, 439, 453, 466, 480, 493,
    507, 520, 534, 547, 561, 574, 588, 601,
)

MATCH_DIST = 60.0
WINDOW_PX = 5


def now_ist() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).astimezone(IST)


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def mm_to_color(mm: float | None) -> str | None:
    if mm is None:
        return None
    best = min(PAC_LUT, key=lambda kv: abs(kv[0] - mm))
    return best[1]


def download_pac(retries: int = 3) -> bytes:
    import requests

    last_err = None
    for i in range(retries):
        try:
            r = requests.get(PAC_URL, headers=HEADERS, timeout=60)
            r.raise_for_status()
            if r.content[:6] not in (b"GIF87a", b"GIF89a",
                                     b"\x89PNG\r\n"):
                raise ValueError("PAC body is not an image")
            if len(r.content) < 20_000:
                raise ValueError(f"suspiciously small PAC: {len(r.content)}")
            return r.content
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"pac download attempt {i + 1}/{retries} failed: {e}",
                  file=sys.stderr)
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"pac download failed: {last_err}")


def _pac_lut_rgb() -> list[tuple[float, tuple[int, int, int]]]:
    return [(mm, _hex_to_rgb(c)) for mm, c in PAC_LUT]


# PAC range rings drawn on every frame, in km.
PAC_RING_KM: tuple[float, ...] = (100, 150, 200, 250)


def calibrate_pac(im):
    """Fit PAC radar center + km/px from the frame's own range rings.

    The MAX_Z calibrate() looks for six orange "200" labels, but the PAC
    background map draws every ring label (100/150/200/250) in the same
    tan ink, so that fit always bails to the MAX_Z nominal geometry --
    which puts Jaffna on Rameswaram. Instead this fits the black dotted
    range rings + radial spokes, which rain never paints over:

    - radar center = intersection of the N-S and E-W spokes, i.e. the
      dominant column / row of sea-masked black pixels (legend panel
      excluded so its borders can't win);
    - scale = concentric-circle fit of the radial histogram peaks to
      the 100:150:200:250 ratio.

    All pixel thresholds scale with frame size, so any IMD-served
    resolution works. Raises ValueError when the rings aren't found.
    """
    import numpy as np
    from PIL import Image, ImageFilter

    from rain_finder import FrameGeom

    W, H = im.size
    a = np.array(im.convert("RGB")).astype(np.int16)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    black = (R < 60) & (G < 60) & (B < 60)
    grey = ((R >= 180) & (R <= 202) & (G >= 180) & (G <= 202)
            & (B >= 180) & (B <= 202))
    k = max(3, int(round(7 * W / 880.0)))
    if k % 2 == 0:
        k += 1
    sea = np.array(Image.fromarray(grey)
                   .filter(ImageFilter.MaxFilter(k))) > 0
    panel_x = int(round(W * 0.80))
    m = black & sea
    m[:, panel_x:] = False

    # Raw counts: the spokes are dotted 1px lines, so any smoothing
    # dilutes them into the background.
    col = m.sum(axis=0).astype(float)
    row = m.sum(axis=1).astype(float)
    cx = int(np.argmax(col[:panel_x]))
    cy = int(np.argmax(row))

    def _second_peak(s: np.ndarray, at: int, gap: int) -> float:
        s2 = s.copy()
        s2[max(0, at - gap):at + gap + 1] = -1.0
        return float(s2.max())

    if not (0.15 * W <= cx <= 0.75 * W and 0.20 * H <= cy <= 0.80 * H):
        raise ValueError(f"pac spokes off-map: cx={cx} cy={cy} "
                         f"for {W}x{H}")
    if col[cx] < max(20.0, 60.0 * H / 720.0) or col[cx] < 1.5 * _second_peak(col, cx, 8):
        raise ValueError(f"weak N-S spoke: peak={col[cx]:.0f}")
    if row[cy] < max(20.0, 60.0 * W / 880.0) or row[cy] < 1.5 * _second_peak(row, cy, 8):
        raise ValueError(f"weak E-W spoke: peak={row[cy]:.0f}")

    ys, xs = np.where(m)
    r = np.hypot(xs.astype(float) - cx, ys.astype(float) - cy)
    rmax = int(min(W, H) * 0.55)
    hist, _ = np.histogram(r, bins=np.arange(rmax + 1))
    sm = (np.pad(hist.astype(float), 1, mode="edge")[:-2]
          + hist.astype(float)
          + np.pad(hist.astype(float), 1, mode="edge")[2:]) / 3.0
    lo = int(0.06 * W)
    peaks: list[int] = []
    for i in range(lo, rmax):
        if sm[i] == sm[max(lo, i - 4):i + 5].max() and sm[i] >= 0.12 * sm[lo:].max():
            peaks.append(i)
    peaks.sort(key=lambda i: sm[i], reverse=True)
    peaks = peaks[:12]

    best: tuple[float, float] | None = None  # (err, px_per_km)
    for n in (4, 3):
        if len(peaks) < n:
            continue
        from itertools import combinations

        for combo in combinations(peaks, n):
            rr = sorted(combo)
            ring_opts = [PAC_RING_KM] if n == 4 else [
                tuple(kk for j, kk in enumerate(PAC_RING_KM) if j != skip)
                for skip in range(4)]
            for rings in ring_opts:
                base = sum(r_ / k_ for r_, k_ in zip(rr, rings)) / n
                err = max(abs(r_ / k_ - base) / base for r_, k_ in zip(rr, rings))
                if err < 0.04 and (best is None or err < best[0]):
                    best = (err, base)
        if best is not None:
            break
    if best is None:
        raise ValueError(f"no concentric PAC rings at ({cx},{cy})")
    km_per_px = 1.0 / best[1]
    r250 = 250.0 / km_per_px
    if not (0.30 * W <= r250 <= 0.50 * W):
        raise ValueError(f"implausible PAC scale: {km_per_px:.4f} km/px")
    x0 = max(0, int(round(cx - r250 - 2)))
    y0 = max(0, int(round(cy - r250 - 2)))
    x1 = min(W, int(round(cx + r250 + 3)))
    y1 = min(H, int(round(cy + r250 + 3)))
    return FrameGeom(x0, y0, x1, y1, float(cx - x0), float(cy - y0),
                     km_per_px, "pac-rings")


def parse_pac_image(im) -> dict[tuple[str, str], float | None]:
    """Map a PAC PIL image onto places. Returns {(name, district): mm}."""
    import numpy as np

    from rain_finder import RADAR_LAT, RADAR_LON, latlon_to_km

    geom = calibrate_pac(im.convert("RGB"))
    x0, y0, x1, y1 = geom.x0, geom.y0, geom.x1, geom.y1
    crop = im.convert("RGB").crop((x0, y0, x1, y1))
    A = np.array(crop).astype(np.int16)
    lut = _pac_lut_rgb()
    lut_rgb = np.array([c for _, c in lut])
    d2 = ((A[:, :, None, :] - lut_rgb[None, None, :, :]) ** 2).sum(axis=3)
    best = d2.argmin(axis=2)
    mind = np.sqrt(d2.min(axis=2))
    echo = mind < MATCH_DIST
    ph, pw = echo.shape
    mm_vals = np.array([m for m, _ in lut])
    places = json.loads(MASTER_JSON.read_text())
    out: dict[tuple[str, str], float | None] = {}
    h = WINDOW_PX // 2
    for p in places:
        ek, nk = latlon_to_km(p["lat"], p["lon"], RADAR_LAT, RADAR_LON)
        px = geom.cx + ek / geom.km_per_px
        py = geom.cy - nk / geom.km_per_px
        ix, iy = int(round(px)), int(round(py))
        if not (0 <= ix < pw and 0 <= iy < ph):
            out[(p["name_en"], p["district"])] = None
            continue
        win_echo = echo[max(0, iy - h):iy + h + 1, max(0, ix - h):ix + h + 1]
        if not win_echo.any():
            out[(p["name_en"], p["district"])] = None
            continue
        win_best = best[max(0, iy - h):iy + h + 1, max(0, ix - h):ix + h + 1]
        vals = mm_vals[win_best[win_echo]]
        out[(p["name_en"], p["district"])] = float(vals.max())
    return out


def _load_pac_json() -> dict:
    try:
        m = json.loads(PAC_JSON.read_text())
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def export_pac_json() -> Path:
    data = _load_pac_json()
    days = data.get("days", [])
    if not isinstance(days, list):
        days = []
    days.sort(key=lambda d: d.get("date", ""), reverse=True)
    data["days"] = days
    places = json.loads(MASTER_JSON.read_text())
    data.update({
        "radar": "KKL_PAC (Karaikal 24H accumulation)",
        "source": PAC_URL,
        "updated_ist": now_ist().strftime("%Y-%m-%d %H:%M IST"),
        "lut": [{"mm": mm, "color": color} for mm, color in PAC_LUT],
        "places": [{"name_en": p["name_en"], "district": p["district"]}
                   for p in sorted(places,
                                   key=lambda q: (q["district"],
                                                  q["name_en"]))],
    })
    PAC_JSON.parent.mkdir(parents=True, exist_ok=True)
    PAC_JSON.write_text(json.dumps(data, indent=1))
    return PAC_JSON


def store_day(date_ist: str, readings: dict[tuple[str, str], float | None],
              image_rel: str, captured_ist: str) -> None:
    data = _load_pac_json()
    days = data.get("days", [])
    if not isinstance(days, list):
        days = []
    rows = []
    for (name, district), mm in sorted(readings.items()):
        rows.append({"place": name, "district": district, "mm": mm,
                     "color": mm_to_color(mm)})
    rows.sort(key=lambda r: ((r["mm"] is None), -(r["mm"] or 0),
                             r["district"], r["place"]))
    entry = {"date": date_ist, "image": image_rel,
             "captured_ist": captured_ist, "rows": rows}
    days = [d for d in days if d.get("date") != date_ist] + [entry]
    data["days"] = days
    PAC_JSON.parent.mkdir(parents=True, exist_ok=True)
    PAC_JSON.write_text(json.dumps(data, indent=1))
    export_pac_json()


# IMD issues the 24h PAC at ~08:30 IST, stamped with that IST date.
# Before the cutoff the live URL still serves the previous issue, so a
# midnight freeze would archive yesterday's product under today's date
# while later runs overwrite the readings from the new issue. Freeze
# only after the cutoff; earlier runs refresh the previous day instead.
PAC_CUTOFF_IST = datetime.time(9, 30)


def pac_target_date(now: datetime.datetime | None = None) -> str:
    """IST date the live PAC product belongs to (its issue date)."""
    now = now or now_ist()
    day = now.date()
    if now.time() < PAC_CUTOFF_IST:
        day -= datetime.timedelta(days=1)
    return day.strftime("%Y-%m-%d")


# Timestamp block in the right info panel ("03:00:00Z / 3 OCT 2026 UTC"),
# as fractions of frame size so any IMD-served resolution works.
PAC_TS_BOX = (0.795, 0.295, 0.985, 0.375)


def ocr_pac_issue_date(im) -> str | None:
    """IST issue date read from the panel timestamp. None if unreadable.

    Parses the UTC clock + date the product stamps on every frame and
    converts to IST, so the archive follows the actual product instead
    of the wall clock. Guards digit glitches by rejecting dates more
    than a day from today; callers fall back to pac_target_date().
    """
    try:
        from fetch_kkl import DATE_RE, TIME_RE, TIME_Z_RE, ocr_text
    except ImportError:
        return None
    try:
        from PIL import ImageOps

        W, H = im.size
        c = im.convert("RGB").crop(
            (int(W * PAC_TS_BOX[0]), int(H * PAC_TS_BOX[1]),
             int(W * PAC_TS_BOX[2]), int(H * PAC_TS_BOX[3])))
        g = c.convert("L").point(lambda v: 255 if v > 140 else 0)
        box = ImageOps.invert(g).getbbox()
        if box:
            pad = 4
            x0, y0, x1, y1 = box
            g = g.crop((max(0, x0 - pad), max(0, y0 - pad),
                        min(g.width, x1 + pad), min(g.height, y1 + pad)))
        scale = max(1, 120 // max(1, g.height))
        g = g.resize((g.width * scale, g.height * scale))
        t = ocr_text(g).upper()
        m = TIME_Z_RE.search(t) or TIME_RE.search(t)
        dm = DATE_RE.search(t)
        if not (m and dm):
            return None
        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if hh > 23 or mm > 59 or ss > 61:
            return None
        d = datetime.datetime.strptime(
            f"{dm.group(1)} {dm.group(2)} {dm.group(3)}",
            "%d %b %Y").date()
        if not (2020 <= d.year <= 2030):
            return None
        utc = datetime.datetime(d.year, d.month, d.day, hh, mm, ss,
                                tzinfo=datetime.timezone.utc)
        issue = utc.astimezone(IST).date()
        if abs((issue - now_ist().date()).days) > 1:
            return None
        return issue.strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return None


def _same_issue(im_a, im_b, tol: float = 2.0) -> bool:
    """Pixel-compare two frames; True when they show the same product."""
    import numpy as np

    if im_a.size != im_b.size:
        return False
    a = np.array(im_a.convert("RGB")).astype(float)
    b = np.array(im_b.convert("RGB")).astype(float)
    return bool(abs(a - b).mean() < tol)


def fetch() -> dict:
    from PIL import Image
    import io

    now = now_ist()
    today = now.strftime("%Y-%m-%d")
    blob = download_pac()
    im = Image.open(io.BytesIO(blob)).convert("RGB")
    target = ocr_pac_issue_date(im) or pac_target_date(now)
    if target != pac_target_date(now):
        print(f"pac: panel stamp says {target}")
    readings = parse_pac_image(im)
    day_dir = ARCHIVE / target
    day_dir.mkdir(parents=True, exist_ok=True)
    pac_path = day_dir / "pac.png"
    if pac_path.is_file():
        print(f"pac: {target} already frozen, refreshing readings only")
    else:
        prev = (now.date() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        prev_path = ARCHIVE / prev / "pac.png"
        if (target == today and prev_path.is_file()
                and _same_issue(im, Image.open(prev_path))):
            # New issue not published yet (IMD delay): the live frame is
            # still the previous product. Refresh it in place instead of
            # freezing a stale image under today.
            print("pac: live frame still previous issue, "
                  f"refreshing {prev} instead of freezing {today}")
            target, day_dir, pac_path = prev, ARCHIVE / prev, prev_path
        else:
            im.save(pac_path, optimize=True)
            print(f"pac: froze post-issue image for {target}")
    captured = now.strftime("%Y-%m-%d %H:%M IST")
    store_day(target, readings, f"archive/{target}/pac.png", captured)
    export_pac_json()
    n_wet = sum(1 for v in readings.values() if v is not None)
    print(f"pac: {target} {n_wet}/{len(readings)} places with echo")
    return {"date": target, "wet": n_wet, "total": len(readings)}


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    p = sub.add_parser("parse")
    p.add_argument("--src", type=Path, required=True)
    p.add_argument("--date", required=True)
    sub.add_parser("export")
    args = ap.parse_args()
    if args.cmd == "fetch":
        fetch()
    elif args.cmd == "parse":
        from PIL import Image

        im = Image.open(args.src).convert("RGB")
        readings = parse_pac_image(im)
        store_day(args.date, readings, f"archive/{args.date}/pac.png",
                  now_ist().strftime("%Y-%m-%d %H:%M IST"))
        print(f"pac parse: {args.date} "
              f"{sum(1 for v in readings.values() if v is not None)} wet")
    elif args.cmd == "export":
        out = export_pac_json()
        print(f"pac export: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
