# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/exa_search.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Direct search API (Exa), used in place of Anthropic's own agentic
`web_search_20260209` tool for the evidence-gathering calls that were the
dominant cost of a run — see the plan's "run time" investigation. Live-
confirmed (2026-09-03): a `type="deep-lite"` Exa search with `outputSchema`
returned a fully-cited, correct revenue/EBITDA JSON payload in 5 seconds for
$0.012 — the same shape of task Anthropic's own agentic web_search tool took
~30 MINUTES to do via `secondary_research.extract_revenue_figure()`.

Canonical reference (fetch before changing anything here — this is Exa's
own source of truth, not this docstring): https://docs.exa.ai/reference/search-api-guide-for-coding-agents

Confirmed live: `Authorization: Bearer <key>` AND `x-api-key: <key>` both
authenticate successfully against `POST https://api.exa.ai/search` — this
module uses `x-api-key` (Exa's more commonly documented header).

Two call shapes, matching the two real needs across the four call sites
this replaces:
- `search_and_synthesize()`: Exa's own `outputSchema` + `systemPrompt` do
  retrieval AND grounded structured synthesis server-side, in ONE call —
  used for small, fixed-shape JSON outputs (revenue figure, ownership
  classification, competitor list) where a separate LLM synthesis call
  would be pure overhead on top of an already-fast Exa call.
- `search_many()`: plain concurrent retrieval (title/url/date/highlights
  per result) across several targeted queries, formatted into one evidence
  block — used for `research()`'s free-text narrative, which needs the
  skill's own persona/voice and conflict-resolution framing and doesn't fit
  Exa's own schema constraints (max nesting depth 2, max 10 properties).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

from api.icp.compat import get_settings

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.exa.ai/search"
_DEFAULT_TIMEOUT_SECONDS = 60.0


class ExaError(RuntimeError):
    pass


def _headers() -> dict:
    settings = get_settings()
    if not settings.exa_api_key:
        raise ExaError("EXA_API_KEY is not configured — set it in .env.")
    return {"x-api-key": settings.exa_api_key, "Content-Type": "application/json"}


def _post(body: dict, *, timeout: float) -> dict:
    try:
        response = httpx.post(_SEARCH_URL, headers=_headers(), json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ExaError(f"Exa request failed: {exc}") from exc
    if response.status_code != 200:
        raise ExaError(f"Exa returned {response.status_code}: {response.text[:500]}")
    return response.json()


def search_and_synthesize(
    query: str,
    *,
    output_schema: dict,
    system_prompt: str,
    search_type: str = "deep-lite",
    num_results: int = 6,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[dict, list[dict]]:
    """One Exa call does retrieval AND grounded structured synthesis —
    returns `(content, grounding)`, where `content` matches `output_schema`
    and `grounding` is Exa's own per-field `[{field, citations, confidence}]`
    list. `search_type` should be `"deep-lite"` (live-confirmed ~5s, cheap)
    or `"deep"`/`"deep-reasoning"` for harder synthesis — `"auto"`/`"fast"`
    are built for plain retrieval speed, not synthesis quality, per Exa's
    own guidance."""
    data = _post(
        {
            "query": query,
            "type": search_type,
            "numResults": num_results,
            "systemPrompt": system_prompt,
            "outputSchema": output_schema,
            "contents": {"highlights": True},
        },
        timeout=timeout,
    )
    output = data.get("output") or {}
    content = output.get("content")
    if not isinstance(content, dict):
        raise ExaError(f"Exa did not return structured output.content for query {query!r}: {data!r}")
    grounding = output.get("grounding") or []
    logger.info(
        "Exa search_and_synthesize(%r) -> %d grounded field(s), cost=$%.4f",
        query[:80], len(grounding), (data.get("costDollars") or {}).get("total", 0.0),
    )
    return content, grounding


def _format_result(result: dict) -> str:
    parts = [f"{result.get('title') or '(untitled)'} — {result.get('url')}"]
    if result.get("publishedDate"):
        parts.append(f"published: {result['publishedDate']}")
    highlights = result.get("highlights") or []
    if highlights:
        parts.append("excerpt: " + " ... ".join(h.strip() for h in highlights[:2] if h and h.strip()))
    elif result.get("text"):
        parts.append(f"excerpt: {result['text'][:600]}")
    return " — ".join(parts)


def search_many(
    queries: list[str],
    *,
    num_results_per_query: int = 5,
    max_characters: int | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Issues every query CONCURRENTLY (`ThreadPoolExecutor` — these are
    blocking HTTP calls, live-confirmed ~1-2s each on Exa's `"auto"` type)
    and formats the combined results into one evidence block (title, URL,
    published date, excerpt) ready to paste into a prompt — this is the
    retrieval half of what Anthropic's own agentic web_search tool used to
    do serially, one round at a time, taking minutes.

    `max_characters`, when given, requests full page text (capped) instead
    of query-relevant highlights — use this for a call that genuinely needs
    to read deep into one or two specific pages; `highlights` (the default)
    is the token-efficient choice for most evidence-gathering.

    A single failed query is logged and skipped rather than failing the
    whole batch — one bad query shouldn't cost every other query's real
    results, matching how every other connector in this pipeline degrades
    (evidence_assembler.py's own individually-wrapped calls)."""
    contents: dict = {"text": {"maxCharacters": max_characters}} if max_characters else {"highlights": True}

    def _run_one(query: str) -> tuple[str, list[dict]]:
        data = _post(
            {"query": query, "type": "auto", "numResults": num_results_per_query, "contents": contents},
            timeout=timeout,
        )
        return query, data.get("results") or []

    blocks: list[str] = []
    urls_seen: set[str] = set()
    with ThreadPoolExecutor(max_workers=min(8, len(queries)) or 1) as pool:
        futures = {pool.submit(_run_one, q): q for q in queries}
        for future in futures:
            query = futures[future]
            try:
                _, results = future.result()
            except Exception as exc:
                logger.warning("Exa search_many() query %r FAILED: %s — skipping it.", query[:80], exc)
                continue
            lines = []
            for result in results:
                url = result.get("url")
                if url and url in urls_seen:
                    continue
                if url:
                    urls_seen.add(url)
                lines.append(f"- {_format_result(result)}")
            if lines:
                blocks.append(f"Query: {query}\n" + "\n".join(lines))

    logger.info("Exa search_many(): %d/%d quer(ies) returned usable results, %d unique source(s)", len(blocks), len(queries), len(urls_seen))
    return "\n\n".join(blocks)
