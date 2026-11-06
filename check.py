#!/usr/bin/env python3
"""Watch cinema sites for specific movies and email alerts when they appear.

Reads ``watches.json`` (a list of {id, site, movie} entries), fetches each
site's listings, and matches each movie against them. The first time a match
is seen for a watch, an email is sent through Resend and the match is recorded
in ``state.json`` so it is not emailed again. Each watch is independent:
"Dune on Showcase IMAX" never triggers from Cinépolis listings and vice versa.

Sites:
    showcase_imax - Showcase's IMAX block (server-rendered HTML).
    cinepolis     - Cinépolis Argentina via its GraphQL billboard API.

Usage:
    python check.py                 # real run (used by GitHub Actions)
    python check.py --dry-run       # print matches, send nothing
    python check.py --test-email    # send a test email through Resend
    python check.py --force         # simulate matches to test the full path
    python check.py --watch <id>    # run only one watch entry
    python check.py --match <txt>   # run only watches whose movie contains <txt>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as browser_requests

WATCHES_FILE = Path(__file__).resolve().parent / "watches.json"
STATE_FILE = Path(__file__).resolve().parent / "state.json"
RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_FROM = "onboarding@resend.dev"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

IMAX_BLOCK_ID = "cartelera_cine_40219"
SHOWCASE_HOMEPAGE = "https://www.todoshowcase.com/"

# Cinepolis GraphQL billboard API (backing the cinepolis.com/ar SPA).
# Plain requests gets Cloudflare-blocked, so calls go through curl_cffi
# with a Chrome TLS fingerprint.
CINEPOLIS_API = "https://api-g.cinepolis.com/v2/billboards/graphql"
CINEPOLIS_API_KEY = "lQM6Mkvri1iHksKKCfpAiwGXq0YUZA7Nn6XAXRPr4i13LwXo"
CINEPOLIS_QUERY = """\
query Movies($countryId: String!, $category: String, $cinemas: String, $after: String, $limit: Int, $isAbsoluteMediaPath: Boolean) {
  movies(
    countryId: $countryId
    category: $category
    cinemas: $cinemas
    after: $after
    limit: $limit
    isAbsoluteMediaPath: $isAbsoluteMediaPath
  ) {
    pageInfo { startCursor endCursor hasNextPage hasPreviousPage __typename }
    totalCount
    edges {
      cursor
      node {
        id
        name
        originalName
        rating
        ratingDescription
        availableForSale
        releaseDate
        formats
        tags { name resource __typename }
        categories
        experiences { icon name __typename }
        __typename
      }
      __typename
    }
    __typename
  }
}"""


class SiteNotImplemented(RuntimeError):
    """Raised when a watch's site has no scraper yet."""


# --- Site scrapers ----------------------------------------------------------
#
# Each scraper takes the site dict and returns (films, meta):
#     films: list of {"title", "film_id", "tags", "url"} dicts
#     meta:  dict with site-specific context for the email (e.g. fallback flag)


def fetch_html(url: str) -> str:
    response = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
        },
        timeout=30,
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def extract_film_id(href: str | None) -> str | None:
    match = re.search(r"filmid=(\d+)", href or "")
    return match.group(1) if match else None


def parse_showcase_films(html: str) -> tuple[list[dict], bool]:
    """Return (films, used_fallback) from the Showcase IMAX block."""
    soup = BeautifulSoup(html, "html.parser")
    block = soup.find(id=IMAX_BLOCK_ID)
    used_fallback = block is None
    scope = soup if used_fallback else block

    films: list[dict] = []
    for box in scope.select(".boxfilm"):
        title_link = box.select_one(".titulo-pelicula h2 a")
        if title_link is None:
            continue
        title = title_link.get_text(strip=True)
        if not title:
            continue
        poster_link = box.select_one(".afiche-pelicula a")
        href = (title_link.get("href") or (poster_link.get("href") if poster_link else "") or "").strip()
        tags: list[str] = []
        for tag_el in box.select(".tag-container"):
            inner = [p.get_text(strip=True) for p in tag_el.select("p")]
            if inner:
                tags.extend(inner)
            else:
                text = tag_el.get_text(strip=True)
                if text:
                    tags.append(text)
        films.append(
            {
                "title": title,
                "film_id": extract_film_id(href),
                "tags": [t for t in tags if t],
                "url": href or SHOWCASE_HOMEPAGE,
            }
        )
    return films, used_fallback


