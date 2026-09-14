# Karaikal Radar Archive 

Archives the IMD Karaikal radar loop, which IMD only keeps ~3h online, into a permanent day-wise (IST) replay gallery.

- Source: `https://mausam.imd.gov.in/Radar/animation/Converted/KKL_MAXZ.gif` (animated, ~3h history)
- Live gallery: `docs/index.html` (served via **Settings → Pages → Deploy from branch → `main` → `/docs`**)
- Covers rain over South India / Northern Sri Lanka. Times everywhere are IST.

## How it works

- `.github/workflows/kkl-cron.yml` runs hourly (`5 * * * *`) + manual `Run workflow` (`workflow_dispatch`).
  Installs `requirements.txt` + `tesseract-ocr`, runs `scripts/fetch_kkl.py`, prunes old per-frame images, rebuilds the index, commits `docs/archive` + `docs/data/index.json` with rebase-retry push.
- `scripts/fetch_kkl.py` downloads the GIF and decodes it **in memory** (raw `*.gif` is never committed):
  - saves per-snapshot `{HHMM}-UTC_{HHMM}-IST_last.png` (last frame) in the snapshot's IST day dir
  - extracts unique frames to `frames/HHMMSS_NNN.png` (original resolution, lossless PNG), filed under **each frame's own clock-derived IST date** (so 22:xx IST frames from a post-midnight snapshot land on the previous day)
  - dedupes via masked hash (timestamp box blacked out), in-GIF repeats, cross-snapshot + previous-day tail, and whole-blob sha (identical re-downloads are index-only noops unless `--force`)
  - timestamps each frame by OCR-ing the clock overlay with Tesseract (cached in `frames.json` as `ocr_v` + `t_utc`; unreadable frames are interpolated at ~10 min cadence, last resort snapshot time; future-dated misreads are clamped back whole days)
  - rebuilds per day: `daily.gif` (chronological animation), `strip.png` (most recent ≤8 `_last.png`, aspect-fit 4-col grid; legacy `strip.jpg` removed), `frames.json` manifest (`hashes`, `frames`, `slots` per 30-min IST slot, `stats`, `blob_shas`, `ocr` cache)
  - rebuilds `docs/data/index.json` (`radar`, `source`, `updated_ist`, per-day `snaps`/`slots`/`frames`/`daily_gif`/`strip`, sorted newest-first)
- `docs/index.html` + `docs/app.js` + `docs/style.css` is the gallery: day stepper + `?date=YYYY-MM-DD` deep link, hero scope viewer, play (900 ms), scrub slider, filmstrip timeline, 24h coverage tick + needle, light/dark Scandinavian theme (persisted), keyboard (space / ←→ frame / shift+←→ day) + touch swipe, lightbox expand. Pruned days fall back to `daily.gif`/`strip.png` instead of "no frames".
- `docs/bg-lab.html` is a prototype-only scope-background lab (10 ideas); nothing live changes until a winner is applied to `style.css`.
- Pruning (same workflow, `RETENTION_DAYS: "30"`): for IST days older than 30d, deletes legacy `*-IST.gif` + the whole `frames/` dir, keeps `daily.gif` / `strip.png` / `*_last.png` / `frames.json`, then re-indexes. Tweak `RETENTION_DAYS` in the workflow env.

## Layout (all under `docs/` so Pages can serve it)

```
docs/archive/YYYY-MM-DD/{HHMM}-UTC_{HHMM}-IST_last.png
docs/archive/YYYY-MM-DD/frames/HHMMSS_NNN.png
docs/archive/YYYY-MM-DD/daily.gif
docs/archive/YYYY-MM-DD/strip.png
docs/archive/YYYY-MM-DD/frames.json
docs/data/index.json
docs/index.html  app.js  style.css  bg-lab.html
```

`requirements.txt`: `requests`, `Pillow`, `pytesseract` (optional — needs the `tesseract-ocr` binary for clock reads; without it all frame times are estimated).

## Run locally

```
pip install -r requirements.txt
sudo apt-get install -y tesseract-ocr   # optional but recommended (frame clock OCR)
python scripts/fetch_kkl.py --test      # dry run to /tmp/opencode/kkl_test, no index rewrite
python scripts/fetch_kkl.py             # real save into docs/
python scripts/fetch_kkl.py --force     # re-ingest even if blob sha already seen
python -m http.server -d docs 8000      # view at http://localhost:8000/?date=YYYY-MM-DD
```

## Notes / limits

- GitHub Actions cron can lag 5–15 min and pauses after 60 days of repo inactivity — any commit re-enables; `workflow_dispatch` stays for manual/backfill runs.
- Estimated times: frames Tesseract can't read are marked `estimated` (amber dot in the gallery) and interpolated — treat slot times as ±10 min in that case.
- Repo size: pruning only shrinks the working tree; git history keeps every committed blob. Raw GIFs are never committed — only extracted PNGs + daily summaries — so growth is far slower than committing GIFs, but full-resolution PNGs are larger than the old JPGs, so watch size. If the clone gets heavy, squash history, e.g.:
  ```
  git filter-repo --path docs/archive --invert-paths --path-glob '*-IST.gif' --use-base-name --force
  # or: move archives to an orphan branch
  # (keep only *_last.png + daily.gif/strip.png/frames).
  ```
  If `filter-repo` is unavailable: `git filter-branch` equivalent, or start a fresh orphan `archive` branch for old days.
