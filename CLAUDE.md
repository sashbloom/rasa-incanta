# Rasa Incanta (RI): project brief

## What this is
Rasa Incanta (RI) is a weekly agent for Practus business development (business owner: Mahak). For every live Zoho
opportunity from Prospect to Negotiated Proposal Sent, it recommends 3–4 next best actions (NBAs),
each backed by evidence, and learns from what each user selects and why. Business goal: close deals
faster, and keep on-hold clients engaged so conversations resume instead of restarting.

It is a standalone agent with its own logic, data connections and deployment. The ICP bot's
account-fit and stakeholder logic is copied into this codebase, not called over its API.

## Vocabulary (use these names in code, UI and docs)
- **Rasa Incanta (RI):** the agent's name. Rasa is Sanskrit for essence or elixir; Incanta is
  Italian for "enchants". Use "Rasa Incanta" in titles and UI, "RI" as the short form, and
  `rasa-incanta` / `rasa_incanta` in identifiers (Langfuse tag, logger).
- **NBA / recommendation:** one specific, evidence-backed action for one deal this week.
  Internal nickname: "the potion".
- **Five input signals** (columns on `context_cards`):
  - `account_fit` and `stakeholder`: ICP logic copied from the ICP bot into our own code (Brick 3).
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
1. Read-only on every source (Zoho, Read.ai, Outlook, Setu). Never write back.
2. Every NBA cites at least one real, sourced fact (call, email, CRM field, case study).
   No evidence means no NBA: flag the gap instead.
3. Company and contact come only from the Zoho Potentials record. Never substitute a more senior contact.
4. History is append-only: never overwrite or delete runs, snapshots, recommendations, reviews or
   decisions. A rerun in the same week regenerates only recommendations nobody has decided on yet.
5. A missing source becomes a gap flag on the card (`no_call_logged`, `no_icp`, `no_contact`,
   `no_setu_match`, `no_mail`, `unverified`). One failing source never fails the whole run.
6. Secrets live only in environment variables (repo-root `.env` locally, Railway Variables in
   production). Only `.env.example` is tracked, and it lists every variable the code reads, with
   no values. Secrets have no literal defaults and fail closed.
7. Only deal fields and call or mail excerpts go to the language model, never whole mailboxes or transcripts.
8. Keep NBAs short: action ≤ 60 words, one-line why-now, 3–4 per deal, at least two different objectives.

## Stack and layout
- Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, Postgres on Railway, pydantic-settings, httpx,
  Anthropic SDK. Models: `LLM_MODEL_ACTIONS` for NBAs, `LLM_MODEL_EXTRACTION` for reasoning factors.
- Practus report standard (`docs/building-a-new-report.md`, `docs/report-developer-onboarding.md`):
  one Railway service in Singapore, one process, served at `/` and under `/reports/rasa-incanta/`
  (`api/prefix.py` sets `root_path` and never strips the path). `/health` stays public at the root.
- The repo-root `Dockerfile` builds the frontend into `api/static`, then runs
  `alembic -c api/alembic.ini upgrade head` and `uvicorn api.main:app`.
- `api/`
  - `main.py`: every route; `config.py`, `db.py`, `models.py`, `migrations/`
  - `auth.py`: own login (scrypt, signed cookie) and the portal branch (`CGO_REPORTS_API_KEY` +
    `X-CGO-Portal-User-Email`). **Not wired in: the board is open, with no sign-in** (decided
    24 Sep 2026). Kept and tested so access control can return: wire `authenticate` into an
    app-wide dependency and scope `visible_deals` by `user_allowed_sbus`.
  - `views.py`: board and deal payloads, always through `visible_deals` (every active deal today)
  - `domain/`: pure business rules, fully unit-tested
  - `sources/<name>.py`: one integration per file
  - `engine/`: the weekly run (thin slice now, full engine in Brick 4). Started by `POST /api/run`
    (background, one at a time, an NBA for every deal); `GET /api/run` is its status.
- `frontend/`: React + Vite + Tailwind. Every API call goes through `apiFetch` in `src/api.ts`,
  never bare `fetch`. The router basename comes from Vite's `BASE_URL`.
- UI direction, brand tokens and screens: `DESIGN.md`.

