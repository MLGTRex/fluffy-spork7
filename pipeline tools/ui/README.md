# Stage 6 UI

Static dashboard for the JSON Stage 6 writes into `Stage 6 DRAFT/output/`.
Vanilla HTML/CSS/JS — no build step, no npm.

## Local preview

The site fetches data from `./data/` relative to `index.html`. For local
browsing, point it at Stage 6's output directory via the `dataRoot` query
param:

```bash
cd /path/to/fluffy-spork7
python3 -m http.server 8000
```

Then open:

```
http://localhost:8000/pipeline%20tools/ui/index.html?dataRoot=../../Stage%206%20DRAFT/output
```

## Deploy

`.github/workflows/pages.yml` assembles a `_site/` of `pipeline tools/ui/*` +
`Stage 6 DRAFT/output/*` (copied under `_site/data/`) and publishes to
GitHub Pages on every Stage 6 run, push to the UI/output paths on `main`, or
manual dispatch. The published site lives at
`https://mlgtrex.github.io/fluffy-spork7/`.

One-time prereq: **Settings → Pages → Source: GitHub Actions**.
