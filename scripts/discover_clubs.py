"""
One-shot discovery script: finds LTA association/group URLs for every Level-1
club (clubs that appear in seed-club divisions but whose own profile URL is
unknown).

Strategy:
  1. Load all draw URLs from seed data (data/seed/*.js).
  2. For each draw URL not yet mapped in clubs-registry.json, navigate to the
     draw page and look for /association/group/ links in the standings table.
  3. Write any discovered {slug: url} pairs into data/clubs-registry.json.

Run manually when new clubs appear or after a fresh season starts:
    uv run python scripts/discover_clubs.py

The output is clubs-registry.json, which scrape_clubs.py then reads.

If the LTA standings table does NOT link to club profiles (check the DEBUG
output in data/debug/ to verify), entries must be added manually in this form:
    {
      "chandos-ltc": "https://competitions.lta.org.uk/association/group/{GUID}"
    }
"""

import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
_SEED_DIR = ROOT / "data" / "seed"
REGISTRY_PATH = ROOT / "data" / "clubs-registry.json"
DEBUG_DIR = ROOT / "data" / "debug"


def seed_files() -> list[Path]:
    seeded = list(_SEED_DIR.glob("*.js"))
    if seeded:
        return seeded
    fallback = ROOT.parent / "psc-tennis"
    return [
        fallback / "clubs" / "cltc" / "data" / "results.js",
        fallback / "clubs" / "psc" / "data" / "results.js",
    ]

# Already-known club → group URL (seed clubs, always excluded from discovery)
SEED_SLUGS = {"cltc", "psc"}

LTA_BASE = "https://competitions.lta.org.uk"
CLUB_ALIASES = {
    "cumberland lawn tennis club & hampstead cricket": "cltc",
    "cumberland lawn tennis club": "cltc",
    "cumberland": "cltc",
    "paddington sports club": "psc",
    "paddington sc": "psc",
}

debug = "--debug" in sys.argv


