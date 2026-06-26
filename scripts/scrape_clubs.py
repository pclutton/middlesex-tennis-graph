"""
Weekly scraper: for each club in data/clubs-registry.json, navigate to their
LTA association/group page, discover all their division draw URLs, then scrape
any draws not already present in data/scraped/draws/{drawId}.json.

This means we never re-fetch a draw we already have, limiting LTA site load.
New draws only appear at the start of a season or if a club enters a new
competition — typically rare mid-season.

Usage:
    uv run python scripts/scrape_clubs.py            # scrape all registered clubs
    uv run python scripts/scrape_clubs.py --dry-run  # show what would be fetched
    uv run python scripts/scrape_clubs.py --debug    # save HTML snapshots to data/debug/
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
REGISTRY_PATH = ROOT / "data" / "clubs-registry.json"
DRAWS_DIR = ROOT / "data" / "scraped" / "draws"
DEBUG_DIR = ROOT / "data" / "debug"
SEED_DIR = ROOT / "data" / "seed"

DRY_RUN = "--dry-run" in sys.argv
DEBUG = "--debug" in sys.argv
DELAY_MS = 2500  # polite delay between requests


def load_results(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    raw = re.sub(r"^window\.__RESULTS__\s*=\s*", "", raw.strip()).rstrip(";").strip()
    return json.loads(raw)


def seed_files() -> list[Path]:
    seeded = list(SEED_DIR.glob("*.js"))
    if seeded:
        return seeded
    fallback = ROOT.parent / "psc-tennis"
    return [
        fallback / "clubs" / "cltc" / "data" / "results.js",
        fallback / "clubs" / "psc" / "data" / "results.js",
    ]


def already_scraped_keys() -> tuple[set[str], set[str]]:
    """
    Return (scraped_file_keys, seed_comp_ids).
    Seed competition IDs are treated as fully covered — skip any draw in those comps.
    Scraped file keys are '{comp_id}-{draw_id}' stems from data/scraped/draws/.
    """
    seed_comp_ids: set[str] = set()
    for path in seed_files():
        if not path.exists():
            continue
        data = load_results(path)
        for comp in data.get("competitions", []):
            seed_comp_ids.add(comp["id"].lower())
    keys: set[str] = {path.stem.lower() for path in DRAWS_DIR.glob("*.json")}
    return keys, seed_comp_ids


def accept_cookies(page):
    for sel in [
        'button:has-text("Accept all")',
        'button:has-text("Accept All")',
        '#cookiescript_accept',
        '[aria-label*="accept" i]',
        'button:has-text("Accept")',
        '.js-simple-accept-view button[type="submit"]',
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=2000)
                page.wait_for_load_state("networkidle", timeout=15000)
                return
        except Exception:
            pass
    try:
        page.evaluate("""
            const form = document.querySelector('form[action*="cookiewall"]');
            if (form) form.submit();
        """)
        page.wait_for_load_state("networkidle", timeout=15000)
        return
    except Exception:
        pass
    try:
        page.context.add_cookies([{
            "name": "CookieScriptConsent",
            "value": '{"action":"accept"}',
            "domain": ".lta.org.uk",
            "path": "/",
        }])
    except Exception:
        pass


def navigate_past_cookiewall(page, url: str):
    """Navigate to url, accepting cookie wall if encountered."""
    page.goto(url, wait_until="networkidle", timeout=60000)
    if "cookiewall" in page.url:
        accept_cookies(page)
        if "cookiewall" in page.url:
            page.goto(url, wait_until="networkidle", timeout=60000)


def find_draw_links(page, club_url: str) -> list[dict]:
    """
    Two-hop: navigate to the club group page, follow each /league/.../club/N link
    to the comp-club page, collect all /draw/ links from there.
    """
    try:
        navigate_past_cookiewall(page, club_url)
        if DEBUG:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            slug = club_url.rstrip("/").split("/")[-1][:12]
            (DEBUG_DIR / f"group-{slug}.html").write_text(page.content(), encoding="utf-8")

        comp_club_links = page.eval_on_selector_all(
            'a[href*="/league/"][href*="/club/"]',
            "els => els.map(a => a.href)"
        )
        # Filter: only /league/{guid}/club/{n} (not sub-pages like /club/N/Index/*)
        comp_club_links = [
            h for h in comp_club_links
            if re.search(r"/league/[^/]+/club/\d+$", h)
        ]
        comp_club_links = list(dict.fromkeys(comp_club_links))  # dedupe preserving order
    except Exception as e:
        print(f"  group page failed for {club_url}: {e}", file=sys.stderr)
        return []

    all_draw_links: dict[str, dict] = {}
    for cc_url in comp_club_links:
        try:
            page.wait_for_timeout(1000)
            navigate_past_cookiewall(page, cc_url)
            links = page.eval_on_selector_all(
                'a[href*="/draw/"]',
                "els => els.map(a => ({href: a.href, text: a.textContent.trim()}))"
            )
            for l in links:
                if re.search(r"/league/[^/]+/draw/\d+$", l["href"]):
                    all_draw_links[l["href"]] = l
        except Exception as e:
            print(f"  comp-club page failed {cc_url}: {e}", file=sys.stderr)

    return list(all_draw_links.values())


def scrape_draw(page, draw_url: str, draw_id: str, comp_id: str, comp_name: str) -> dict | None:
    """
    Scrape a single draw page: full standings table + all match results.
    Returns a dict matching the same shape as results.js competition teams,
    or None on failure.
    """
    for attempt in range(1, 4):
        try:
            page.goto(draw_url, wait_until="networkidle", timeout=60000)
            accept_cookies(page)
            page.wait_for_selector("table", timeout=8000)
            page.wait_for_timeout(600)
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_selector(".match--team-match", timeout=6000)
            page.wait_for_timeout(500)

            if DEBUG:
                DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                (DEBUG_DIR / f"draw-{draw_id}.html").write_text(page.content(), encoding="utf-8")

            # --- standings ---
            standings = []
            rows = page.query_selector_all("table tbody tr")
            for row in rows:
                cells = [c.inner_text().strip() for c in row.query_selector_all("td")]
                if len(cells) < 4:
                    continue
                # Typical columns: rank, name, played, won, drawn, lost, rubbers, points
                try:
                    standings.append({
                        "rank": int(cells[0]) if cells[0].isdigit() else len(standings) + 1,
                        "name": cells[1],
                        "played": _int(cells[2]),
                        "won": _int(cells[3]),
                        "drawn": _int(cells[4]) if len(cells) > 4 else 0,
                        "lost": _int(cells[5]) if len(cells) > 5 else 0,
                        "rubbers": cells[6] if len(cells) > 6 else "",
                        "points": _int(cells[7]) if len(cells) > 7 else 0,
                    })
                except (ValueError, IndexError):
                    continue

            # --- matches ---
            matches = []
            match_els = page.query_selector_all(".match--team-match")
            for el in match_els:
                text = el.inner_text()
                # Pattern: "Team A  score - score  Team B  date"
                # Try to extract via aria or structured sub-elements first
                teams = el.query_selector_all(".match__team-name, .team-name, [class*='team']")
                scores = el.query_selector_all(".match__score, [class*='score']")
                date_el = el.query_selector(".match__date, [class*='date'], time")

                home = teams[0].inner_text().strip() if len(teams) > 0 else ""
                away = teams[1].inner_text().strip() if len(teams) > 1 else ""
                date_str = date_el.inner_text().strip() if date_el else ""

                hs = as_ = None
                if len(scores) >= 2:
                    hs = _int_or_none(scores[0].inner_text().strip())
                    as_ = _int_or_none(scores[1].inner_text().strip())
                elif len(scores) == 1:
                    parts = re.split(r"\s*[-–]\s*", scores[0].inner_text().strip())
                    if len(parts) == 2:
                        hs, as_ = _int_or_none(parts[0]), _int_or_none(parts[1])

                if home or away:
                    matches.append({
                        "home": home,
                        "away": away,
                        "hs": hs,
                        "as": as_,
                        "date": date_str,
                    })

            # --- division name from page heading ---
            heading = page.query_selector("h1, h2, .draw__title, [class*='heading']")
            div_name = heading.inner_text().strip() if heading else draw_url

            # --- division field (zone/tier) ---
            # Try to find a breadcrumb or subtitle that says e.g. "East Premier"
            division_field = ""
            for sel in [".draw__division", "[class*='division']", ".breadcrumb li:last-child"]:
                el = page.query_selector(sel)
                if el:
                    division_field = el.inner_text().strip()
                    break

            return {
                "drawId": f"{comp_id}-{draw_id}",
                "competitionId": comp_id,
                "competitionName": comp_name,
                "divisionName": div_name,
                "division": division_field,
                "drawUrl": draw_url,
                "scrapedAt": datetime.now(timezone.utc).isoformat(),
                "standings": standings,
                "matches": matches,
            }

        except Exception as e:
            print(f"  draw {draw_id} attempt {attempt}/3 failed: {e}", file=sys.stderr)
            page.wait_for_timeout(900 * attempt)
    return None


def _int(s: str) -> int:
    try:
        return int(re.sub(r"[^\d]", "", s) or "0")
    except ValueError:
        return 0


def _int_or_none(s: str) -> int | None:
    s = re.sub(r"[^\d]", "", s)
    return int(s) if s else None


def main():
    registry: dict[str, str] = json.loads(REGISTRY_PATH.read_text())
    if not registry:
        print("clubs-registry.json is empty. Run discover_clubs.py first.")
        sys.exit(1)

    known_keys, seed_comp_ids = already_scraped_keys()
    print(f"Already-scraped draw keys: {len(known_keys)}")
    print(f"Seed competition IDs (skipped): {len(seed_comp_ids)}")
    print(f"Clubs in registry: {len(registry)}")
    DRAWS_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ))
        page = ctx.new_page()

        for club_slug, group_url in registry.items():
            print(f"\n[{club_slug}] {group_url}")
            draw_links = find_draw_links(page, group_url)
            if not draw_links:
                print(f"  No draw links found — skipping")
                continue

            new_draws = []
            for l in draw_links:
                m = re.search(r"/league/([^/]+)/draw/(\w+)", l["href"], re.I)
                if not m:
                    continue
                comp_id, draw_id = m.group(1).lower(), m.group(2).lower()
                file_key = f"{comp_id}-{draw_id}"
                if comp_id in seed_comp_ids or file_key in known_keys:
                    continue
                new_draws.append((l, comp_id, draw_id, file_key))

            print(f"  {len(draw_links)} draws total, {len(new_draws)} new")

            for dl, comp_id, draw_id, file_key in new_draws:
                comp_name = dl.get("text", comp_id)

                print(f"  scraping {file_key} ...", end=" ", flush=True)
                if DRY_RUN:
                    print("(dry run)")
                    continue

                page.wait_for_timeout(DELAY_MS)
                result = scrape_draw(page, dl["href"], draw_id, comp_id, comp_name)
                if result:
                    out = DRAWS_DIR / f"{file_key}.json"
                    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
                    known_keys.add(file_key)
                    print(f"done ({len(result['standings'])} clubs, {len(result['matches'])} matches)")
                else:
                    print("failed")

                page.wait_for_timeout(DELAY_MS)

        browser.close()

    print(f"\nDone. Scraped draws stored in {DRAWS_DIR}")


if __name__ == "__main__":
    main()
