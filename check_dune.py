#!/usr/bin/env python3
"""Check whether the new Dune movie is listed on Showcase IMAX and email an alert.

Scrapes https://www.todoshowcase.com/ (server-rendered HTML, no browser needed),
looks at the IMAX Theatre block, and emails via the Resend API when a title
matching "Dune" shows up.

Usage:
    python check_dune.py              # real run (used by GitHub Actions)
    python check_dune.py --dry-run    # print matches, send nothing
    python check_dune.py --test-email # send a test email through Resend
    python check_dune.py --force      # simulate a match to test the full path
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HOMEPAGE = "https://www.todoshowcase.com/"
IMAX_BLOCK_ID = "cartelera_cine_40219"
DEFAULT_TARGET_MOVIE = "dune"
STATE_FILE = Path(__file__).resolve().parent / "state.json"
RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_FROM = "onboarding@resend.dev"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch Showcase IMAX for a movie.")
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
        help="simulate a match to exercise the full alert path",
    )
    parser.add_argument(
        "--match",
        default=None,
        help="movie title substring to look for (overrides TARGET_MOVIE)",
    )
    parser.add_argument(
        "--always",
        action="store_true",
        help="email on every run, ignoring the state de-duplication",
    )
    return parser.parse_args()


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


def extract_films(html: str) -> tuple[list[dict], bool]:
    """Return (films, used_fallback) from the IMAX block.

    ``used_fallback`` is True when the IMAX block was not found and the whole
    page was scanned instead (e.g. after a site redesign).
    """
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
                "url": href or HOMEPAGE,
            }
        )
    return films, used_fallback


def identifier(film: dict) -> str:
    return film.get("film_id") or film["title"]


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(state, dict) and isinstance(state.get("notified"), list):
                return state
        except (json.JSONDecodeError, OSError):
            pass
    return {"notified": []}


def save_state(state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def build_email(films: list[dict], used_fallback: bool, target: str) -> tuple[str, str]:
    titles = ", ".join(f["title"] for f in films)
    subject = f"[IMAX] {titles} ya esta en la cartelera"

    rows = []
    for film in films:
        tags = " / ".join(film["tags"]) if film["tags"] else "-"
        detail = f"{film['title']} ({tags})"
        if film.get("film_id"):
            detail += f" - filmid {film['film_id']}"
        rows.append(f"<li><a href=\"{film['url']}\">{detail}</a></li>")

    warning = ""
    if used_fallback:
        warning = (
            "<p style=\"color:#b45309\"><b>Nota:</b> no se encontro el bloque de IMAX "
            "habitual; se reviso la pagina completa. Conviene verificar que el sitio "
            "no haya cambiado de estructura.</p>"
        )

    html = f"""\
<html>
  <body style="font-family:Arial,sans-serif;color:#1f2937">
    <h2 style="color:#4011a7">{target.title()} ya aparece en Showcase IMAX</h2>
    <ul>{''.join(rows)}</ul>
    {warning}
    <p>Compra de entradas: <a href="{HOMEPAGE}">{HOMEPAGE}</a></p>
    <p style="color:#6b7280;font-size:12px">
      Aviso automatico de dune-imax-watch.
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
    print(f"Email sent to {recipient} (id={response.json().get('id')}).")


def main() -> None:
    args = parse_args()

    if args.test_email:
        send_email(
            "[Dune IMAX Watcher] Test email",
            "<p>Si recibis este correo, la configuracion de Resend funciona.</p>",
        )
        return

    target = (args.match or os.environ.get("TARGET_MOVIE") or DEFAULT_TARGET_MOVIE).strip()
    target = target or DEFAULT_TARGET_MOVIE
    pattern = re.compile(re.escape(target), re.IGNORECASE)
    always = args.always or os.environ.get("ALWAYS_NOTIFY", "").lower() in {"1", "true", "yes"}
    print(f"Looking for '{target}' in the IMAX block." + (" (always notify)" if always else ""))

    html = fetch_html(HOMEPAGE)
    films, used_fallback = extract_films(html)
    if used_fallback:
        print(f"Warning: #{IMAX_BLOCK_ID} not found; scanned the whole page.")

    matches = [f for f in films if pattern.search(f["title"])]

    if args.force:
        matches = matches or [
            {
                "title": f"{target.title()}: FORCE TEST",
                "film_id": "FORCE-TEST",
                "tags": ["TEST"],
                "url": HOMEPAGE,
            }
        ]
        print("--force enabled: bypassing state de-duplication.")

    if not matches:
        print(f"No '{target}' match in the IMAX block (checked {len(films)} films).")
        return

    print(f"Found {len(matches)} '{target}' match(es):")
    for film in matches:
        print(f"  - {film['title']} [{', '.join(film['tags']) or '-'}] {film['url']}")

    if args.dry_run:
        print("Dry run: no email sent, state unchanged.")
        return

    state = load_state()
    notified = set(state.get("notified", []))
    new_matches = [f for f in matches if identifier(f) not in notified]

    if not new_matches and not args.force and not always:
        print(f"'{target}' was already notified previously; skipping email.")
        return

    to_send = matches if (always or args.force) else new_matches
    subject, body = build_email(to_send, used_fallback, target)
    send_email(subject, body)

    if not args.force and not always:
        notified.update(identifier(f) for f in to_send)
        state["notified"] = sorted(notified)
        save_state(state)
        print(f"state.json updated ({len(state['notified'])} id(s) tracked).")
    elif always:
        print("ALWAYS_NOTIFY set: state left unchanged, email sent every run.")


if __name__ == "__main__":
    main()
