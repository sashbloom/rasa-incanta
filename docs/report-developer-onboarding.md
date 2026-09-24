# You've been asked to build a Practus report. Start here.

**Who this is for:** someone new who has just been handed "build a report" and
has not seen how we do it. No prior context assumed.

**What you'll have at the end:** a report deployed on Railway, reading the data
it needs, that appears inside the Practus Portal with people already signed in.

**Written 9 Sep 2026** from the six reports that exist, with the deployment
region and the volume/replica limit added 23 Sep after both were measured. Everything here is what
those repos actually do, not a proposal — where reports disagree, this says so
and says which one to copy.

**Read this first, then
[building-a-new-report.md](./building-a-new-report.md)** — that one is the
integration contract in detail, and it is where the traps live. This file is the
orientation.

---

## What a "report" is here

A small, self-contained web app that answers one question for one audience —
"which deals did we lose and why", "which follow-ups are overdue". It has its own
repo, its own Railway service, its own database, and its own owner. **Six exist.**

**Reports are not part of the portal codebase, and you do not need access to it.**
You build and deploy independently. The portal proxies your app so people reach
it without a second login. That is the whole relationship.

**You keep your stack.** Nobody will ask you to move credentials into a shared
service, adopt someone else's database layout, or rewrite your logic. That was
considered and taken off the table — rebuilding six UIs buys nothing anyone can
see.

---

## Pick your stack: FastAPI + React unless you have a reason

| Report | Backend | Frontend |
|---|---|---|
| TAT Tracker | FastAPI | React + Vite |
| Lost Deals | FastAPI | React + Vite |
| PPP / EP-EL | FastAPI | React + Vite |
| Meeting Coaching | Express (Node) | server-rendered, no framework |
| QC | Next.js | Next.js |

**Default to FastAPI + React + Vite.** Three of six use it, so it is the stack
with the most working examples to copy, the most people who can help, and the one
this documentation is written against.

Next.js is fine and makes the portal integration a single line (`basePath`).
Express is fine. **Do not pick something nobody else here runs** unless the report
genuinely needs it — you will be the only person who can debug it at 6pm.

---

## Where your data comes from

Three sources, and most reports use more than one.

### Zoho CRM — via a read-only Postgres mirror, not the REST API

```python
REQUIRED_ENV_VARS = ["ZOHO_DB_HOST", "ZOHO_DB_NAME", "ZOHO_DB_USER", "ZOHO_DB_PASSWORD"]
# also ZOHO_DB_PORT, ZOHO_DB_SSLMODE
```

An ADF/Databricks pipeline mirrors Zoho into Postgres and you query that. **It is
read-only — you cannot write back to Zoho through it.** Ask for the credentials;
do not go looking for the Zoho REST API, which is a different and slower path.

`Lost-Deals-Report/db.py` is the reference: about twenty lines, SQLAlchemy engine,
credentials from environment variables.

### Microsoft 365 — Graph API, with a service mailbox

Mail, calendar and subscriptions come from Graph using a refresh token for a
service account. TAT does this for meeting follow-ups.

**Two traps that have both bitten:** the refresh token is usually stored in your
database and takes precedence over the environment variable — so pasting a fresh
one into Railway does nothing if a dead one is in the DB. And a Graph token dies
eventually, so **build the expiry alert on day one and make sure it can actually
send** (see below).

### Read.ai — meeting transcripts and summaries, via webhook

Read.ai posts to an endpoint you expose. **Sign it.** Verify an HMAC over the body
against a shared secret, and **refuse when the secret is unset** — see the
fail-closed rule below, which exists because a report did the opposite.

### And your own store

Everything the report itself owns — items, assignments, user accounts — lives in
your own database. SQLite on a Railway volume is normal here and fine.

**If you use SQLite, mount a volume and point at it.** A file inside the container
is wiped on every redeploy:

```
Railway volume mounted at /data,  DB_PATH=/data/yourapp.db
```

Set the variable *and* mount the volume. One without the other silently loses
data, and nobody notices until a deploy. **Two reports and one agent have now
shipped with the variable unset or the path hardcoded next to the source** — in
each case a feature that wrote data appeared to work for days and forgot
everything on the next deploy.

⚠️ **Know what a volume costs you, before you choose one.** Railway's own
documentation: *"Replicas cannot be used with volumes."* So a service with a
volume can **never** be horizontally scaled — not a setting to tune later, a
hard limit. That is usually fine for a report, and it is worth knowing when you
choose rather than when you need to grow. If you expect real concurrency, put
the state in Postgres and keep the service stateless.

---

## Deploying: Railway, one service, Singapore

Every report is a single Railway service serving both its API and its built
frontend.

**Deploy in Southeast Asia (Singapore).** Not a preference — measured. Our
Postgres is in Azure Central India, and every service used to sit in US West.
One trivial query cost **~470 ms**; after moving everything to Singapore the same
query costs **~124 ms**, and the tail improved far more than the median (p95 at
25 concurrent fell from ~3,000 ms to ~511 ms). A report deployed in US West pays
that difference on every query, and it is the single cheapest decision on this
page. Set it under Scale → Regions & Replicas. **Do not split the frontend into its own service** — PPP did, and had
to be merged back, because a static file server cannot proxy and neither half
could serve both the app and the identity handoff.

Two build styles are in use:

