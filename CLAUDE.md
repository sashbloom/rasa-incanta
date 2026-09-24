# Rasa Incanta (RI): project brief

## What this is
Rasa Incanta (RI) is a weekly agent for Practus business development (business owner: Mahak). For every live Zoho
opportunity from Prospect to Negotiated Proposal Sent, it recommends 3–4 next best actions (NBAs),
each backed by evidence, and learns from what each user selects and why. Business goal: close deals
faster, and keep on-hold clients engaged so conversations resume instead of restarting.

It is a standalone agent with its own logic, data connections and deployment. The ICP bot is one
input framework, called through its API.

## Vocabulary (use these names in code, UI and docs)
- **Rasa Incanta (RI):** the agent's name. Rasa is Sanskrit for essence or elixir; Incanta is
  Italian for "enchants". Use "Rasa Incanta" in titles and UI, "RI" as the short form, and
  `rasa-incanta` / `rasa_incanta` in identifiers (Langfuse tag, logger).
- **NBA / recommendation:** one specific, evidence-backed action for one deal this week.
  Internal nickname: "the potion".
- **Five input signals** (columns on `context_cards`):
  - `account_fit` and `stakeholder`: from the ICP bot API.
  - `conversation`: Read.ai calls and Outlook mail, through Myrah.
  - `capability`: Setu case studies and Practus SMEs.
  - `deal_state`: Zoho stage, momentum (on track, ahead, stalled, on hold, inactive), last touch.
- **Five objectives** (`app/domain/objectives.py`): advance, unblock, reframe, nurture, re_engage.
  Leadership buckets: stay_warm (nurture, re_engage), reposition (reframe), next_level (advance, unblock).
- **Boards** (`app/domain/stages.py`): prospect, pre_pipeline, pipeline.
- **Ideas Treasury:** Mahak's reference library of engagement plays. Inspiration only, never the
  decision logic. An NBA may carry `treasury_ref` (closest idea, e.g. "4.1") or none.
- **Myrah:** the Microsoft 365 account Mahak operates; it holds read-only access to Zoho, Read.ai and Outlook.

## Non-negotiable rules
1. Read-only on every source (Zoho, Read.ai, Outlook, Setu, ICP bot). Never write back.
2. Every NBA cites at least one real, sourced fact (call, email, CRM field, case study).
   No evidence means no NBA: flag the gap instead.
3. Company and contact come only from the Zoho Potentials record. Never substitute a more senior contact.
4. History is append-only: never overwrite or delete runs, snapshots, recommendations, reviews or
   decisions. A rerun in the same week regenerates only recommendations nobody has decided on yet.
5. A missing source becomes a gap flag on the card (`no_call_logged`, `no_icp`, `no_contact`,
   `no_setu_match`, `no_mail`, `unverified`). One failing source never fails the whole run.
6. Secrets live only in environment variables (`backend/.env` locally, Railway Variables in
   production). Only `.env.example` is tracked.
7. Only deal fields and call or mail excerpts go to the language model, never whole mailboxes or transcripts.
8. Keep NBAs short: action ≤ 60 words, one-line why-now, 3–4 per deal, at least two different objectives.

## Stack and layout
- Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, Postgres on Railway, pydantic-settings, httpx,
  Anthropic SDK. Models: `LLM_MODEL_ACTIONS` for NBAs, `LLM_MODEL_EXTRACTION` for reasoning factors.
- One Railway service from the repo-root `Dockerfile`; it runs `alembic upgrade head`, then uvicorn.
- `backend/app/`
  - `config.py`, `db.py`, `models.py`
  - `domain/`: pure business rules, fully unit-tested
  - `api/`: routers
  - `sources/<name>.py` (Brick 2 onwards): one integration per file
  - `engine/` (Brick 4)
- Frontend (Brick 5): React + Vite in `frontend/`, built into the same image and served by FastAPI.
- UI direction, brand tokens and screens: `DESIGN.md`.

## Source notes (learned from the ICP bot's setup; our connections stay separate)
- **Zoho:** a read-only Postgres copy of Zoho CRM exists (`zoho_data`; see `ZOHO_PG_*` in the ICP
  bot's `.env.example`). If its `Database_Guide.md` shows the Deals fields listed below, prefer our
  own read-only login to it over the Zoho API: plain SQL, no tokens. Otherwise use the Zoho API with a
  Myrah self-client. Azure Postgres needs `sslmode=require`.
- **Setu:** read-only Postgres mirror `wisible_data` (case studies, team roster). Database only.
- **Outlook:** Microsoft Graph with an app registration (tenant, client ID, client secret) and
  Mail.Read limited to Myrah's mailbox. No refresh token to store.
- **Read.ai:** our own OAuth client for a weekly import (no webhook of our own). Refresh tokens can
  rotate, so store tokens in our Postgres (an `oauth_tokens` table), never only in env vars. The ICP
  bot's `readai_oauth_setup.py` and `readai_backfill_cli.py` are a useful reference.
- **ICP bot:** call its run API once per company and cache the result for 4 weeks. It needs a
  service API key before we rely on it; its current config only has CORS, which does not protect
  server-to-server calls.
- **LLM observability:** follow the Practus platform convention. Every agent uses the same shared
  Langfuse project and keys (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASEURL`), and
  agents are told apart by a tag in code. Ours is `rasa-incanta`. Tracing is a silent no-op when unset.
- **News (optional, for stay-warm actions):** the ICP bot uses Exa for search; an `EXA_API_KEY` would
  let nurture and re-engage actions cite recent company news.

## Working agreements
- Schema change: edit `models.py`, then `alembic revision --autogenerate -m "..."`. If autogenerate
  writes `Text()`, change it to `sa.Text()`. `test_models_and_migrations_are_in_sync` must pass.
- Tests: `cd backend && python -m pytest -q`. Every brick adds tests. Sources are tested with
  recorded fixtures, never live calls.
- Each source returns a typed result plus its gap flags, so the engine never has to guess what failed.

## Build plan
| # | Brick | Done when |
|---|---|---|
| 1 | Skeleton: settings, schema, health check, Docker, Railway (done) | `/api/health` is green on Railway |
| 2 | Thin slice: Zoho pull, minimal context, one NBA from Claude, simple page | One real deal shows a real NBA |
| 3 | All signals: Read.ai, Setu, ICP bot, Outlook; context cards; name matching | Every deal has a context card |
| 4 | Engine: deal state, objectives, 3–4 NBAs, evidence validation, per-user generation | A full weekly run on all deals |
| 5 | Board: sign-in, per-user boards, ticks, rationale, own action, export | Users can review and decide |
| 6 | Learning and history: decisions to factors to next run; four-week history; weekly summary | Week 2 uses week 1's decisions |

Committed go-live: Tue 29 Sep 2026. Internal target: a working run by Fri 25 Sep.

## Zoho reference (Deals module API names)
Core: `id`, `Deal_Name`, `Owner`, `Stage`, `City_State`, `Potential_Lead_Source`, `Industry_Type`,
`EP_Involved`, `EL_Involved`, `Account_Name`, `Contact_Name`.
Context: `Modified_Time`, `Stage_Modified_Time`, `Date_on_which_proposal_sent`, `Amount`,
`Nature_of_Potential`, `Business_Area`, `Problem_Statement_1/2/3`, `Problem_Area_1/2/3`.
Lookups (Owner, Account_Name, Contact_Name) come back as objects: read `.name` and `.id`.
Multi-select fields (EP_Involved, EL_Involved) come back as lists. API domain: `https://www.zohoapis.in`.
