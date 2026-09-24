# Rasa Incanta (RI)

Weekly next-best-action agent for Practus business development. `CLAUDE.md` has the full brief,
rules and build plan; Claude Code reads it automatically when you open this folder.
`DESIGN.md` has the UI direction, Practus brand tokens and the v1 screens.

## Run locally

Windows (PowerShell):

```powershell
cd backend
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

Mac or Linux:

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

Open http://localhost:8000/api/health. You should see `"status": "ok"` and `"database": "ok"`.
Locally it uses a SQLite file, so no database setup is needed.

Run the tests with `python -m pytest -q` from `backend/`.

## Deploy on Railway

1. Push this folder to a new private GitHub repo.
2. In Railway: New Project, then Deploy from GitHub repo, then pick the repo. Leave Root Directory
   as the repo root; Railway finds the `Dockerfile`.
3. In the same project: New, then Database, then PostgreSQL.
4. In the app service's Variables, add:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `ENVIRONMENT` = `production`
   
   Add the rest from `backend/.env.example` as access lands.
5. Settings, then Networking, then Generate Domain. Open `https://<your-domain>/api/health`.

Every deploy runs the database migrations before the app starts.

## Status

| Brick | State |
|---|---|
| 1. Skeleton | Done: settings, schema (users, runs, deals, snapshots, context cards, recommendations, reviews, decisions), health check, Docker, 23 tests |
| 2. Thin slice | Next |
