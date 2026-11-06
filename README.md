# Cinema watchlist

Watches cinema sites for specific movies and emails you the moment a movie
appears in the listings (including pre-sale "Venta Anticipada" / "Preventa"
entries). Emails are sent through [Resend](https://resend.com), so no personal
email credentials are used.

Each entry in the watchlist pairs **one movie with one site**, so watches are
independent: "Dune on Showcase IMAX" never triggers from Cinépolis listings
and vice versa.

## Watchlist (`watches.json`)

```json
{
  "watches": [
    { "id": "dune-imax", "site": "showcase_imax", "movie": "dune" },
    {
      "id": "puedes-verlo-todo-cinepolis",
      "site": "cinepolis",
      "movie": "Puedes verlo todo"
    }
  ]
}
```

- `id` - unique name for the watch (used for de-duplication and `--watch`).
- `site` - one of the sites below.
- `movie` - case- and accent-insensitive substring matched against titles.
  `"dune"` matches `Duna: Parte Tres`.
- `require_tags` - optional list of substrings; when set, a match is only
  alerted if the film has at least one tag containing at least one of them.
  Not used by the default watchlist; see the Cinepolis notes for the
  trade-off.

## Sites

| Site | Status |
|---|---|
| `showcase_imax` | Working: parses the IMAX block `#cartelera_cine_40219` on `todoshowcase.com`. Falls back to scanning the whole page (with an email warning) if the block disappears. |
| `cinepolis` | Working: calls the GraphQL billboard API behind `cinepolis.com/ar`. Titles are tagged `Cartelera` (now-playing, tickets buyable incl. pre-sales) or `Proximamente` (coming-soon), plus the API's own tags such as `Preventa`. |

### Cinepolis notes

- The site is a Cloudflare-protected SPA. Plain `requests` gets a 403, so the
  scraper uses `curl_cffi` with a Chrome TLS fingerprint (dependency already
  in `requirements.txt`).
- `availableForSale` is `True` even for coming-soon titles, so the reliable
  "tickets start selling" signal is the movie **appearing in the now-playing
  billboard** (`Cartelera` tag).
- The default watchlist alerts on **either** billboard: first when the movie
  appears in "Proximamente" (the email says tickets are not on sale yet -
  useful as an early pipeline test) and again when it moves to "Cartelera".
  This works because de-duplication is per film *and* per billboard.
  To alert only when tickets are actually on sale, add
  `"require_tags": ["cartelera"]` to the watch.
- The API key is the public one embedded in the Cinepolis web app.

## How it works

1. Reads `watches.json` and runs each watch in sequence.
2. Fetches the site's listings and matches the movie (accent/case-folded
   substring).
3. Emails you via Resend the first time a match is seen, then records the
   match in `state.json` under the watch's `id` so you are not spammed on
   later runs.
4. State is committed back to the repo by the runner (local `run_local.sh` or
   the GitHub Actions workflow), so de-duplication survives across runs.

## Triggering: local runner (primary)

The Cinepolis API is Cloudflare-protected and **rejects GitHub Actions'
datacenter IPs** (plain clients and datacenter IPs both get 403s), while a
residential IP works. So the watcher runs on your Mac via launchd every
10 minutes:

- `run_local.sh` - loads `.env`, runs `check.py`, and pushes `state.json`
  changes so the repo stays in sync.
- `com.duneimaxwatch.check.plist` - launchd job (every 600s, logs to
  `run.log`).

### One-time setup on the Mac

1. Fill in `.env` (gitignored) with the same values as the GitHub secrets:

   ```bash
   RESEND_API_KEY=re_xxxxxxxx
   ALERT_EMAIL=you@example.com
   # RESEND_FROM=          # optional
   ```

2. Install and load the launchd job:

   ```bash
   cp com.duneimaxwatch.check.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.duneimaxwatch.check.plist
   ```

3. **Grant Full Disk Access to `/bin/zsh`** — required on macOS because
   TCC blocks launchd/cron children from reading `~/Documents`
   ("Operation not permitted", "can't open input file"):
   System Settings -> Privacy & Security -> Full Disk Access -> `+` ->
   Cmd+Shift+G -> `/bin/zsh` -> Open -> enable the toggle.

4. Verify:

   ```bash
   launchctl kickstart -k gui/$(id -u)/com.duneimaxwatch.check
   tail run.log    # should show both watches running
   ```

The job runs every 10 minutes while you are logged in (runs catch up when
the Mac wakes). `run.log` is gitignored and self-rotates by size if it ever
matters — each run is a few lines.