def scrape_showcase(site: dict) -> tuple[list[dict], dict]:
    html = fetch_html(site["url"])
    films, used_fallback = parse_showcase_films(html)
    return films, {"used_fallback": used_fallback}


def cinepolis_fetch(category: str, after: str | None = None) -> dict:
    variables: dict = {
        "category": category,
        "countryId": "AR",
        "isAbsoluteMediaPath": True,
        "limit": 100,
    }
    if after:
        variables["after"] = after
    if category == "now-playing":
        variables["cinemas"] = ""

    response = browser_requests.post(
        CINEPOLIS_API,
        headers={
            "accept": "*/*",
            "accept-language": "es-AR,es;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "country-id": "AR",
            "language": "ES",
            "origin": "https://cinepolis.com",
            "referer": "https://cinepolis.com/",
            "x-apikey": CINEPOLIS_API_KEY,
        },
        json={"operationName": "Movies", "variables": variables, "query": CINEPOLIS_QUERY},
        timeout=30,
        impersonate="chrome",
    )
    response.raise_for_status()
    data = response.json()
    if "errors" in data:
        raise RuntimeError(f"cinepolis GraphQL errors: {data['errors']!r}")
    return data


def scrape_cinepolis(site: dict) -> tuple[list[dict], dict]:
    """Fetch both billboards; tag films so watches can filter by listing.

    Tags include the API's own tags (e.g. "Preventa") plus a synthetic tag:
    "Cartelera" for the now-playing billboard (tickets buyable, incl.
    pre-sales) and "Proximamente" for coming-soon. Note: availableForSale is
    True even for coming-soon titles, so the billboard is the real signal.
    """
    films: list[dict] = []
    for category, label in (("now-playing", "Cartelera"), ("coming-soon", "Proximamente")):
        after: str | None = None
        for _ in range(5):  # pagination safety cap
            data = cinepolis_fetch(category, after)
            payload = (data.get("data") or {}).get("movies") or {}
            for edge in payload.get("edges") or []:
                node = edge.get("node") or {}
                title = (node.get("name") or "").strip()
                if not title:
                    continue
                tags = [t.get("name") for t in node.get("tags") or [] if t.get("name")]
                tags.append(label)
                node_id = node.get("id")
                films.append(
                    {
                        "title": title,
                        "film_id": str(node_id) if node_id is not None else None,
                        "tags": tags,
                        "url": site["homepage"],
                        "category": category,
                    }
                )
            page_info = payload.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if not after:
                break
    return films, {}


SITES: dict[str, dict] = {
    "showcase_imax": {
        "label": "Showcase IMAX",
        "url": SHOWCASE_HOMEPAGE,
        "homepage": SHOWCASE_HOMEPAGE,
        "scrape": scrape_showcase,
    },
    "cinepolis": {
        "label": "Cinepolis",
        "url": "https://cinepolis.com/ar",
        "homepage": "https://cinepolis.com/ar",
        "scrape": scrape_cinepolis,
    },
}


# --- Watchlist and state ----------------------------------------------------


