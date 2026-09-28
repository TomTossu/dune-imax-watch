# dune-imax-watch

Checks the Showcase **IMAX Theatre** listings (Argentina) and emails you the
moment the new **Dune** movie appears, including the pre-sale ("Venta
Anticipada") listing. Runs on a GitHub Actions cron every 3 hours and sends
mail through [Resend](https://resend.com) (no personal email credentials).

## How it works

1. Downloads `https://www.todoshowcase.com/` (plain server-rendered HTML).
2. Parses the IMAX Theatre block `#cartelera_cine_40219`.
3. Matches any movie title containing "Dune".
4. Emails you via Resend the first time a match is seen, then records its
   `filmid` in `state.json` so you are not spammed on later runs.

If the `#cartelera_cine_40219` block ever disappears (site redesign), the
script falls back to scanning the whole page and adds a warning to the email.

## One-time setup

### 1. Resend
1. Create a free account at https://resend.com.
2. Create an **API key** (Dashboard -> API Keys).
3. While you have no verified domain, Resend only lets you send
   `from: onboarding@resend.dev` and `to:` the **email address you signed up
   with**. That is fine for this use case. To use a custom sender/recipient,
   verify a domain and set `RESEND_FROM`.

### 2. GitHub repository
1. Push this folder to a new GitHub repo.
2. Add repository secrets (Settings -> Secrets and variables -> Actions):
   - `RESEND_API_KEY` - your Resend API key
   - `ALERT_EMAIL` - the email address that should receive alerts
   - `RESEND_FROM` - optional, defaults to `onboarding@resend.dev`
3. The workflow `.github/workflows/check.yml` runs every 3 hours automatically.
   You can also trigger it manually from the Actions tab.

## Local usage

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python check_dune.py --dry-run      # scrape + print, nothing sent
python check_dune.py --test-email   # send one Resend test (needs env vars)
python check_dune.py --force        # simulate a match to test the full path
python check_dune.py                # real run
```

Local runs that send email need the environment variables exported:

```bash
export RESEND_API_KEY=re_xxxxxxxx
export ALERT_EMAIL=you@example.com
```

## Notes

- GitHub schedules can be delayed a few minutes and are disabled after ~60 days
  of repository inactivity; the workflow commits to `state.json` which keeps it
  active when a match is found. A manual `workflow_dispatch` run also resets it.
- Only one HTTP request per run, with a normal browser User-Agent.
