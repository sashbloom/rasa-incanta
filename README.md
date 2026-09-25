# Rasa Incanta (RI)

Weekly next-best-action agent for Practus business development. `CLAUDE.md` has the full brief,
rules and build plan; Claude Code reads it automatically when you open this folder.
`DESIGN.md` has the UI direction, Practus brand tokens and the v1 screens. `docs/` holds the
Practus report standards this service follows (portal prefix, identity handoff, fail closed).

```
api/            FastAPI app; main.py holds the routes, api/static is the built frontend
frontend/       Vite + React (built into api/static)
tests/          pytest, recorded fixtures only
requirements.txt, requirements-dev.txt
Dockerfile, railway.toml, .env.example
```

One process serves everything, at `/` and under `/reports/rasa-incanta/`. `/health` answers at both.

**The board is open: there is no sign-in.** Anyone with the URL sees every live deal, so do not
share the Railway domain. The login and portal identity code is kept in `api/auth.py`, tested but
not wired in, and the user tables stay in the schema for when access control returns.

## Run locally

Windows (PowerShell), from the repo root:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env
alembic -c api/alembic.ini upgrade head
cd frontend; npm ci; npm run build; cd ..
uvicorn api.main:app --reload
```

Mac or Linux: the same with `python3.12 -m venv .venv && source .venv/bin/activate` and `cp`.

On the Practus network npm may fail with `SELF_SIGNED_CERT_IN_CHAIN`. Run npm with
`NODE_OPTIONS=--use-system-ca` so Node trusts the Windows certificate store; do not turn off
certificate checks.

Open http://localhost:8000/ (or http://localhost:8000/reports/rasa-incanta/); the board opens directly.

`python -m api.cli add-user` still records users and their SBUs for later; nothing reads them yet.
`/health` should show `"status": "ok"` and `"database": "ok"`.

For frontend work with hot reload, keep uvicorn running and `cd frontend && npm run dev`
(http://localhost:5173/reports/rasa-incanta/, which calls the API on port 8000).

Run the tests with `python -m pytest -q` from the repo root, and `npx tsc --noEmit` in `frontend/`.

### Run the week

With `ZOHO_PG_*` and `ANTHROPIC_API_KEY` set in `.env`, press **Run now** at the top of My week: it
shows a progress bar while the run drafts actions and reloads the deals when it finishes. The
same calls, which also answer under `/reports/rasa-incanta`:

| Call | What it does |
|---|---|
| `POST /api/run` | Starts a full run in the background: Zoho pull, a context card per deal, an NBA for every deal. Returns the run (`202`, status `running`), or `409` if one is already going. |
| `GET /api/run` | The latest run: `running`, `succeeded`, `partial` (some deals got no NBA; `stats.nba_skipped` says why) or `failed` (`error` says why). |
| `GET /api/deals` | Every deal, active or not, with the NBAs from its latest run. |

```bash
curl -X POST http://localhost:8000/api/run
curl http://localhost:8000/api/run        # poll until status is no longer "running"
curl http://localhost:8000/api/deals
```

No sign-in guards these, so anyone with the URL can start a run, and each run pays for one
Claude call per deal. Runs never overlap.

`python -m api.cli run` still exists for one deal from a terminal (`--deal <zoho id>`).

### Connect the sources (once, on Railway)

The **Sources** page shows each source's status from the latest run.

- **Outlook:** in Azure, add the redirect URI the Sources page shows (App registration >
  Authentication > Web > Redirect URIs). By default that is
  `https://<domain>/reports/rasa-incanta/api/outlook/callback`; set `MS_REDIRECT_URI` to use a
  different one. Then press Connect Outlook and sign in as Myrah: Microsoft returns to
  `/api/outlook/callback` and the connection completes by itself. Only Myrah's own sign-in is
  accepted. `MS_CLIENT_SECRET` must be the secret's value, not its ID.
- **Read.ai:** in Read.ai add a workspace webhook for "meeting end" pointed at the address the
  Sources page shows (`https://<domain>/reports/rasa-incanta/api/webhooks/readai`), and set
  `READAI_WEBHOOK_SECRET` to the signing key Read.ai gives you.
- **ICP:** set `EXA_API_KEY`. The first run scores every company (several minutes each, four at a
  time, so expect hours for 83); later runs reuse scores for 28 days.

Rough Claude cost: about one re-rank and one NBA call per deal per run, one small key-points call
per deal with mail, and a few large calls per company the first time it is ICP-scored.

## Deploy on Railway

1. Push to the private GitHub repo and deploy it as one service; Railway reads `railway.toml`
   and builds the `Dockerfile` (frontend build, then the API image).
2. **Scale, then Regions & Replicas: Southeast Asia (Singapore).** Our Postgres sources are in
   Azure India; US West costs roughly 4x per query.
3. Add a PostgreSQL database in the same project.
4. In the service's Variables set every name in `.env.example` that applies. At minimum:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `ENVIRONMENT` = `production`
5. The healthcheck is `/health`. Every deploy runs migrations before the app starts, and a failed
   migration stops the boot, so Railway keeps the last good deploy.

Portal readiness checks (from `docs/building-a-new-report.md`), against the deployed URL:

```bash
curl -s $S/health
curl -s $S/reports/rasa-incanta/ | grep -oE '(src|href)="/[^"]*"'   # every local path prefixed
curl --path-as-is "$S/%2e%2e%2f%2e%2e%2f%2e%2e%2fetc/passwd"          # must not return a file
```

## Status

| Brick | State |
|---|---|
| 1. Skeleton | Done: settings, schema, health check, Docker, Railway |
| 2. Thin slice | Built: Zoho Postgres source, deal upsert, weekly snapshots, minimal context cards, one evidence-checked NBA from Claude. Waiting on `ZOHO_PG_*` and `ANTHROPIC_API_KEY` for the first real run |
| 3. All signals | Built: all five signals. Conversation from the Zoho outreach log, Read.ai webhook meetings and delegated Outlook mail (with key points); capability from Setu with the Claude re-rank and SMEs; account fit and stakeholder from the full ICP port (cached 4 weeks); name matching. First live run happens on Railway |
| Report standard | Done: `api/` + `frontend/` layout, one process at `/` and `/reports/rasa-incanta/`, React board (My week, deal detail), 100 tests. Board open, no sign-in; identity code and user schema kept, unwired |
