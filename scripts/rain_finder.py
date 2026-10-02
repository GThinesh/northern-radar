#!/usr/bin/env python3
"""Rain-finding logic for the KKL radar pipeline.

Turns a radar image into per-place readings: the dBZ legend, the
category thresholds, the PPI geometry fit, the echo segmentation, and
the place projection. The SQLite layer lives in kkl_db.py and calls
into RainFinder. Nothing here touches a database.

Theory in one paragraph. dBZ is the log scale of radar reflectivity:
dBZ = 10 * log10(Z). Higher values mean heavier rain. The IMD MAX_Z
frame paints echoes with a fixed 16-step colorbar, so this module reads
each frame's own legend and matches echo pixels to it by color. Pixel
offsets become kilometers through the azimuthal equidistant projection
centered on the radar, then back into latitude and longitude. A place
takes the max dBZ in a small window around its pixel.
"""
from __future__ import annotations

import math
from typing import NamedTuple

from PIL import Image

RADAR_LAT, RADAR_LON = 10.9327, 79.8319
R_EARTH = 6371.0

# IMD KKL MAX_Z 16-step dBZ colorbar, sampled from a full-resolution PNG
# frame's own legend (right-hand colorbar). Used to color the rain-table
# cells exactly like the radar image. Sorted high -> low. dBZ steps are
# roughly 2.7 apart, so each step is about a doubling of reflectivity.
# 20 dBZ is the noise floor, 30 starts real rain, 40 is heavy, and past
# 50 means severe cells or hail. Single source: DBZ_STEPS pairs with
# DBZ_COLORS by index; segment() reuses DBZ_STEPS so the legend match
# targets can never drift from the rain-table colors.
DBZ_STEPS: list[float] = [
    60.0, 57.3, 54.7, 52.0, 49.3, 46.7, 44.0, 41.3,
    38.7, 36.0, 33.3, 30.7, 28.0, 25.3, 22.7, 20.0,
]
DBZ_COLORS: list[str] = [
    "#c80039", "#d30d07", "#ff3e1f", "#ff3a00",
    "#ff4000", "#ff7d00", "#ffb600", "#ffde00",
    "#fff6a6", "#c3efff", "#40c1ff", "#1499ff",
    "#006dff", "#002ef8", "#0301c7", "#3600a2",
]
DBZ_LUT: list[tuple[float, str]] = list(zip(DBZ_STEPS, DBZ_COLORS))

# Legend geometry: y of each colorband center in a 720px-tall frame.
LEGEND_BAND_YS_720 = (379, 394, 409, 424, 439, 454, 469, 484, 497, 509,
                      524, 539, 554, 569, 584, 599)

# dBZ category thresholds, high -> low. Single source for category_of()
# and the hourly_rain SQL CASE in kkl_db (via its category_case()).
CATEGORIES: list[tuple[float, str]] = [
    (50.0, "very heavy (>50 dBZ)"),
    (40.0, "heavy (40-50 dBZ)"),
    (30.0, "moderate (30-40 dBZ)"),
    (20.0, "light (20-30 dBZ)"),
]
DRY_LABEL = "no echo (<20 dBZ)"
OUT_OF_RANGE_LABEL = "out of range"


class FrameGeom(NamedTuple):
    """Fitted geometry of one PPI frame.

    x0, y0, x1, y1 bound the PPI crop inside the full image. cx and cy
    are the radar center in crop pixels. km_per_px is the ground scale.
    method records how the fit was found: ring-labels from a clean fit,
    ring-labels-fallback when labels were buried, stored when reused
    from an earlier ingest.
    """
    x0: int
    y0: int
    x1: int
    y1: int
    cx: float
    cy: float
    km_per_px: float
    method: str

    @property
    def r250_px(self) -> float:
        """The 250 km radar range ring, in pixels."""
        return 250.0 / self.km_per_px


class Reading(NamedTuple):
    """One place's rain values for one frame.

    dist_km is the ground distance from the radar. max_dbz is the
    strongest echo in the place window, or None when the window is dry.
    cover_pct is the echo share of the window in percent. nearest_echo_km
    is the distance to the closest echo pixel anywhere, or 999.9 when
    the frame holds no echo at all. category is the human label.
    """
    dist_km: float
    max_dbz: float | None
    cover_pct: float
    nearest_echo_km: float
    category: str