def load_watches() -> list[dict]:
    if not WATCHES_FILE.exists():
        sys.exit(f"{WATCHES_FILE.name} not found; create it with a 'watches' list.")
    try:
        data = json.loads(WATCHES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        sys.exit(f"{WATCHES_FILE.name} is not valid JSON: {exc}")

    raw = data.get("watches", data) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        sys.exit(f"{WATCHES_FILE.name} must contain a 'watches' list.")

    watches: list[dict] = []
    for i, entry in enumerate(raw):
        site = entry.get("site")
        movie = (entry.get("movie") or "").strip()
        if not site or not movie:
            sys.exit(
                f"{WATCHES_FILE.name} entry #{i}: 'site' and 'movie' are required."
            )
        if site not in SITES:
            sys.exit(
                f"{WATCHES_FILE.name} entry #{i}: unknown site '{site}' "
                f"(known sites: {', '.join(sorted(SITES))})."
            )
        require_tags = entry.get("require_tags") or []
        if not isinstance(require_tags, list) or not all(
            isinstance(t, str) for t in require_tags
        ):
            sys.exit(
                f"{WATCHES_FILE.name} entry #{i}: 'require_tags' must be a list of strings."
            )
        watches.append(
            {
                "id": entry.get("id") or f"{site}:{movie.lower()}",
                "site": site,
                "movie": movie,
                "require_tags": require_tags,
            }
        )
    return watches


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(state, dict) and isinstance(state.get("notified"), dict):
                return state
        except (json.JSONDecodeError, OSError):
            pass
    return {"notified": {}}


def save_state(state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def identifier(film: dict) -> str:
    """De-duplication key.

    Includes the billboard category when present so that a Cinepolis movie
    appearing first in "coming-soon" and later in "now-playing" alerts twice
    (once per transition), instead of the second one being suppressed.
    """
    base = film.get("film_id") or film["title"]
    category = film.get("category")
    return f"{base}|{category}" if category else base


def fold(text: str) -> str:
    """Lowercase and strip accents so 'Duna' matches 'duna' and 'Dúna'."""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()


# --- Email ------------------------------------------------------------------


def build_email(
    watch: dict, site: dict, films: list[dict], meta: dict
) -> tuple[str, str]:
    label = site["label"]
    titles = ", ".join(f["title"] for f in films)
    listing = "la cartelera"
    if films and films[0].get("category") == "coming-soon":
        listing = "Proximamente"
    subject = f"[{label}] {titles} ya esta en {listing}"

    rows = []
    for film in films:
        tags = " / ".join(film["tags"]) if film["tags"] else "-"
        detail = f"{film['title']} ({tags})"
        if film.get("film_id"):
            detail += f" - filmid {film['film_id']}"
        rows.append(f"<li><a href=\"{film['url']}\">{detail}</a></li>")

    warning = ""
    if films and films[0].get("category") == "coming-soon":
        warning = (
            "<p style=\"color:#b45309\"><b>Nota:</b> aparece en Proximamente; "
            "las entradas aun no estan a la venta. Este aviso es informativo.</p>"
        )
    elif meta.get("used_fallback"):
        warning = (
            "<p style=\"color:#b45309\"><b>Nota:</b> no se encontro el bloque "
            "habitual del sitio; se reviso la pagina completa. Conviene verificar "
            "que el sitio no haya cambiado de estructura.</p>"
        )

    html = f"""\
<html>
  <body style="font-family:Arial,sans-serif;color:#1f2937">
    <h2 style="color:#4011a7">{watch['movie']} ya aparece en {label}</h2>
    <ul>{''.join(rows)}</ul>
    {warning}
    <p>Compra de entradas: <a href="{site['homepage']}">{site['homepage']}</a></p>
    <p style="color:#6b7280;font-size:12px">
      Aviso automatico del watchlist de cines (watch: {watch['id']}).
    </p>
  </body>
</html>
"""
    return subject, html


def send_email(subject: str, html: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    recipient = os.environ.get("ALERT_EMAIL")
    sender = os.environ.get("RESEND_FROM") or DEFAULT_FROM

    if not api_key or not recipient:
        sys.exit(
            "Missing RESEND_API_KEY and/or ALERT_EMAIL environment variables."
        )

    response = requests.post(
        RESEND_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": sender,
            "to": [recipient],
            "subject": subject,
            "html": html,
        },
        timeout=30,
    )
    if response.status_code >= 300:
        sys.exit(f"Resend error {response.status_code}: {response.text}")
    print(f"  Email sent to {recipient} (id={response.json().get('id')}).")


# --- Main -------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch cinemas for movies.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be sent without sending an email",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="send a single test email through Resend and exit",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="simulate a match on every watch to exercise the full alert path",
    )
    parser.add_argument(
        "--always",
        action="store_true",
        help="email on every run, ignoring the state de-duplication",
    )
    parser.add_argument(
        "--watch",
        default=None,
        help="only run the watch entry with this id",
    )
    parser.add_argument(
        "--match",
        default=None,
        help="only run watches whose movie contains this text",
    )
    return parser.parse_args()


def run_watch(
    watch: dict, args: argparse.Namespace, notified_by_watch: dict
) -> tuple[bool, bool]:
    """Run one watch entry. Returns (changed_state, failed)."""
    site = SITES[watch["site"]]
    print(f"[{watch['id']}] Looking for '{watch['movie']}' on {site['label']}.")

    try:
        films, meta = site["scrape"](site)
    except SiteNotImplemented as exc:
        print(f"  Skipped: {exc}")
        return False, False
    except Exception as exc:  # noqa: BLE001 - report and keep going
        print(f"  ERROR ({type(exc).__name__}): {exc}", file=sys.stderr)
        return False, True

    pattern = re.compile(re.escape(fold(watch["movie"])))
    title_matches = [f for f in films if pattern.search(fold(f["title"]))]

    require_tags = [fold(t) for t in watch.get("require_tags") or []]
    if require_tags:
        matches = [
            f
            for f in title_matches
            if any(
                any(req in tag for tag in (fold(t) for t in f.get("tags") or []))
                for req in require_tags
            )
        ]
        for film in title_matches:
            if film not in matches:
                print(
                    f"  '{film['title']}' matches the title but is missing a required "
                    f"tag ({', '.join(film['tags']) or '-'}); waiting."
                )
    else:
        matches = title_matches

    if args.force:
        matches = matches or [
            {
                "title": f"{watch['movie']}: FORCE TEST",
                "film_id": f"FORCE-{watch['id']}",
                "tags": ["TEST"],
                "url": site["homepage"],
            }
        ]
        print("  --force enabled: simulating a match (de-duplication bypassed).")

    if not matches:
        print(f"  No match (checked {len(films)} films).")
        return False, False

    print(f"  Found {len(matches)} match(es):")
    for film in matches:
        print(f"    - {film['title']} [{', '.join(film['tags']) or '-'}] {film['url']}")

    if args.dry_run:
        subject, _ = build_email(watch, site, matches, meta)
        print(f"  Dry run: would email '{subject}' (state unchanged).")
        return False, False

    always = args.always or os.environ.get("ALWAYS_NOTIFY", "").lower() in {"1", "true", "yes"}
    notified = set(notified_by_watch.get(watch["id"], []))
    new_matches = [f for f in matches if identifier(f) not in notified]

    if not new_matches and not args.force and not always:
        print(f"  '{watch['movie']}' was already notified previously; skipping email.")
        return False, False

    to_send = matches if (always or args.force) else new_matches
    subject, body = build_email(watch, site, to_send, meta)
    send_email(subject, body)

    if not args.force and not always:
        notified.update(identifier(f) for f in to_send)
        notified_by_watch[watch["id"]] = sorted(notified)
        return True, False
    if always:
        print("  ALWAYS_NOTIFY set: state left unchanged, email sent every run.")
    return False, False


def main() -> None:
    args = parse_args()

    if args.test_email:
        send_email(
            "[Cinema watchlist] Test email",
            "<p>Si recibis este correo, la configuracion de Resend funciona.</p>",
        )
        return

    watches = load_watches()
    if args.watch:
        watches = [w for w in watches if w["id"] == args.watch]
        if not watches:
            sys.exit(f"No watch with id '{args.watch}'.")
    if args.match:
        pattern = re.compile(re.escape(fold(args.match)))
        watches = [w for w in watches if pattern.search(fold(w["movie"]))]
    if not watches:
        print("No watches to run (check watches.json and any --watch/--match filters).")
        return

    state = load_state()
    notified_by_watch = state.setdefault("notified", {})
    changed = False
    failures = 0

    for watch in watches:
        watch_changed, failed = run_watch(watch, args, notified_by_watch)
        changed = changed or watch_changed
        failures += int(failed)

    if changed:
        save_state(state)
        print("state.json updated.")

    if failures:
        sys.exit(f"{failures} watch(es) failed.")


if __name__ == "__main__":
    main()