- **Dockerfile** — TAT, PPP. Explicit, and what to use if you need system packages.
- **nixpacks.toml** — Lost Deals. Less to write.

Either way the pattern is: install Python deps, build the frontend, serve
everything from one process.

```dockerfile
RUN pip install --no-cache-dir -r requirements.txt
RUN cd frontend && npm ci && npm run build
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
```

**Note `sh -c` around the command.** Railway provides `$PORT` at runtime; without
a shell the variable is passed literally and the container binds to a port called
`$PORT`. This took a production report down for two minutes.

**Add a healthcheck** in `railway.toml` / `railway.json`:

```toml
[deploy]
healthcheckPath = "/health"
```

Then **`/health` must keep answering at your service's root, forever.** It becomes
a constraint later: a change that moves your whole app under a path prefix fails
the healthcheck and Railway rolls the deploy back.

---

## Repo layout

```
your-report/
  api/            FastAPI app; main.py holds the routes
  frontend/       Vite + React; frontend/dist is built into the image
  scripts/        one-off jobs — seeding, re-authorising a mailbox
  requirements.txt
  Dockerfile  or  nixpacks.toml
  railway.toml
  .env.example    every variable your code reads, with NO values
  README.md
```

`api/main.py` holding all routes is what the existing reports do. It grows large
— TAT's is 3,000 lines — and that is survivable, but split it once you are past a
few hundred if you have the choice.

---

## Build it portal-ready from line one

This is the part that is nearly free on day one and expensive later. Full detail
in [building-a-new-report.md](./building-a-new-report.md); the short version:

**1. Serve under a prefix as well as your own root.** Pick a slug — `tat`, `qc` —
and serve at `/reports/<id>/` too. In Vite that is `base: '/reports/<id>/'`; in
Next.js it is `basePath`. Your router needs the same prefix as its basename.

**2. One function for every API call.** Not a shared constant — a function:

```ts
export async function apiFetch(path: string, options = {}) {
  return fetch(`${API_BASE}${path}`, options)
}
```

A constant can be bypassed by a component calling `fetch('/api/…')` directly, and
six of them were. A function cannot, and it gives you one place to add an auth
header later.

**3. Keep your own login, and add a branch in front of it** for requests carrying
the portal's key plus a verified email. Two doors: portal visitors walk straight
in, your own URL still asks for a password.

**4. Copy the user schema, do not invent one.** Three reports share it:

```sql
users              id, username, password_hash, role, is_active, ms_email
user_allowed_sbus  (user_id, sbu)      -- access is a join table
```

---

## Four rules that are not style

Each of these is a real incident in a real report.

**Fail closed. Always.** A check that skips itself when its secret is missing is
not a check:

```python
if not secret:
    return True        # ← unset secret means every request is "valid"
```

That shipped. Unset must mean *refuse*.

**Never ship a literal default for a secret.** `os.getenv("APP_SECRET",
"change-me")` publishes your signing key in the repository forever. Read it and
refuse to boot without it.

**Every variable your code reads goes in `.env.example`, with no value.** One
report's template documented twelve variables, seven of which nothing read, while
ten the code did read were missing — and both of its fail-open secrets were in
the missing set. That is the mechanism, not bad luck: a deployer configures what
the template mentions, and everything else silently takes its insecure default.
And a template with a real key in it is a leaked key, because `COPY . .` puts it
in your image.

**If your monitoring cannot send, say so at startup.** A report had a daily
token-expiry check whose alert function returned `False` on its first line
because the SMTP variables were never set. The token died and nothing told
anyone. **A monitor whose only output channel is unconfigured is worse than no
monitor, because the team believes it is covered.**

---

## Before you ask for it to be added to the portal

```bash
S=https://your-service.up.railway.app

curl -s $S/health                                    # 200 JSON
curl -s $S/reports/<id>/ | grep -oE '(src|href)="/[^"]*"'
                                                     # every local path prefixed
curl --path-as-is "$S/%2e%2e%2f%2e%2e%2f%2e%2e%2fetc/passwd"
                                                     # must NOT return a file
```

**Check the body, not the status code.** A single-page app answers *everything*
with 200, including nonsense paths, because the catch-all serves `index.html`. A
200 tells you the fallback ran, nothing more. This has misled us repeatedly —
including nearly filing a live security hole as fine.

**And use three or more `../` levels in that traversal check.** A two-level vector
reads as safe even against vulnerable code, because `../../` from
`/app/frontend/dist` lands on `/app/etc/passwd`, which never existed.

---

## Then send us

1. Your service URL and your chosen id.
2. That every local asset path sits under `/reports/<id>/`.
3. That your own root still works, `/health` included.
4. Whether your report shows different data to different people.

We set one Railway variable and it appears in the portal on the next page load.
No deploy on either side, and your report keeps working throughout.

---

## Who to ask

- **Portal integration, or "is my report ready"** — the portal team. Send the
  three checks above.
- **Zoho mirror credentials, Graph service account** — ask before building
  against a guess about what the data looks like.
- **An existing report to copy** — `Lost-Deals-Report` is the cleanest small
  example: FastAPI, React, its own users table, portal identity, roughly 175 +
  159 lines for the whole auth layer.

**Read one existing report end to end before writing yours.** It is an hour, and
it is the fastest way to absorb the conventions this document can only list.