def dbz_to_color(max_dbz: float | None) -> str | None:
    """Nearest radar-legend color for a dBZ value; None when no echo.

    Picks the legend step with the smallest dBZ gap, so a table cell
    wears the same color the radar image paints for that value.
    """
    if max_dbz is None:
        return None
    return min(DBZ_LUT, key=lambda kv: abs(kv[0] - max_dbz))[1]


def category_of(max_dbz: float | None, in_range: bool) -> str:
    """Human label for a dBZ value.

    Walks CATEGORIES from the top, so the first threshold the value
    reaches names it. None means no echo in the window: dry inside the
    250 km range, out of range beyond it.
    """
    if max_dbz is None:
        return OUT_OF_RANGE_LABEL if not in_range else DRY_LABEL
    for thresh, label in CATEGORIES:
        if max_dbz >= thresh:
            return label
    return CATEGORIES[-1][1]


def latlon_to_km(lat, lon, lat0, lon0) -> tuple[float, float]:
    """East and north kilometers of a point relative to a reference.

    Uses the spherical azimuthal equidistant projection: the angle c
    between the points comes from the spherical law of cosines, and the
    factor k = c / sin(c) stretches local offsets so distances from the
    reference stay exact in every direction. Returns (east, north).
    """
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

    Returns a FrameGeom. The radar draws a 200 km range ring with six
    orange "200" tags around it. Orange pixels are thresholded, grouped
    into clusters, and averaged into tag centers. Their centroid is the
    radar center and their mean radius is 200 km, which sets the ground
    scale. Heavy echoes can bury a label; then reuse the caller-supplied
    fallback geometry instead of failing the run.
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
        return FrameGeom(x0, y0, x1, y1, cx, cy, 200.0 / r200, "ring-labels")
    if fallback is None:
        raise ValueError(f"only {len(centers)} of 6 '200' labels visible "
                         "and no fallback geometry available")
    print(f"warn: only {len(centers)}/6 ring labels visible, "
          f"reusing fallback geometry {fallback}")
    fb_cx, fb_cy, fb_km = fallback
    return FrameGeom(x0, y0, x1, y1, fb_cx, fb_cy, fb_km,
                     "ring-labels-fallback")


def segment(im: Image.Image, cx: float, cy: float, km_per_px: float):
    """Extract echo overlay (RGBA) + per-pixel dBZ using the frame's legend.

    Each legend band is sampled from the frame itself, so the match
    targets track how this exact frame rendered its colors. Every PPI
    pixel takes the dBZ of its nearest legend color in RGB space, and
    pixels farther than that are not echoes. The 250 km circle masks
    everything outside radar range. Dark storm cores match no legend
    color, so pixels that are near-black yet ringed by echo are filled
    back in. Returns (echo mask, dBZ array, legend map, crop box).
    """
    import numpy as np
    W, H = im.size
    cbx = int(round(W * 790 / 880))
    full = np.array(im.convert("RGB")).astype(int)
    LUT = []
    for yref, dbz in zip(LEGEND_BAND_YS_720, DBZ_STEPS):
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


def ov_win(echo, dbz_arr, ix: int, iy: int, h: int):
    """Echo stats for the window of half-width h around one pixel.

    A place is a few pixels wide on the PPI, and one pixel can glitch,
    so the reading covers a small neighborhood. cover is the echo share
    of the window. max is the strongest dBZ inside it, or None when the
    window holds no echo.
    """
    m = echo[max(0, iy - h):iy + h + 1, max(0, ix - h):ix + h + 1]
    cover = float(m.mean())
    if not m.any():
        return None, cover
    return float(dbz_arr[max(0, iy - h):iy + h + 1,
                         max(0, ix - h):ix + h + 1][m].max()), cover