def load_results(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    raw = re.sub(r"^window\.__RESULTS__\s*=\s*", "", raw.strip()).rstrip(";").strip()
    return json.loads(raw)


def strip_team_number(name: str) -> str:
    return re.sub(r"\s+\d+$", "", name).strip()


def slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def club_slug(raw_name: str) -> str:
    stripped = strip_team_number(raw_name).lower()
    return CLUB_ALIASES.get(stripped, slugify(stripped))


def collect_draw_urls() -> dict[str, str]:
    """Return {full_draw_url: draw_id} for all draws in seed data.
    Keyed by full URL because draw numbers are only unique within a competition;
    two competitions can both have a /draw/8 that are entirely different pages.
    """
    urls: dict[str, str] = {}  # url → draw_id (for deduplication by URL)
    for path in seed_files():
        data = load_results(path)
        for comp in data.get("competitions", []):
            for team in comp.get("teams", []):
                url = team.get("leagueUrl", "")
                m = re.search(r"/draw/(\d+)", url, re.I)
                if m and url and url not in urls:
                    urls[url] = f"{comp['id'][:8]}-draw{m.group(1)}"
    return urls  # {url: unique_id}


def collect_known_clubs() -> dict[str, str]:
    """Return {slug: name} for all Level-1 clubs seen in standings."""
    clubs: dict[str, str] = {}
    for path in seed_files():
        data = load_results(path)
        for comp in data.get("competitions", []):
            for team in comp.get("teams", []):
                for row in team.get("standings", []):
                    slug = club_slug(row["name"])
                    if slug not in SEED_SLUGS and slug not in clubs:
                        clubs[slug] = strip_team_number(row["name"])
    return clubs


def accept_cookies(page):
    for sel in [
        'button:has-text("Accept all")',
        'button:has-text("Accept All")',
        '#cookiescript_accept',
        '[aria-label*="accept" i]',
        'button:has-text("Accept")',
        '.js-simple-accept-view button',
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=2000)
                page.wait_for_timeout(1000)
                return
        except Exception:
            pass
    # Fallback: submit the cookiewall form directly (covers TournamentSoftware instances
    # that don't expose a visible accept button to headless Chromium)
    try:
        page.evaluate("""
            const form = document.querySelector('form[action*="cookiewall"]');
            if (form) form.submit();
        """)
        page.wait_for_load_state("networkidle", timeout=10000)
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


def scrape_draw_for_team_links(page, draw_url: str) -> dict[str, str]:
    """
    Navigate to a draw page and return {team_url: club_name} for all
    /league/.../team/N links found in the standings table.
    """
    found: dict[str, str] = {}
    try:
        page.goto(draw_url, wait_until="networkidle", timeout=60000)
        accept_cookies(page)
        page.wait_for_selector("table", timeout=8000)
        page.wait_for_timeout(600)

        if debug:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            draw_id = re.search(r"/draw/(\d+)", draw_url).group(1)
            (DEBUG_DIR / f"draw-{draw_id}.html").write_text(page.content(), encoding="utf-8")

        links = page.eval_on_selector_all(
            'a[href*="/team/"]',
            "els => els.map(a => ({href: a.href, text: a.textContent.trim()}))"
        )
        for link in links:
            href = link.get("href", "")
            text = link.get("text", "")
            # Only /league/.../team/N (numeric) — excludes /team-match/
            if re.search(r"/team/\d+$", href) and text:
                found[href] = text
    except Exception as e:
        print(f"  warning: {draw_url} — {e}", file=sys.stderr)
    return found


def scrape_team_for_club_url(page, team_url: str, club_name: str) -> str | None:
    """
    Navigate to a team page. The team page links to a competition-scoped
    /league/{compId}/club/{clubId} page. Follow that to find the global
    /association/group/{GUID} club profile URL.
    """
    try:
        page.goto(team_url, wait_until="networkidle", timeout=60000)
        accept_cookies(page)
        page.wait_for_timeout(800)

        if debug:
            team_id = re.search(r"/team/(\d+)", team_url).group(1)
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            (DEBUG_DIR / f"team-{team_id}.html").write_text(page.content(), encoding="utf-8")

        # First: check for a direct /association/group/ link
        direct = page.eval_on_selector_all(
            'a[href*="/association/group/"]',
            "els => els.map(a => a.href)"
        )
        if direct:
            return direct[0].split("?")[0]

        # Second: find competition-scoped /club/N link and follow it
        club_links = page.eval_on_selector_all(
            'a[href*="/club/"]',
            "els => els.map(a => a.href)"
        )
        comp_club_url = next(
            (h for h in club_links if re.search(r"/league/[^/]+/club/\d+", h)),
            None
        )
        if not comp_club_url:
            return None

        page.goto(comp_club_url, wait_until="networkidle", timeout=60000)
        accept_cookies(page)
        page.wait_for_timeout(800)

        if debug:
            club_id = re.search(r"/club/(\d+)", comp_club_url).group(1)
            (DEBUG_DIR / f"compclub-{club_id}.html").write_text(page.content(), encoding="utf-8")

        group_links = page.eval_on_selector_all(
            'a[href*="/association/group/"]',
            "els => els.map(a => a.href)"
        )
        if group_links:
            return group_links[0].split("?")[0]

    except Exception as e:
        print(f"  warning: team page {team_url} — {e}", file=sys.stderr)
    return None


def main():
    registry: dict[str, str] = json.loads(REGISTRY_PATH.read_text())
    draw_urls = collect_draw_urls()
    known_clubs = collect_known_clubs()

    print(f"Draw URLs in seed data: {len(draw_urls)}")
    print(f"Level-1 clubs in standings: {len(known_clubs)}")
    print(f"Already in registry: {len(registry)}")

    already_mapped = set(registry.keys()) | SEED_SLUGS
    clubs_to_find = {s: n for s, n in known_clubs.items() if s not in already_mapped}
    print(f"Clubs still to discover: {len(clubs_to_find)}")

    if not clubs_to_find:
        print("Registry is complete. Nothing to do.")
        return

    new_entries: dict[str, str] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ))
        page = ctx.new_page()

        # Collect all team URLs across all draw pages, deduped by team URL.
        # team_url → raw club name from the standings link text.
        all_team_links: dict[str, str] = {}

        for i, (draw_url, draw_id) in enumerate(draw_urls.items(), 1):
            print(f"[{i}/{len(draw_urls)}] Scanning {draw_id} for team links …")
            team_links = scrape_draw_for_team_links(page, draw_url)
            for url, name in team_links.items():
                slug = club_slug(name)
                if slug in clubs_to_find and url not in all_team_links:
                    all_team_links[url] = name
            page.wait_for_timeout(1500)

        print(f"\nFound {len(all_team_links)} team pages to visit for {len(clubs_to_find)} clubs")

        # Visit each team page to find the club's association/group URL.
        for j, (team_url, club_name) in enumerate(all_team_links.items(), 1):
            slug = club_slug(club_name)
            if slug in new_entries:
                continue  # already found this club via another team

            remaining = len(clubs_to_find) - len(new_entries)
            print(f"  [{j}/{len(all_team_links)}] {club_name} ({slug}) …", end=" ", flush=True)
            group_url = scrape_team_for_club_url(page, team_url, club_name)
            if group_url:
                new_entries[slug] = group_url
                print(f"FOUND {group_url}")
            else:
                print("not found")
            page.wait_for_timeout(1500)

            if not (clubs_to_find.keys() - new_entries.keys()):
                print("All clubs found — stopping early.")
                break

        browser.close()

    if new_entries:
        registry.update(new_entries)
        REGISTRY_PATH.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nWrote {len(new_entries)} new entries to {REGISTRY_PATH}")
    else:
        print(
            "\nNo club profile links found on draw pages. "
            "The LTA standings table does not link to club profiles.\n"
            "Populate clubs-registry.json manually or run with --debug to inspect the page HTML."
        )

    still_missing = {s: n for s, n in clubs_to_find.items() if s not in new_entries}
    if still_missing:
        print(f"\nStill missing {len(still_missing)} clubs:")
        for slug, name in sorted(still_missing.items()):
            print(f"  {slug}  ({name})")


if __name__ == "__main__":
    main()
