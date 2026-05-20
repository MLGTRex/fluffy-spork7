# Stage 6 UIs

Two static UIs share this directory:

```
pipeline tools/ui/
├── public/   → deployed to https://mlgtrex.github.io/fluffy-spork7/
└── admin/    → deployed to https://mlgtrex.github.io/fluffy-spork7/admin/
```

## Public UI (`public/`)

Shareable single-page overview of the pipeline. Two sections: a narrative
explaining how the six stages work, and live performance vs the S&P 500 (SPY)
benchmark. Reads **only** `data/public_summary.json` — no tickers, allocations,
theses, prediction-log entries or sector exposure are emitted to the public
data file.

## Admin UI (`admin/`)

Full operator dashboard: portfolio overview, positions table, per-ticker
dossiers (Stage 1 scores → Stage 2 debate → Stage 3 scenarios → Stage 4
candidate summary + status → live Alpaca position → history), forecast
accuracy, history timeline, snapshot index. Identical to the prior
top-level UI; only the location and the password gate changed.

Access is gated by a JS prompt for the password `12345`. This is **URL-only
obscurity, not real security** — the underlying data files at
`/admin/data/...` are still publicly fetchable by anyone who knows the URL.
Suitable only for low-stakes "don't show this to the wrong person at the
first click" gating.

## Local preview

From the repo root, with Stage 6 having already produced output:

```bash
python3 -m http.server 8000
```

Public UI:
```
http://localhost:8000/pipeline%20tools/ui/public/index.html?dataRoot=../../../Stage%206%20DRAFT/output
```

Admin UI:
```
http://localhost:8000/pipeline%20tools/ui/admin/index.html?dataRoot=../../../Stage%206%20DRAFT/output
```

The `?dataRoot=…` query param overrides the default `./data/` lookup so the
UI can be browsed in-place against the repo's Stage 6 output folder without
the deploy assembling a `_site/`.

## Deploy

`.github/workflows/pages.yml` assembles `_site/` from `public/` (at root) and
`admin/` (under `/admin/`), copies `Stage 6 DRAFT/output/public_summary.json`
to `_site/data/`, and the full Stage 6 output set to `_site/admin/data/`.
Triggers on every Stage 6 workflow completion, on direct edits to the UI or
Stage 6 output paths, and via manual `workflow_dispatch`.
