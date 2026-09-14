# KKL Karaikal Radar — hourly archive + day-wise gallery (free on GitHub)

Source: `https://mausam.imd.gov.in/Radar/animation/Converted/KKL_MAXZ.gif` (animated, ~3h history).

## How it works
- `.github/workflows/archive.yml` runs hourly (`15 * * * *`) + manual `Run workflow`.
- `scripts/fetch_kkl.py` downloads the GIF, decodes it in memory, saves per-snapshot
  `_last.png` (last frame) + extracted `frames/HHMMSS_NNN.png` (full-resolution,
  lossless PNG), rebuilds `daily.gif`
  + `strip.png` for that IST day, and updates `docs/data/index.json`.
  Raw `*.gif` snapshots are never committed — they are discarded after extraction.
- `docs/index.html` is the day-wise gallery (viewer + scrollable storyboard timeline). Enable **Settings → Pages → Deploy from branch → `main` → `/docs`**.
- Per-frame PNGs older than 30 days are pruned from the working tree (keeps `daily.gif`/`strip.png`/`*_last.png` forever). Tweak `RETENTION_DAYS` in workflow.

## Run locally
```
pip install -r requirements.txt
python scripts/fetch_kkl.py --test   # dry run to /tmp/opencode/kkl_test
python scripts/fetch_kkl.py          # real save into docs/
python -m http.server -d docs 8000   # view at http://localhost:8000/?date=YYYY-MM-DD
```

## Notes / limits
- GitHub Actions cron can lag 5–15 min and pauses after 60 days of repo inactivity — any commit re-enables; keep `workflow_dispatch` for manual runs.
- Repo size: pruning deletes old files from the working tree, but git history keeps every committed blob. Raw GIFs (~1.7 MB × 8/day ≈ 400 MB/mo) are never committed — only extracted PNGs + daily summaries — so growth is far slower than before. Full-resolution PNGs are larger than the old JPGs, so watch repo size. If the clone still gets heavy, squash history, e.g.:
  ```
  git filter-repo --path docs/archive --invert-paths --path-glob '*-IST.gif' --use-base-name --force
  # or: move archives to an orphan branch, or stop committing raw *.gif
  # (keep only *_last.png + daily.gif/strip.png/frames).
  ```
  If `filter-repo` is unavailable: `git filter-branch` equivalent, or start a fresh orphan `archive` branch for old days.
