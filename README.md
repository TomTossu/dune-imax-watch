# dune-imax-watch

Checks the Showcase **IMAX Theatre** listings (Argentina) and emails you the
moment the new **Dune** movie appears, including the pre-sale ("Venta
Anticipada") listing. Emails are sent through [Resend](https://resend.com), so
no personal email credentials are used.

## How it works

1. Downloads `https://www.todoshowcase.com/` (plain server-rendered HTML).
2. Parses the IMAX Theatre block `#cartelera_cine_40219`.
3. Matches any movie title containing the target (default `"dune"`).
4. Emails you via Resend the first time a match is seen, then records its
   `filmid` in `state.json` so you are not spammed on later runs.

If the `#cartelera_cine_40219` block ever disappears (site redesign), the
script falls back to scanning the whole page and adds a warning to the email.

## Triggering (read this)

GitHub's built-in `schedule` cron is **not reliable**: runs can be delayed or
dropped under load and changed cron expressions may be ignored. So instead the
workflow is triggered by an **external cron that calls GitHub's
`repository_dispatch` API**, which starts it deterministically.

### Set up the external trigger

1. Create a **fine-grained** personal access token
   (GitHub -> Settings -> Developer settings -> Personal access tokens ->
   Fine-grained tokens):
   - Repository access: **Only select repositories** -> `dune-imax-watch`
   - Permissions: **Contents: Read and write** (required for
     `repository_dispatch`)
   - Give it an expiry you are comfortable with.
2. Create a free account at https://cron-job.org (or any scheduler that can
   send a custom POST with headers; UptimeRobot cannot).
3. Create a cron job:
   - URL: `https://api.github.com/repos/<owner>/dune-imax-watch/dispatches`
   - Schedule: every 10 minutes (or your preferred interval)
   - Method: `POST`
   - Headers:
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer <YOUR_PAT>`
     - `X-GitHub-Api-Version: 2022-11-28`
     - `Content-Type: application/json`
   - Request body: `{"event_type":"check-dune"}`
4. Verify a run appears:
   `gh run list --repo <owner>/dune-imax-watch --event repository_dispatch`

The PAT is only stored in the scheduler and only has access to this one repo,
so it can be revoked at any time.

## Configuration

| Variable | Where | Purpose |
|---|---|---|
| `RESEND_API_KEY` | repo secret | Resend API key |
| `ALERT_EMAIL` | repo secret | recipient (must be your Resend account email until a domain is verified) |
| `RESEND_FROM` | repo secret | optional, defaults to `onboarding@resend.dev` |
| `TARGET_MOVIE` | workflow env / CLI | title substring to watch, default `dune` |
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

python check_dune.py --dry-run             # scrape + print, nothing sent
python check_dune.py --match "avengers"    # watch a different title
python check_dune.py --always --dry-run     # ignore de-dupe (dry run)
python check_dune.py --test-email          # send one Resend test
python check_dune.py --force               # simulate a match
```

Local runs that send email need the environment variables exported:

```bash
export RESEND_API_KEY=re_xxxxxxxx
export ALERT_EMAIL=you@example.com
```

## Workflows

- `check.yml` - production watcher, triggered by `repository_dispatch`
  (targets Dune, de-duplicates).

## Notes

- Only one HTTP request per run, with a normal browser User-Agent.
- Triggering is done entirely by the external cron via `repository_dispatch`;
  there is no GitHub `schedule` fallback.
