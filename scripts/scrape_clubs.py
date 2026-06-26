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


def already_scraped_draw_ids() -> set[str]:
    """Draw IDs we already have from seed data or prior scrape runs."""
    ids: set[str] = set()
    # From seed data
    for path in SEED_DIR.glob("*.js"):
        data = load_results(path)
        for comp in data.get("competitions", []):
            ids.add(comp["id"].lower())
    # From previously scraped draws
    for path in DRAWS_DIR.glob("*.json"):
        ids.add(path.stem.lower())
    return ids


def accept_cookies(page):
    for sel in [
        'button:has-text("Accept all")',
        'button:has-text("Accept All")',
        '#cookiescript_accept',
        '[aria-label*="accept" i]',
        'button:has-text("Accept")',
    ]:
        try:
            el = page.query_selector(sel)
            if el:
                el.click(timeout=2000)
                page.wait_for_timeout(800)
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


def find_draw_links(page, club_url: str) -> list[dict]:
    """Navigate to a club's group page and return [{href, text}] for all /draw/ links."""
    for attempt in range(1, 4):
        try:
            page.goto(club_url, wait_until="networkidle", timeout=60000)
            accept_cookies(page)
            page.goto(club_url, wait_until="networkidle", timeout=60000)
            page.wait_for_selector('a[href*="/draw/"]', timeout=12000)
            page.wait_for_timeout(600)
            links = page.eval_on_selector_all(
                'a[href*="/draw/"]',
                "els => els.map(a => ({href: a.href, text: a.textContent.trim()}))"
            )
            unique = {l["href"]: l for l in links}
            return list(unique.values())
        except Exception as e:
            print(f"  attempt {attempt}/3 failed for {club_url}: {e}", file=sys.stderr)
            page.wait_for_timeout(900 * attempt)
    return []


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
                "drawId": draw_id,
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

    known_ids = already_scraped_draw_ids()
    print(f"Already-known draw IDs: {len(known_ids)}")
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
        cookies_accepted = False

        for club_slug, group_url in registry.items():
            print(f"\n[{club_slug}] {group_url}")
            draw_links = find_draw_links(page, group_url)
            if not draw_links:
                print(f"  No draw links found — skipping")
                continue

            cookies_accepted = True
            new_draws = [
                l for l in draw_links
                if (m := re.search(r"/draw/(\w+)", l["href"], re.I)) and m.group(1).lower() not in known_ids
            ]
            print(f"  {len(draw_links)} draws total, {len(new_draws)} new")

            for dl in new_draws:
                m = re.search(r"/league/([^/]+)/draw/(\w+)", dl["href"], re.I)
                if not m:
                    continue
                comp_id, draw_id = m.group(1).lower(), m.group(2).lower()
                comp_name = dl.get("text", comp_id)

                print(f"  → scraping draw {draw_id} …", end=" ", flush=True)
                if DRY_RUN:
                    print("(dry run)")
                    continue

                page.wait_for_timeout(DELAY_MS)
                result = scrape_draw(page, dl["href"], draw_id, comp_id, comp_name)
                if result:
                    out = DRAWS_DIR / f"{draw_id}.json"
                    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
                    known_ids.add(draw_id)
                    print(f"✓ ({len(result['standings'])} clubs, {len(result['matches'])} matches)")
                else:
                    print("✗ failed")

                page.wait_for_timeout(DELAY_MS)

        browser.close()

    print(f"\nDone. Scraped draws stored in {DRAWS_DIR}")


if __name__ == "__main__":
    main()