### Remove the old external trigger

Delete the cron-job.org job that called `repository_dispatch` — with the
local runner it would just create failing Actions runs every 10 minutes.

## Triggering: GitHub Actions (legacy)

GitHub's built-in `schedule` cron is **not reliable**: runs can be delayed or
dropped under load and changed cron expressions may be ignored. So instead the
workflow is triggered by an **external cron that calls GitHub's
`repository_dispatch` API**, which starts it deterministically.

Note: the Cinepolis watch fails with a 403 from Actions' datacenter IPs, so
Actions is only useful for the Showcase watch or manual dry runs.

### Set up the external trigger

1. Create a **fine-grained** personal access token
   (GitHub -> Settings -> Developer settings -> Personal access tokens ->
   Fine-grained tokens):
   - Repository access: **Only select repositories** -> this repo
   - Permissions: **Contents: Read and write** (required for
     `repository_dispatch`)
   - Give it an expiry you are comfortable with.
2. Create a free account at https://cron-job.org (or any scheduler that can
   send a custom POST with headers; UptimeRobot cannot).
3. Create a cron job:
   - URL: `https://api.github.com/repos/<owner>/<repo>/dispatches`
   - Schedule: every 10 minutes (or your preferred interval)
   - Method: `POST`
   - Headers:
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer <YOUR_PAT>`
     - `X-GitHub-Api-Version: 2022-11-28`
     - `Content-Type: application/json`
   - Request body: `{"event_type":"check-dune"}`
4. Verify a run appears:
   `gh run list --repo <owner>/<repo> --event repository_dispatch`

The PAT is only stored in the scheduler and only has access to this one repo,
so it can be revoked at any time.

## Configuration

| Variable | Where | Purpose |
|---|---|---|
| `RESEND_API_KEY` | `.env` (local) / repo secret | Resend API key |
| `ALERT_EMAIL` | `.env` (local) / repo secret | recipient (must be your Resend account email until a domain is verified) |
| `RESEND_FROM` | `.env` (local) / repo secret | optional, defaults to `onboarding@resend.dev` |
| `ALWAYS_NOTIFY` | workflow env | if truthy, email on every run and ignore `state.json` |

## One-time setup: Resend and GitHub

1. Create a free account at https://resend.com and an **API key**.
   Until you verify a domain, Resend only allows `from: onboarding@resend.dev`
   and `to:` the email you signed up with.
2. Add repository secrets (Settings -> Secrets and variables -> Actions):
   `RESEND_API_KEY`, `ALERT_EMAIL`, and optionally `RESEND_FROM`.

## Local usage

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python check.py --dry-run              # run all watches, print, send nothing
python check.py --watch dune-imax      # run only one watch
python check.py --match dune           # run watches whose movie contains "dune"
python check.py --force --dry-run      # simulate matches, print, send nothing
python check.py --test-email           # send one Resend test
python check_dune.py --dry-run         # shim: same as --watch dune-imax
./run_local.sh                         # what launchd runs: check.py + state push
```

Local runs that send email need the environment variables exported:

```bash
export RESEND_API_KEY=re_xxxxxxxx
export ALERT_EMAIL=you@example.com
```

## Adding a watch

Add an entry to `watches.json`:

```json
{ "id": "mickey-17-imax", "site": "showcase_imax", "movie": "mickey" }
```

That's it — the workflow picks it up on the next run. For sites with multiple
listing states (like Cinepolis), add `require_tags` to only alert at the
right moment.

## Adding a new site

1. Add a `scrape_<name>` function in `check.py` that returns
   `(films, meta)` where `films` is a list of
   `{"title", "film_id", "tags", "url"}` dicts and `meta` may include
   `{"used_fallback": true}`.
2. Register it in the `SITES` dict with a `label`, `url`, and `homepage`.
3. Use `"site": "<name>"` in `watches.json`.

## Workflows

- `check.yml` - production watcher, triggered by `repository_dispatch`
  (runs every watch, de-duplicates per watch).

## Notes

- One or two HTTP requests per site per run, with a normal browser
  User-Agent; Cinepolis calls go through `curl_cffi` (Chrome TLS fingerprint)
  because Cloudflare blocks plain clients.
- The Cinepolis API is only reachable from residential IPs; GitHub Actions'
  datacenter IPs get a 403, which is why the primary trigger is the local
  launchd runner. Manual Actions runs still work for the Showcase watch.
- If a site errors, the run reports the failure and continues with the other
  watches (a failed watch exits non-zero). The local runner only commits
  `state.json` on a successful run.
