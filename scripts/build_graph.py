"""
Reads results.js files from the psc-tennis repo and builds a club-to-club
graph in node-link JSON format, output to docs/graph.json.

Edge unit: one edge per (clubA, clubB, competitionId, divisionName, season, period).
WinsLeft/WinsRight are from the alphabetically-first slug's perspective.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
SEED_DIR = ROOT / "data" / "seed"
DRAWS_DIR = ROOT / "data" / "scraped" / "draws"

# Seed results: prefer data/seed/ (CI + local after sync), fall back to sibling repo for local dev
def _seed_files() -> list[Path]:
    seeded = list(SEED_DIR.glob("*.js"))
    if seeded:
        return seeded
    fallback = ROOT.parent / "psc-tennis"
    return [
        fallback / "clubs" / "cltc" / "data" / "results.js",
        fallback / "clubs" / "psc" / "data" / "results.js",
    ]

SEED_CLUBS = {"cltc", "psc"}

# ---------------------------------------------------------------------------
# Club name aliases → canonical slug
# ---------------------------------------------------------------------------
CLUB_ALIASES: dict[str, str] = {
    "cumberland lawn tennis club & hampstead cricket": "cltc",
    "cumberland lawn tennis club": "cltc",
    "cumberland": "cltc",
    "paddington sports club": "psc",
    "paddington sc": "psc",
}

CANONICAL_NAMES: dict[str, str] = {
    "cltc": "Cumberland LTC",
    "psc": "Paddington SC",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_results(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    raw = re.sub(r"^window\.__RESULTS__\s*=\s*", "", raw.strip())
    raw = raw.rstrip(";").strip()
    return json.loads(raw)


def strip_team_number(name: str) -> str:
    return re.sub(r"\s+\d+$", "", name).strip()


def slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def club_slug(raw_name: str) -> str:
    stripped = strip_team_number(raw_name).lower()
    if stripped in CLUB_ALIASES:
        return CLUB_ALIASES[stripped]
    slug = slugify(stripped)
    if slug not in CANONICAL_NAMES:
        CANONICAL_NAMES[slug] = strip_team_number(raw_name)
    return slug


def parse_division(div_str: str) -> tuple[str | None, str | None]:
    """Return (tier, zone) from division string like 'East Premier' or 'North-East Division 2'."""
    if not div_str:
        return None, None
    is_top = bool(re.search(r"premier|intermediate", div_str, re.I))
    is_lower = bool(re.search(r"division", div_str, re.I))
    tier = "top" if is_top else ("lower" if is_lower else None)
    m = re.search(r"\b(North[\s-]East|North[\s-]West|East|West)\b", div_str, re.I)
    zone = None
    if m:
        raw_zone = re.sub(r"\s+", "-", m.group(1)).title()
        zone = {"North-East": "North-East", "North-West": "North-West", "East": "East", "West": "West"}.get(raw_zone, raw_zone)
    return tier, zone


def parse_period(comp_name: str) -> str:
    return "winter" if re.search(r"winter", comp_name, re.I) else "summer"


# ---------------------------------------------------------------------------
# Accumulation
# ---------------------------------------------------------------------------

nodes: dict[str, dict] = {}   # slug → node dict
edges: dict[str, dict] = {}   # edge key → edge dict


def ensure_node(slug: str) -> dict:
    if slug not in nodes:
        nodes[slug] = {
            "id": slug,
            "name": CANONICAL_NAMES.get(slug, slug),
            "zones": set(),
            "seedClub": slug in SEED_CLUBS,
        }
    return nodes[slug]


def record_edge(
    *,
    slug_a: str,
    slug_b: str,
    comp_id: str,
    comp_name: str,
    div_name: str,
    season: str,
    period: str,
    zone: str | None,
    tier: str | None,
    home_slug: str,
    hs,
    as_,
) -> None:
    left, right = sorted([slug_a, slug_b])
    key = "|".join([left, right, comp_id, div_name, season, period])

    if key not in edges:
        edges[key] = {
            "source": left,
            "target": right,
            "competitionId": comp_id,
            "competitionName": comp_name,
            "divisionName": div_name,
            "season": season,
            "period": period,
            "zone": zone,
            "tier": tier,
            "winsLeft": 0,
            "draws": 0,
            "winsRight": 0,
        }

    if hs is None or as_ is None:
        return

    edge = edges[key]
    home_is_left = home_slug == left

    if hs > as_:
        if home_is_left:
            edge["winsLeft"] += 1
        else:
            edge["winsRight"] += 1
    elif as_ > hs:
        if home_is_left:
            edge["winsRight"] += 1
        else:
            edge["winsLeft"] += 1
    else:
        edge["draws"] += 1


# ---------------------------------------------------------------------------
# Main — ingest seed data (results.js format)
# ---------------------------------------------------------------------------

def ingest_results_js(data: dict) -> None:
    season = data["season"]
    for comp in data.get("competitions", []):
        period = parse_period(comp["name"])
        for team in comp.get("teams", []):
            tier, zone = parse_division(team.get("division", ""))
            seed_slug = club_slug(team["pscName"])
            seed_node = ensure_node(seed_slug)
            if zone:
                seed_node["zones"].add(zone)
            for row in team.get("standings", []):
                slug = club_slug(row["name"])
                n = ensure_node(slug)
                if zone:
                    n["zones"].add(zone)
            for match in team.get("matches", []):
                home_slug = club_slug(match["home"])
                away_slug = club_slug(match["away"])
                if home_slug == away_slug:
                    continue
                ensure_node(home_slug)
                ensure_node(away_slug)
                record_edge(
                    slug_a=home_slug,
                    slug_b=away_slug,
                    comp_id=comp["id"],
                    comp_name=comp["name"],
                    div_name=team["name"],
                    season=season,
                    period=period,
                    zone=zone,
                    tier=tier,
                    home_slug=home_slug,
                    hs=match.get("hs"),
                    as_=match.get("as"),
                )


def _parse_scraped_match(match: dict) -> tuple[str, str, int | None, int | None] | None:
    """
    The scraper's DOM selectors capture the full match block as text in the 'away' field:
    'HomeTeam\nHS\n-\nAS\nAwayTeam'  (5 parts after splitting on newline)
    Returns (home_name, away_name, hs, as_) or None if unparseable.
    """
    raw = match.get("away", "")
    parts = [p.strip() for p in raw.split("\n") if p.strip()]
    if len(parts) >= 5:
        home = parts[0]
        away = parts[-1]
        try:
            hs: int | None = int(parts[1])
            as_: int | None = int(parts[3])
        except (ValueError, IndexError):
            hs = as_ = None
        return home, away, hs, as_
    if len(parts) == 3:
        home, score_block, away = parts
        m = re.match(r"(\d+)\s*-\s*(\d+)", score_block)
        if m:
            return home, away, int(m.group(1)), int(m.group(2))
    return None


def ingest_scraped_draw(draw: dict) -> None:
    """Ingest a scraped draw JSON (produced by scrape_clubs.py)."""
    comp_id = draw["competitionId"]
    # competitionName = draw-link text, e.g. "Mens Doubles \n ⬤ North West Division 1 ⬤ Group 1"
    # Strip non-ASCII bullet chars then parse zone/tier from what remains.
    raw_comp = re.sub(r"[^\x00-\x7F]", " ", draw.get("competitionName", ""))
    raw_comp = re.sub(r"\s+", " ", raw_comp).strip()
    tier, zone = parse_division(raw_comp)
    season = _extract_season(raw_comp)
    period = parse_period(raw_comp)

    # Fall back to year from match dates if season not in competition name
    if season == "unknown":
        for match in draw.get("matches", []):
            date_str = match.get("date", "") or match.get("home", "")
            yr = re.search(r"\b(20\d\d)\b", date_str)
            if yr:
                season = yr.group(1)
                break

    for match in draw.get("matches", []):
        parsed = _parse_scraped_match(match)
        if not parsed:
            continue
        home_name, away_name, hs, as_ = parsed
        home_slug = club_slug(home_name)
        away_slug = club_slug(away_name)
        if not home_name or not away_name or home_slug == away_slug:
            continue
        ensure_node(home_slug)
        ensure_node(away_slug)
        if zone:
            nodes[home_slug]["zones"].add(zone)
            nodes[away_slug]["zones"].add(zone)
        record_edge(
            slug_a=home_slug,
            slug_b=away_slug,
            comp_id=comp_id,
            comp_name=raw_comp,
            div_name=raw_comp,
            season=season,
            period=period,
            zone=zone,
            tier=tier,
            home_slug=home_slug,
            hs=hs,
            as_=as_,
        )


def _extract_season(comp_name: str) -> str:
    m = re.search(r"\b(20\d\d)\b", comp_name)
    return m.group(1) if m else "unknown"


# Seed sources
for source_path in _seed_files():
    if not source_path.exists():
        print(f"WARNING: {source_path} not found — skipping", file=sys.stderr)
        continue
    print(f"Ingesting seed: {source_path.name}")
    ingest_results_js(load_results(source_path))

# Additional scraped draws
scraped_count = 0
for draw_path in sorted(DRAWS_DIR.glob("*.json")):
    draw = json.loads(draw_path.read_text(encoding="utf-8"))
    ingest_scraped_draw(draw)
    scraped_count += 1
if scraped_count:
    print(f"Ingested {scraped_count} additional scraped draws")

# ---------------------------------------------------------------------------
# Serialise
# ---------------------------------------------------------------------------

def _primary_zone(zones: set) -> str | None:
    """Pick a single zone label from a node's zone set (alphabetically first for stability)."""
    return sorted(zones)[0] if zones else None


node_list = [
    {
        **{k: v for k, v in n.items() if k != "zones"},
        "zones": sorted(n["zones"]),
        "zone": _primary_zone(n["zones"]),
    }
    for n in nodes.values()
]
link_list = list(edges.values())

seasons = sorted({l["season"] for l in link_list})
periods = sorted({l["period"] for l in link_list})
zones = sorted({l["zone"] for l in link_list if l["zone"]})

graph = {
    "meta": {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "seedClubs": sorted(SEED_CLUBS),
        "seasons": seasons,
        "note": "Ego-network graph: seed clubs CLTC + PSC plus Level-1 clubs from data/scraped/draws/. Edge = one per (club-pair × competition × division × season × period).",
    },
    "nodes": node_list,
    "links": link_list,
}

out_path = ROOT / "docs" / "graph.json"
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")

print(f"Graph written to {out_path}")
print(f"  Nodes : {len(node_list)}")
print(f"  Links : {len(link_list)}")
print(f"  Seasons: {', '.join(seasons)}  |  Periods: {', '.join(periods)}")
print(f"  Zones : {', '.join(zones)}")