## Source notes (learned from the ICP bot's setup; our connections stay separate)
- **Zoho:** a read-only Postgres copy of Zoho CRM exists (`zoho_data`; see `ZOHO_PG_*` in the ICP
  bot's `.env.example`). If its `Database_Guide.md` shows the Deals fields listed below, prefer our
  own read-only login to it over the Zoho API: plain SQL, no tokens. Otherwise use the Zoho API with a
  Myrah self-client. Azure Postgres needs `sslmode=require`.
  Confirmed from `icp-bot/Database_Guide.md` and `icp-bot/schema.sql`: tables live in `public`;
  owner via `deals.owner_id` -> `users.full_name`, company via `deals.account_id` ->
  `accounts.account_name`; `sbu`, `client_problem_statement`, `nature_of_potential`, `problem_area_1/2`
  and every `date_became_*` / `date_proposal_sent` column exist. There is no `business_area`,
  `problem_statement_1..3` or `stage_modified_time` (the code tolerates their absence).
  **The mirror has no contacts:** `deals.contact_id` is a Zoho Contacts ID and the Contacts module
  was never exported, so the Postgres path gives no contact name and every card flags `no_contact`.
  Timestamps are `TIMESTAMP` without a zone; we treat them as UTC (unverified).
  `reachout_tracker` (BD calls, emails and meetings per deal, with `client_contact_name`) is a
  candidate Brick 3 signal.
- **ICP bot repo:** a read-only reference clone at `icp-bot/` (gitignored, excluded from pytest and
  Docker). Read it, never edit or commit it. `icp-bot/Database_Guide.md` holds a plaintext login:
  never copy it into this repo, tests, docs or memory.
- **Setu:** read-only Postgres mirror `wisible_data`, database only (its chat API is unreliable; the
  ICP bot retired it). Case studies are `knowledge_chunks` rows with `source_type='case_study'`
  (title `entity_name`, prose `content`, `metadata` industry/service_line; no stable id). SMEs:
  `employees`, plus `knowledge_chunks` types `skill_profile`, `resume`, `partner_profile`.
  `engine/capability.py` uses the ICP bot's P2 matcher and its Claude re-rank (one call per deal);
  if the re-rank fails it falls back to our strict match (same industry, or rare shared terms with
  keyword score >= 0.5), never to the re-rank's own zero-score fallback. SMEs come from the P3 team
  matcher, active people only, no extra call.
- **Conversation from Zoho:** `reachout_tracker` (the deal's outreach log) gives every in-scope
  deal a dated touch with the person met, designation, role and remarks, read in `sources/zoho.py`.
  Teams / in-person / phone clear `no_call_logged`. Emails and phone numbers are scrubbed from
  every fact (`engine/context.scrub`).
- **Outlook:** Microsoft Graph, DELEGATED (Practus policy allows no app-level Mail.Read).
  `sources/outlook.py`: Myrah signs in once from the Sources page (authorization code, confidential
  client). Microsoft redirects to `GET /api/outlook/callback` (works at `/` and under the prefix),
  which checks the one-time state, exchanges the code with the same redirect URI and returns the
  browser to the Sources page with a fixed status word. The redirect URI is MS_REDIRECT_URI, or when
  unset `<origin>/reports/rasa-incanta/api/outlook/callback` from the forwarded host and scheme;
  whichever it is must be registered in Azure. Only a sign-in as MYRAH_MAILBOX is stored; the
  rotating refresh token lives in `oauth_tokens`. Mail is searched per deal on the company name and each contact domain;
  `engine/linking.py` keeps only mail that really concerns the deal; `engine/mail.py` extracts key
  points from previews with LLM_MODEL_EXTRACTION.
- **Read.ai:** the signed workspace webhook, `POST /api/webhooks/readai` (`sources/readai.py`).
  HMAC-SHA256 of the raw body; the key READAI_WEBHOOK_SECRET is tried raw and base64-decoded; unset
  means every delivery is refused. Stored in `meetings` without transcripts; linked to deals by
  participant email domain or the company named in the title. Only meetings from the day the
  webhook goes live (no OAuth backfill).
- **ICP logic:** a full copy of the ICP bot's scoring in `api/icp/` (criteria, gates, scorer,
  verdicts, rule precompute, interpretation, evidence assembly, ownership and secondary research via
  Exa, Practus history, conflict check, Setu P2/P3), plus its reference files in `api/icp/reference/`.
  Only plumbing is adapted: `compat.py` maps settings, `mail_client.py` / `meetings_store.py` serve
  our Outlook and Read.ai data, `pipeline.py` is orchestrator.run() steps 1-8 without reports,
  persona or QC. Its own tests are copied to `tests/icp/` and must keep passing; `tests/icp/conftest.py`
  makes every Zoho/Setu read hermetic. Results are cached per company in `company_icp` for
  ICP_CACHE_DAYS (28); a first score takes minutes per company (Exa research).
- **LLM observability:** follow the Practus platform convention. Every agent uses the same shared
  Langfuse project and keys (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASEURL`), and
  agents are told apart by a tag in code. Ours is `rasa-incanta`. Tracing is a silent no-op when unset.
- **Exa:** `EXA_API_KEY` powers the ICP bot's web research (ownership, scale, financials,
  competitors). Without it those criteria are data gaps, as in the ICP bot, and scores are often
  provisional (Gate 7).

## Working agreements
- Schema change: edit `api/models.py`, then `alembic -c api/alembic.ini revision --autogenerate -m "..."`.
  If autogenerate writes `Text()`, change it to `sa.Text()`. Name every constraint.
  `test_models_and_migrations_are_in_sync` must pass.
- Tests: `python -m pytest -q` from the repo root. Every brick adds tests. Sources are tested with
  recorded fixtures, never live calls. Security checks must be able to fail: traversal uses 3+
  levels against the raw ASGI path, scoping uses two restricted users who see different deals.
- Each source returns a typed result plus its gap flags, so the engine never has to guess what failed.

## Build plan
| # | Brick | Done when |
|---|---|---|
| 1 | Skeleton: settings, schema, health check, Docker, Railway (done) | `/health` is green on Railway |
| 2 | Thin slice: Zoho pull, minimal context, one NBA from Claude, simple page | One real deal shows a real NBA |
| 3 | All signals: Read.ai, Setu, ICP logic (copied in), Outlook; context cards; name matching (built) | Every deal has a context card |
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
