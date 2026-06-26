# Middlesex Tennis Club Graph

An interactive force-directed graph of club-to-club match relationships across the Middlesex tennis league ecosystem — visualised by zone, tier, and season.

**[View the live graph →](https://pclutton.github.io/middlesex-tennis-graph/)**

## What it shows

Each **node** is a tennis club. Each **edge** is a set of results between two clubs in a specific competition, division, and season. Nodes are coloured by Middlesex league zone:

| Colour | Zone |
|--------|------|
| Red | East |
| Blue | West |
| Orange | North-East |
| Green | North-West |
| White | Seed clubs (CLTC & PSC) |

The graph covers the Middlesex Summer League ego-network centred on two seed clubs — **Cumberland Lawn Tennis Club & Hampstead Cricket (CLTC)** and **Paddington Sports Club (PSC)** — expanded to include all Level-1 clubs that interact with them and the matches between those clubs.

## Zone structure

The Middlesex league has a geographic hierarchy:

```
Premier / Intermediate  →  East  |  West
Division 1 and below   →  East  |  North-East  |  North-West  |  West
```

The September Premier Playoff is the only bridge between the East and West macro-clusters.

## Data pipeline

```
pclutton/lta-club  (live results site)
       │
       │  curl (weekly, via GitHub Actions)
       ▼
data/seed/*.js          ← CLTC + PSC results in results.js format

competitions.lta.org.uk
       │
       │  Playwright headless scrape (weekly, Wednesday 09:00 UTC)
       ▼
data/scraped/draws/     ← one JSON file per {comp_id}-{draw_id}
                          skips draws already present (incremental)

scripts/build_graph.py  ← combines seed + scraped → docs/graph.json

docs/index.html         ← D3 v7 force-directed viz (GitHub Pages)
```

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/scrape_clubs.py` | Weekly scraper — reads `data/clubs-registry.json`, navigates group page → comp-club page → draw pages, saves new draws only |
| `scripts/build_graph.py` | Builds `docs/graph.json` from seed data and scraped draws |
| `scripts/discover_clubs.py` | One-shot discovery — finds LTA `association/group` URLs for all Level-1 clubs and writes `data/clubs-registry.json` |

## Running locally

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
# Install dependencies
uv sync
uv run playwright install chromium

# Scrape latest draws (incremental — skips already-fetched draws)
uv run python scripts/scrape_clubs.py

# Rebuild the graph
uv run python scripts/build_graph.py

# Serve locally
cd docs && python -m http.server 8000
```

Seed data (`data/seed/`) is not committed — it is fetched at build time from [pclutton/lta-club](https://github.com/pclutton/lta-club). Locally it falls back to `../psc-tennis/` if that repo is checked out alongside this one.

## Automated updates

A GitHub Actions workflow (`.github/workflows/update-graph.yml`) runs every Wednesday at 09:00 UTC:

1. Fetches latest seed data from `pclutton/lta-club`
2. Scrapes any new draws from the LTA site (skips draws already in `data/scraped/draws/`)
3. Rebuilds `docs/graph.json`
4. Commits new data and deploys to GitHub Pages

The Wednesday schedule is intentional — mid-week after weekend match results are entered but before the next round begins.

## Repo separation

This repo is **deliberately separate** from [pclutton/lta-club](https://github.com/pclutton/lta-club) (the live club results site). The results site is used by club members; the graph is a personal analytics project. The scrapers must never share code or be merged.

## Current graph stats (Summer 2026)

- **197 nodes** (tennis clubs)
- **2,327 links** (club-pair × competition × division edges)
- **181 scraped draws** across 60 Level-1 clubs
- All four zones populated: East, West, North-East, North-West