def grid_latlon(ph: int, pw: int, cx: float, cy: float, km_per_px: float):
    """Lat/lon grids by azimuthal equidistant projection about the radar.

    This inverts latlon_to_km: pixel offsets become east and north
    kilometers, then spherical trigonometry around the radar position
    turns each offset into a latitude and longitude. The two grids let
    reading_for() measure the distance from any place to every echo
    pixel with plain array math.
    """
    import numpy as np
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
    return LAT, LON


class RainFinder:
    """Rain-finding logic for one radar frame.

    Built from an image via from_image(), or geometry-only via
    echo_free() for echo-free frames that need no image work. Query per
    place with reading_for(). The lat/lon grids are precomputed once per
    frame, not once per place, because every place shares them.
    """

    def __init__(self, geom: FrameGeom, echo, dbz_arr, window_px: int = 5):
        """Bind a frame's geometry to its echo grids.

        Accepts tiny synthetic arrays directly, which is how the unit
        tests build frames without image files. echo None means an
        echo-free frame: no grids are built and every query is dry.
        """
        self.geom = geom
        self.window_px = window_px
        self._h = window_px // 2
        self._echo = echo
        self._dbz = dbz_arr
        if echo is None:
            self._eys = self._exs = None
            self._LAT = self._LON = None
        else:
            import numpy as np
            ph, pw = echo.shape
            self._LAT, self._LON = grid_latlon(
                ph, pw, geom.cx, geom.cy, geom.km_per_px)
            self._eys, self._exs = np.where(echo)

    @classmethod
    def from_image(cls, im: Image.Image, geom: FrameGeom,
                   window_px: int = 5) -> RainFinder:
        """Analyze a radar image with already-fitted geometry.

        Runs the legend segmentation for this frame, then precomputes
        the shared grids. Fit the geometry with calibrate() first.
        """
        echo, dbz_arr, _, _ = segment(im, geom.cx, geom.cy, geom.km_per_px)
        return cls(geom, echo, dbz_arr, window_px)

    @classmethod
    def echo_free(cls, geom: FrameGeom, window_px: int = 5) -> RainFinder:
        """Geometry-only finder for a frame with no echoes.

        Skips the image entirely: dry rows need only the place distance
        and the range check, so segmentation and grids are pure cost.
        """
        return cls(geom, None, None, window_px)

    @property
    def echo_pixels(self) -> int:
        """Count of echo pixels in the frame; 0 when echo-free."""
        if self._echo is None:
            return 0
        return int(self._echo.sum())

    @property
    def max_dbz_frame(self) -> float | None:
        """Strongest echo in the frame; None when the frame is dry."""
        if self._echo is None or not self._echo.any():
            return None
        return float(self._dbz[self._echo].max())

    def reading_for(self, lat: float, lon: float) -> Reading:
        """One place's reading for this frame.

        Projects the place into crop pixels with latlon_to_km. Outside
        the 250 km circle the answer is always out of range. Inside, the
        window stats come from ov_win() and the nearest echo comes from
        the precomputed grids, measured flat with a cosine correction
        for the converging meridians. Echo-free finders skip the pixel
        work and return dry rows.
        """
        import numpy as np
        ek, nk = latlon_to_km(lat, lon, RADAR_LAT, RADAR_LON)
        dist_km = math.hypot(ek, nk)
        px = self.geom.cx + ek / self.geom.km_per_px
        py = self.geom.cy - nk / self.geom.km_per_px
        in_range = (math.hypot(px - self.geom.cx, py - self.geom.cy)
                    <= self.geom.r250_px + 2)
        max_dbz, cover, nearest = None, 0.0, 999.9
        if self._echo is not None:
            ph, pw = self._echo.shape
            if in_range and 0 <= int(round(px)) < pw and 0 <= int(round(py)) < ph:
                max_dbz, cover = ov_win(self._echo, self._dbz,
                                        int(round(px)), int(round(py)), self._h)
            drow = np.hypot((self._LAT[self._eys, self._exs] - lat) * 111.0,
                            (self._LON[self._eys, self._exs] - lon) * 111.0
                            * math.cos(math.radians(lat)))
            nearest = round(float(drow.min()), 1) if drow.size else 999.9
        return Reading(round(dist_km, 1), max_dbz, round(cover * 100, 1),
                       nearest, category_of(max_dbz, in_range))
