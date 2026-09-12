# KKL Karaikal Radar — 3-hourly archive + day-wise gallery (free on GitHub)

Source: `https://mausam.imd.gov.in/Radar/animation/Converted/KKL_MAXZ.gif` (animated, ~3h history).

## How it works
- `.github/workflows/archive.yml` runs every 3h (`15 */3 * * *` UTC) + manual `Run workflow`.
- `scripts/fetch_kkl.py` downloads the GIF, saves `docs/archive/YYYY-MM-DD/HHMM-UTC_HHMM-IST.gif`
  + `_last.jpg` (last frame), rebuilds `daily.gif` + `strip.jpg` for that IST day,
  and updates `docs/data/index.json`.
- `docs/index.html` is the day-wise gallery. Enable **Settings → Pages → Deploy from branch → `main` → `/docs`**.
- Raw GIFs older than 90 days are pruned (keeps `daily.gif`/`strip.jpg` forever). Tweak in workflow.

## Run locally
```
pip install -r requirements.txt
python scripts/fetch_kkl.py --test   # dry run to /tmp/opencode/kkl_test
python scripts/fetch_kkl.py          # real save into docs/
python -m http.server -d docs 8000   # view at http://localhost:8000/?date=YYYY-MM-DD
```

## Notes / limits
- GitHub Actions cron can lag 5–15 min and pauses after 60 days of repo inactivity — any commit re-enables; keep `workflow_dispatch` for manual runs.
- If repo size becomes an issue (~1.7 MB × 8/day ≈ 400 MB/mo raw), lower retention or keep only `_last.jpg` + daily summaries.
