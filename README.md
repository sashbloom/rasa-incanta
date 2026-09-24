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

One process serves everything, at `/` and under `/reports/rasa-incanta/`. `/health` is public at both.

## Run locally

Windows (PowerShell), from the repo root:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env          # then set SESSION_SECRET (the app refuses to start without it)
alembic -c api/alembic.ini upgrade head
cd frontend; npm ci; npm run build; cd ..
uvicorn api.main:app --reload
```

Mac or Linux: the same with `python3.12 -m venv .venv && source .venv/bin/activate` and `cp`.

On the Practus network npm may fail with `SELF_SIGNED_CERT_IN_CHAIN`. Run npm with
`NODE_OPTIONS=--use-system-ca` so Node trusts the Windows certificate store; do not turn off
certificate checks.

Generate a session secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

Create someone who can sign in (prompts for a password of 12+ characters):

```
python -m api.cli add-user --username mahak --role admin --sbu India --sbu USA --sbu MEA --sbu Europe
```

A user sees only deals in their SBUs; no SBUs means no deals. Add `--ms-email` to make the
person reachable through the Practus Portal, and `--no-password` for portal-only accounts.

Open http://localhost:8000/ (or http://localhost:8000/reports/rasa-incanta/) and sign in.
`/health` should show `"status": "ok"` and `"database": "ok"`.

For frontend work with hot reload, keep uvicorn running and `cd frontend && npm run dev`
(http://localhost:5173/reports/rasa-incanta/, which calls the API on port 8000).

Run the tests with `python -m pytest -q` from the repo root, and `npx tsc --noEmit` in `frontend/`.

### Run the week

With `ZOHO_PG_*` and `ANTHROPIC_API_KEY` set in `.env`:

```
python -m api.cli run                # pull Zoho, a context card per deal, one NBA
python -m api.cli run --deal <id>    # the NBA for a specific Zoho deal id
```

## Deploy on Railway

1. Push to the private GitHub repo and deploy it as one service; Railway reads `railway.toml`
   and builds the `Dockerfile` (frontend build, then the API image).
2. **Scale, then Regions & Replicas: Southeast Asia (Singapore).** Our Postgres sources are in
   Azure India; US West costs roughly 4x per query.
3. Add a PostgreSQL database in the same project.
4. In the service's Variables set every name in `.env.example` that applies. At minimum:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `ENVIRONMENT` = `production`
   - `SESSION_SECRET` = a fresh random value (the app refuses to start without it)
   - `PORTAL_IDENTITY_ENABLED` = `false` until the portal team asks
5. The healthcheck is `/health`. Every deploy runs migrations before the app starts, and a
   misconfigured secret stops the boot, so Railway keeps the last good deploy.

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
| Report standard | Done: `api/` + `frontend/` layout, one process at `/` and `/reports/rasa-incanta/`, own login plus portal identity (off), users and SBU access, React board (sign-in, My week, deal detail), 109 tests |
