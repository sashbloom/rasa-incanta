# Building a report that drops into the Practus Portal

**Who this is for:** anyone starting a new internal report or tool that people
will open from the Practus Portal.

> **Brand new to building reports here?** Read
> [report-developer-onboarding.md](./report-developer-onboarding.md) first — the
> stack, the data sources, how deployment works and the repo layout. This file
> assumes you know how to build the report and covers making it portal-ready.

**The promise:** follow this and integration is a Railway variable on our side —
about ten minutes. Skip it and integration becomes a project on yours, because
every item below has already cost a real team real days.

**Written 9 Sep 2026**, from what actually went wrong integrating six reports.
Every claim here is something we hit, not something we imagined. Where a rule
exists because of a specific incident, the incident is named — that is the part
worth remembering.

If you are retrofitting a report that already exists, read
[cgo-reports-standard.md](./cgo-reports-standard.md) instead: it is the same
contract written as a migration. This file is for day one, where everything is
cheap.

---

## The one-paragraph version

Serve your app under `/reports/<your-id>/` as well as at `/`. Route every API
call through one function. Keep your own login, and let a request that carries
our key plus a verified email skip it. Fail closed everywhere: an unset secret,
an unmapped user and an empty permission list must all mean *no*, never *yes*.
That is the whole contract.

---

## 1. Pick your id and serve under it from day one

Your id is a short slug: `tat`, `qc`, `pipeline-loss`. Everything about your app
lives under `/reports/<id>/`.

**Three things need the prefix, and the third is the one people miss:**

```js
// 1. vite.config.js — the assets
export default defineConfig({ base: '/reports/<id>/' })

// 2. your API client — the calls
const API_BASE = import.meta.env.BASE_URL.replace(/\/$/, '')

// 3. your router — the client-side routes
<BrowserRouter basename="/reports/<id>">
```

**Next.js is NOT one line, and the difference is a 404 on everyone's bookmark.**
Verified against 16.2.10 on 10 Sep 2026, while onboarding `qc`.

`basePath` **moves** the app rather than adding a path: with it set, the bare root
returns **404** and Next does not redirect. Unlike the FastAPI reports — which
mount the prefix *in addition to* the root and keep both — a Next.js report loses
its own URL the moment `basePath` ships. Every existing bookmark breaks.

So a Next.js report needs `basePath` **plus explicit redirects** from the old
paths. Prefer an exact list over a wildcard: `source: "/:path*"` with
`basePath: false` matches the raw incoming path, so it matches its own
*output* and recurses — and it swallows `/reports/<id>/_next/static/…`,
redirecting assets instead of serving them, which is the blank-frame bug arriving
by a third route. `qc` has three pages, so three exact rules cover it with no
wildcard and no way to catch an asset. Write a comment saying the list is
exhaustive **by design**, or someone "simplifies" it back to `/:path*`.

⚠️ **Target the slash-terminated form** — `/reports/<id>/`, not `/reports/<id>`.
See the matcher gap below; the un-slashed form is the one path a middleware
matcher may not cover, and a redirect that aims there lands users on the single
unguarded path.

⚠️ **`middleware.ts` is `proxy.ts` in Next 16**, and the exported function is
`proxy`, not `middleware`. Grepping for the old name finds nothing and invites the
conclusion that a report has no edge protection at all. That conclusion was drawn,
confidently and wrongly, about `qc`.

⚠️ **The middleware matcher has a real gap under `basePath`**
([#73786](https://github.com/vercel/next.js/issues/73786)), and it is narrower and
nastier than the issue title says. Measured on 16.2.10 with
`matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"]` and
`basePath: "/reports/qc"`:

```
/reports/qc                       match = FALSE   ← the gap: bare prefix, no slash
/reports/qc/                      match = true
/reports/qc/login                 match = true
/reports/qc/_next/static/x.js     match = false   (correct)
```

Adding `"/"` to the matcher array closes it and changes nothing without
`basePath`. **If the matcher misses a path, the middleware never runs for it — so
a default-deny gate becomes default-allow.** That is far worse than a redirect
loop, and silent.

**Verify it, do not reason about it.** Next ships a helper for exactly this, but
⚠️ **its documented name is wrong**: the docs say `unstable_doesProxyMatch`; the
only shipped export is **`unstable_doesMiddlewareMatch`**. The docs were updated
for the rename and the export was not, so the documented name fails at import.
Two more things bite: the helper needs
`globalThis.AsyncLocalStorage = AsyncLocalStorage` set first or it throws an
invariant, and it is CJS absent from Next's ESM export map, so it needs
`createRequire` rather than `import`.

**Derive the API base from the framework's own base, never a second literal.**
In Vite, `import.meta.env.BASE_URL` *is* your `base` with a trailing slash. Two
hardcoded copies of the same string drift, and the failure is silent until a
page loads.

### Why the prefix is not optional

We forward the **full** path, prefix included. A root-absolute `/assets/app.js`
in your `index.html` is resolved by the browser against the **portal's** origin,
not yours — so the portal answers with its own SPA fallback: **HTTP 200, content
type `text/html`.** A browser refuses to execute HTML as a script, and your
report renders as a blank white frame.

**That exact bug shipped.** TAT sat in the portal as a blank frame because its
bundle path was `/assets/index-Dx-Y1f1B.js`. The fix moved it to
`/reports/tat/assets/…`, which returns `200 text/javascript`.

### The router is the part we cannot rescue for you

We rewrite **server-side** redirects that escape the prefix — if your backend
answers `Location: /login` we make it `/reports/<id>/login`. **Client-side
navigation is JavaScript changing the URL with no HTTP request**, so nothing on
our side can see it, let alone fix it.

Miss the basename and your app loads, then the first click leaves the prefix and
lands on the portal's router. The symptom is a report that appears and then
vanishes on the first click, which everyone reads as a portal bug. PPP has twelve
client routes and hit this.

---

## 2. Answer at BOTH your own root and the prefix

Do not move your app under the prefix. **Add** the prefix.

Your own URL must keep working, and this is a hard requirement rather than a
courtesy: if your `railway.toml` sets `healthcheckPath = "/health"`, an app that
only answers under the prefix **fails its healthcheck and Railway rolls the
deploy back.**

For FastAPI, wrap the ASGI app — set `root_path`, and **leave `scope["path"]`
alone**:

```python
PORTAL_PREFIX = os.getenv("PORTAL_PREFIX", "/reports/<id>").rstrip("/")

class MountUnderPrefix:
    def __init__(self, inner, prefix): self._inner, self._prefix = inner, prefix
    async def __call__(self, scope, receive, send):
        if self._prefix and scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path == self._prefix or path.startswith(self._prefix + "/"):
                scope = dict(scope)
                scope["root_path"] = self._prefix   # path deliberately untouched
        await self._inner(scope, receive, send)

fastapi_app = app
app = MountUnderPrefix(fastapi_app, PORTAL_PREFIX)
```

Keep the reassignment as the **last lines of the file**. Any `@app.get` after it
raises `AttributeError` at import and your container never starts.

### Do not also strip the prefix from the path — it double-counts

The intuitive version strips the prefix *and* sets `root_path`. **That is
broken, and it looks fine.** Starlette's `get_route_path()` already subtracts
`root_path`, and every nested `Mount` appends its matched segment to the child
`root_path`. Routes declared directly on the app keep working — so your `/api/…`
routes pass — but `/reports/<id>/assets/<file>` 404s, because `StaticFiles`
subtracts the prefix a second time and looks for `assets/assets/<file>`.

You only notice when a page actually loads. Measured, not theorised.

---

## 3. Route every API call through one function

> ### ⚠️ A prefix migration has a CLIENT half, and every tool you will reach for is blind to it
>
> **The rule, and it is the one sentence to remember:** a framework's `basePath`
> rewrites what the **framework** constructs — `<Link>`, router pushes, asset
> URLs. It rewrites **nothing a developer constructs as a string.**
>
> **This took QC's login out in production**, 11 Sep 2026, hours after three
> carefully reviewed deploys. `components/login-form.tsx` did
> `fetch("/api/auth/login")` — a root-absolute literal. `basePath` moved the
> route to `/reports/qc/api/auth/login` and left the caller pointing at a path
> that no longer existed. The 404 page came back as HTML into a `.json()` call:
>
> ```
> /api/auth/login   404 (Not Found)
> Unexpected token '<', "<!DOCTYPE "... is not valid JSON
> ```
>
> Eleven files, so the whole interactive surface died at once: login, logout,
> refresh, mail review, Read.ai review, manual companies.
>
> **Before calling a prefix migration done, grep for what the framework will not
> touch:**
>
> ```bash
> git grep -nE 'fetch\("/|XMLHttpRequest|new WebSocket|action="/|new URL\("/' -- app components lib src
> ```
>
> …plus service-worker scopes and any hand-built absolute path. Run it
> **case-sensitively**: `apiFetch("/api` contains `Fetch("/api`, so a
> case-insensitive grep reports every corrected site as a hit.



Not "use a base URL consistently". **One function**, and no bare `fetch` anywhere
else:

```ts
export const API_BASE =
  import.meta.env.VITE_API_URL ??
  (import.meta.env.PROD ? import.meta.env.BASE_URL.replace(/\/$/, '')
                        : 'http://localhost:8000')

export async function apiFetch(path: string, options: RequestInit = {}) {
  return fetch(`${API_BASE}${path}`, options)   // path starts with '/'
}
```

**Why one function and not a shared constant:** TAT had a `BASE` constant *and*
six components calling `fetch('/api/…')` directly, bypassing it. Fixing the
constant fixed nothing for those six. With one function there is nowhere to
bypass, and later — when you add an auth header — there is exactly one place to
add it.

**Your gate, and run it case-sensitively:**

```bash
grep -rn "fetch('/api\|fetch(\`/api" src/     # must return nothing
```

A case-*insensitive* grep gives false positives, because `apiFetch('/api`
contains `Fetch('/api`.

---

## 4. Two doors: your login, and ours

Keep your own login exactly as it is. Add a branch **in front of it** for
requests that come from the portal.

```
from the portal   → our key + a verified email → straight in, no login screen
from your own URL → no key                     → your normal login
```

We attach two headers to every request we proxy:

```
Authorization: Bearer <CGO_REPORTS_API_KEY>
X-CGO-Portal-User-Email: <the address Microsoft verified>
```

```python
def resolve_portal_user(request) -> tuple[bool, dict | None]:
    """(handled, user).

    handled=False -> not a portal request; fall through to the normal login.
                     Covers the flag being off, no bearer token, AND a WRONG
                     bearer token. So a forged email header with no valid key
                     lands on the ordinary login screen.
    handled=True, user=None -> a real portal request that failed to resolve.
                     Fails CLOSED as 401. Never falls through.
    """
```

**Five rules, each load-bearing:**

1. **Honour the email header only when the key matches.** The header alone is
   forgeable by anyone who finds your public URL. The key is what makes it mean
   anything.
2. **Key valid but email missing or unmapped → 401.** Never fall through. A
   request that proved it came from the portal but names nobody is a broken
   mapping or an attack, not an anonymous visitor.
3. **Never default to full access.** The shape that hides best is a placeholder
   like `someone@TODO` in a user list: a valid string, so it parses, never
   throws, and quietly maps whoever presents it to whatever that row grants. A
   happy-path test does not catch it.
4. **Behind a flag.** `PORTAL_IDENTITY_ENABLED=false` means today's behaviour
   exactly. Ship it off; turn it on when we confirm the key is flowing.
5. **An unset key means REFUSE, not "any key works".**

⚠️ **If you gate at the edge, the portal branch must live there TOO — not only
in your page/handler layer.** Found on `qc`, 10 Sep 2026. Its Next.js
`proxy.ts` runs before any server component, and a portal request carries our two
headers and **no session cookie** — so the edge redirects it to `/login` before
the code that would have recognised the portal ever executes. The branch has to
exist in both places, sharing **one** verification function so they cannot
diverge. Applies to any report with edge middleware, not just Next.js.

### A portal request is a THIRD caller shape. Find every place you decide who a caller is.

This is the rule that caught the most real bugs onboarding `qc`, and it is not
about secrets. Your app was built to recognise two callers: **a browser with a
session cookie**, and **a server-to-server call with a shared secret**. A portal
request is neither — it carries our key and our email header, no cookie, and no
secret of yours.

So **every place the app asks "which of my two callers is this?" needs a third
answer.** Enumerate them; do not assume it is only the login check. On `qc` it
was three places:

| Place | What happened without the third answer |
|---|---|
| the edge middleware | redirected the portal visitor to a login before any page ran |
| `/api/refresh`, gated on its own secret | **"Refresh now" returned 401 for exactly the people the portal is for** |
| `getCurrentUser()` in pages and handlers | would have sent a portal visitor to a login screen they have no password for |

The middle one is the shape to watch: an endpoint gated on a secret **you** issue
will reject the portal, and it fails as a dead button rather than an error page,
so it reads as your bug and the portal quietly becomes read-only.

**Use one resolver, called from every one of those places.** Two implementations
of "is this a portal request" drift, and the drift is silent.

**Return the user in the same shape the rest of your code expects.** The coaching
viewer returned the raw database row: its scoping read `user.repNames` while the
column is `rep_names`, so `repNames` was `undefined` and scoping threw a 500 —
on every scoped user, while working perfectly for admins, who skip scoping. If
your login parses a row into an object, the portal path must use that same
function.

---

## 5. Users and per-person access — the shape three reports already share

Do not invent this. EP/EL, Lost Deals and (soon) TAT use one schema:

```sql
CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('super_admin','admin','user')),
  is_active     INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  ms_email      TEXT              -- the Microsoft address the portal presents
);

CREATE TABLE user_allowed_sbus (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  sbu     TEXT NOT NULL,
  PRIMARY KEY (user_id, sbu)
);
```

- **Access is a join table, not a comma-separated column.** That is what makes
  "grant this person these SBUs" a normal insert/delete, and why the whole admin
  CRUD is about seventy lines.
- **`is_active` is separate from deletion.** Deactivating beats deleting when
  someone leaves, and it keeps audit references intact.
- **The role CHECK lives in the schema**, not in application code.
- **Empty access list means SEE NOTHING.** Fail-closed is the default people get
  wrong. A newly created person sees zero rows until someone grants them.
- **A user's own filters can only narrow within their allowed set.** Someone
  scoped to SBU A requesting `?sbu=B` gets nothing, not B's rows.
- **Scope every derived view too, not just the rows.** TAT's totals, all five
  filter dropdowns and a company-to-last-email map all came from one unscoped
  query — four insertion points, not one. A scoped user seeing totals that do not
  match their rows reads as broken even when the row filter is right.

### `ms_email` has three traps, all already paid for

1. **Uniqueness must be case-insensitive.** `CREATE UNIQUE INDEX … ON
   users(lower(ms_email))`. Without it, `Person@…` and `person@…` are two rows
   and the lookup is ambiguous — whichever the query returns wins, and it can
   differ between calls. No test finds this, because it needs two rows to exist.
2. **Blank must become NULL, never `""`.** An admin clearing the field twice
   otherwise breaks the unique index, and it surfaces as "cannot save user" —
   which nobody connects to a portal mapping.
3. **Read `is_active` in the same query, and never cache the lookup.** Otherwise
   a deactivated person keeps portal access while being correctly locked out of
   your login, and the two doors disagree about whether they work here.

**Show the mapping in your admin UI as its own column.** `NULL` means "cannot
reach this report through the portal" — that is not a neutral state, so render it
as a warning rather than an empty cell. PPP added a "Portal" column for exactly
this.

---

## 6. Fail closed. Every unset secret, every time.

**This is the rule that has caught the most real bugs, and it is always the same
shape:** a check that skips itself when its secret is missing.

```python
def verify_signature(body, sig):
    secret = os.getenv("WEBHOOK_SECRET", "")
    if not secret:
        return True        # ← WRONG. Unset secret = every request "valid".
```

Found live in TAT. Not "skips validation" — it actively reports a **valid**
signature. Anyone could inject forged data.

**Never ship a literal default for a secret.** `os.getenv("APP_SECRET",
"change-me")` means the signing key is a string published in your repository
forever. Read it, and **refuse to boot** if it is missing.

**And the root cause behind both:** TAT's `.env.example` documented twelve
variables, seven of which nothing read, while ten that the code *did* read were
never mentioned. **Both fail-open secrets were in the undocumented set.** That is
not a coincidence — the template defines what a deployer knows to configure, and
everything it omits silently takes its insecure default.

So: **every variable your code reads appears in `.env.example`, with no value.**
A template with a real key in it is a leaked key — `COPY . .` puts it in your
image, where any file-read bug can reach it.

---

## 7. Serving static files: contain, do not blocklist

If you serve a SPA with a catch-all, you will write this:

```python
candidate = os.path.join(DIST, full_path)
if os.path.isfile(candidate):
    return FileResponse(candidate)      # ← arbitrary file read
```

`os.path.join(DIST, "../../../etc/passwd")` escapes, `isfile` says yes, and you
serve it. **Four CGO reports shipped this.** Add containment:

```python
root = os.path.realpath(DIST)
resolved = os.path.realpath(candidate)
if not (resolved == root or resolved.startswith(root + os.sep)):
    return None
```

**Compare resolved absolute paths. Do not blocklist `..`, `%2e`, backslashes.** A
blocklist is a list of the encodings someone thought of; containment is a
statement about where a file may be, and it holds for encodings nobody has
thought of yet.

**Why the literal form looks safe and the encoded form is not:** intermediaries
normalise a literal `../` away before it reaches you, but `%2e%2e%2f` survives —
and the ASGI server percent-decodes it into `scope["path"]` *after* normalisation,
so it arrives already decoded. `curl --path-as-is` is required to reproduce it,
which is why it is invisible to browsers and to any tool that tidies URLs.

---

## 8. Never reflect input into HTML

```python
return HTMLResponse(f"<h2>Error: {request.query_params.get('error')}</h2>")
```

Live reflected XSS, found in TAT's OAuth callback. **Behind the portal this is
much worse than it looks:** we serve your report on the **portal's own origin**,
so a reflected script executes where the portal's Microsoft tokens live —
affecting every user of every report, not just yours.

**Fix by not reflecting.** Log the detail server-side, render a fixed page. The
value never helps the reader, so escaping defends a reflection that has no reason
to exist.

**And check stored data, not just request data.** TAT's follow-up email
interpolated a meeting summary that arrived via an unsigned webhook — so forged
input reached a real client's inbox from a real mailbox. Ask which of your
interpolated values came from *storage*, not just from the request.

If you must render user text as HTML, **escape before converting newlines**:
`html.escape(text).replace("\n", "<br>")`. Reversed, your own `<br>` is escaped
and every multi-line message shows visible tags.

---

## 9. Comparing secrets: use bytes

```python
hmac.compare_digest(header_value, expected)      # ← TypeError on non-ASCII
hmac.compare_digest(header_value.encode(), expected.encode())   # correct
```

`compare_digest` accepts `str` only when **both** are ASCII. Header values are
attacker-controlled, ASGI headers are bytes, and Starlette decodes them
**latin-1** — so a raw high byte becomes a non-ASCII `str` and the comparison
raises, returning **500 instead of 401**.

Not reachable with `httpx` (it refuses to send such a header), so **a test must
send bytes** or it passes for the wrong reason and you conclude it is
unreachable. Found in two services on the same day.

---

## 10. Any gate that reasons about the path must be prefix-aware

This one is subtle and it fails **open**.

Because the prefix mount deliberately leaves `scope["path"]` intact,
`request.url.path` still carries `/reports/<id>`. A gate doing
`request.url.path.startswith("/api/")` therefore sees `/reports/<id>/api/…`,
classifies it as public, and lets it through.

```
                       your own root   behind the portal
request.url.path       /api/echo       /reports/tat/api/echo
scope["root_path"]     ""              /reports/tat
get_route_path(scope)  /api/echo       /api/echo      ← use this
```

```python
from starlette.routing import get_route_path
def route_path(request): return get_route_path(request.scope)
```

**Measured:** a naive gate let an anonymous `POST /reports/tat/api/delete-company`
through with 200 while correctly refusing the unprefixed form.

**It fails open only on the path real users take.** Signing in through the portal
and finding the report works is the test that misses it; curling your own host
returns 401, so a spot-check confirms the gate "works". Both natural
verifications pass while destructive endpoints stand open.

**Prefer gates that never touch the path at all** — a header check has no
prefix to get wrong. And prefer protect-by-default (a gate on the whole app with
a small public allow-list) over adding a dependency route by route, so a route
added in six months is protected because its author did nothing.

---

## 11. Verify it, and check bodies rather than status codes

> ### Format dates with an explicit `timeZone`, or SSR and the browser disagree
>
> React error **#418** is a hydration mismatch on text: the server-rendered HTML
> did not match what the client produced. In a server-rendered app the commonest
> cause is a date.
>
> `toLocaleDateString("en-IN")` formats in **each runtime's own zone** — the
> container's (UTC on Railway) on the server, the viewer's (IST) in the browser.
> Same instant, but anything between **18:30 and 23:59 UTC renders as a different
> DATE**, so the server emits the 10th and the browser the 11th.
>
> QC had this in **thirteen** places — and correctly passed
> `timeZone: "Asia/Kolkata"` in four others, so someone had already hit it and
> fixed only where it bit. Put one formatter in a shared module so the count
> cannot drift again.
>
> ⚠️ **But do NOT pin every call, and an earlier version of this page said to.**
> The rule is narrower:
>
> | Value | Pin a `timeZone`? |
> |---|---|
> | An **instant** — an ISO string, a DB timestamp, anything with a real moment behind it | **Yes.** Both runtimes must agree on which day that moment falls in. |
> | A **locally-constructed calendar date** — `new Date(y, m-1, d)` from a date picker | **No.** Pinning shifts the day the viewer just chose. |
>
> Measured, for a viewer picking 10 Sep where `parseLocalDate` builds local midnight:
>
> ```
> viewer zone        bare        pinned to IST
> UTC                10/9/2026   10/9/2026
> America/New_York   10/9/2026   10/9/2026
> Asia/Kolkata       10/9/2026   10/9/2026
> Asia/Tokyo         10/9/2026    9/9/2026   ← pinning shifts the day
> Australia/Sydney   10/9/2026    9/9/2026
> Pacific/Auckland   10/9/2026    9/9/2026
> ```
>
> Only viewers **east of the pinned zone** are affected, which is why this is easy
> to "verify" and miss: testing in UTC, IST and New York shows no difference at
> all. Both of us did exactly that and drew the right conclusion from evidence
> that could not have supported it. If you check this, **include a zone east of
> the one you are pinning to.**
>
> ⚠️ **And verify your harness, because ours lied twice in opposite directions.**
> Measured on this machine, through the Bash tool:
>
> ```
> asked TZ=UTC                node saw TZ="UTC"       resolved=UTC
> asked TZ=Pacific/Auckland   node saw TZ=undefined   resolved=Asia/Calcutta
> asked TZ=Asia/Tokyo         node saw TZ=undefined   resolved=Asia/Calcutta
> ```
>
> **The variable never reached the process** — and the reason is narrower than
> "values with a slash get dropped", which was my second wrong guess. Four probes
> in the same Git Bash:
>
> ```
> A  TZ=Pacific/Auckland node -e …           process.env.TZ = undefined   ← lost
> B  TZ=UTC              node -e …           process.env.TZ = "UTC"       ← arrives
> C  node -e 'process.env.TZ="Pacific/Auckland"; …'   resolved = Pacific/Auckland
> D  TZ=Pacific/Auckland bash -c 'echo $TZ'  TZ=[Pacific/Auckland]        ← SURVIVES
> ```
>
> **D is the one that settles it.** The value reaches a child `bash` perfectly
> intact, so the shell is not dropping it. It is lost specifically when crossing
> from MSYS into a **native Windows executable** (`node.exe`), where MSYS rewrites
> or discards env values that look like POSIX paths. The slash is the *trigger*
> for that heuristic, not the cause of the loss — and getting that right matters,
> because "slashes get dropped" predicts D would fail, and it doesn't.
>
> So, for this environment:
>
> | | |
> |---|---|
> | `TZ=Zone/City <native.exe>` from Git Bash | **unreliable, silently empty** |
> | `TZ=UTC <native.exe>` | fine — which is the trap: your control case works |
> | the same prefix to another MSYS program | fine — so it is not "Windows ignores TZ" |
> | `process.env.TZ = "Zone/City"` inside the script | fine, every zone honoured |
>
> ### Testing scoping: two restricted users must see DIFFERENT things, not just less
>
> "The restricted user sees fewer rows" is not evidence that scoping works — an
> empty result, a broken query and a correct scope all look the same. Seed data
> for **two different** restricted users and confirm each sees their own slice:
>
> ```
>                        USA company   India company
>   full user                 ✓              ✓
>   restricted, USA SBU       ✓              ✗
>   restricted, India SBU     ✗              ✓
> ```
>
> The two ✗ cells in different columns are what rules out "restricted users just
> see less" as an artefact. Same for a hidden admin panel: verify it renders for
> someone who **should** see it in the same run, or "hidden from everyone,
> including the people who need it" passes as a success.
>
> That last one is not hypothetical. QC's `manual-companies-admin.tsx:168` is
> `if (manualCompanies.length === 0) return null`, so with no seeded data the
> panel renders nothing for anybody — and a first pass showed `full=0,
> restricted=0` and nearly went down as a pass.

> ### A check needs at least one input whose expected answer differs from the others
>
> This is the concrete, checkable form of "watch it fail", and it is what both of
> our false-confidence failures had in common.
>
> My six-zone table had **no row that was supposed to differ**, because the
> harness had silently flattened them all — so "all identical" was
> indistinguishable from "harness broken". The other session's three-zone test had
> **no zone that was supposed to shift**, because all three sat west of the pinned
> zone — so "all identical" was indistinguishable from "the rule is false".
>
> In both cases the check contained nothing that would have failed if the thing
> under test were broken. A uniform result from a check like that tells you
> nothing at all. **Before trusting a table of results, point at the row that
> proves the check can tell the difference.** If there isn't one, you have not
> tested anything.

> Two ways to be wrong here, and we managed both: I first concluded "Windows
> ignores TZ" (too broad — it arrives fine in PowerShell, and the same command
> works on another shell), then that Node had *substituted* a zone (it hadn't).
> Separately, `Asia/Kolkata` legitimately resolves to **`Asia/Calcutta`** — the
> deprecated IANA alias for the same zone — which is a match, not a failure, and
> is easy to misread as substitution.
>
> **So: don't trust the environment for this at all.** Build the instant with
> `Date.UTC(...)` and pass `timeZone` to the formatter — then the check works on
> every platform and every shell. If you do set `TZ`, assert
> `process.env.TZ` arrived *and* `Intl.DateTimeFormat().resolvedOptions().timeZone`
> is what you asked for, counting a known alias as a match.
>
> ⚠️ **It is NOT merely noisy, and an earlier version of this page said it was.**
> React does discard that subtree's server HTML and re-render — so a viewer whose
> own zone matches the intended one ends up with the right date. **Everyone else
> is shown a date that is simply wrong.** In QC that meant the USA and Europe SBU
> users read every date a day off, in a report where every other timestamp is
> deliberately IST. It stops being cosmetic the moment you ask who is reading it.
>
> ⚠️ **A lazy `useState(() => Date.now())` does NOT make a value hydration-safe.**
> The two runtimes still compute different instants. QC carries a comment
> implying otherwise; that instance is harmless only because the value does not
> reach the first render.

> ### ⚠️ The read path working is not evidence the app works
>
> Two QC regressions in two days hid behind a report that rendered perfectly:
> Read.ai meetings silently not arriving, and then **every button dead.** Anything
> server-rendered looks fine while the interactive surface is broken, so
> **"the report renders" is not a smoke test.**
>
> **Load it in a browser with the network panel open and confirm the calls go to
> `/reports/<id>/api/…`.** That is the only check that covers the client half at
> all. Every other tool in this guide — curl, status codes, content types,
> redirect targets, `unstable_doesMiddlewareMatch` — is server-side. Our
> post-deploy list was thorough in one dimension and blind in the other, and that
> is how a total login outage passed review.



**A 200 proves nothing when a SPA fallback answers everything.** This has misled
us repeatedly. Compare the **content type** and the **body**.

Run every case at **both** roots — that matrix is what catches the prefix bugs:

```bash
S=https://your-service.up.railway.app

# assets: every LOCAL path must carry the prefix.
# Absolute https:// URLs (fonts, CDNs) will not match, and are correct.
curl -s $S/reports/<id>/ | grep -oE '(src|href)="/[^"]*"'

# the bundle must be JS, not HTML. text/html here IS the blank-frame bug.
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" $S/reports/<id>/assets/<hash>.js

# both roots, every kind of route
for p in /health / /api/<something> /some/deep/client/route; do
  curl -s -o /dev/null -w "%{http_code} %{content_type}  $p\n" "$S$p"
  curl -s -o /dev/null -w "%{http_code} %{content_type}  /reports/<id>$p\n" "$S/reports/<id>$p"
done

# traversal — USE 3+ LEVELS (see below), and read the BODY
curl --path-as-is "$S/%2e%2e%2f%2e%2e%2f%2e%2e%2fetc/passwd"
curl --path-as-is "$S/%252e%252e%252fetc/passwd"
curl --path-as-is "$S/..%2f..%2fetc/passwd"
```

**After a routing change, the test is not "does the app load" — it is "does an
anonymous request get DENIED".** Those look identical when the app is working,
and only the second one catches a gate that silently stopped running. Ask it of
every protected path, not just the home page.

**And the twin: a RED assertion is not evidence either, until you have confirmed
the check itself is right.** Three checks failed on correct code during one
onboarding — a grep that stripped the very prefix it was testing for, a
content-type compared against the wrong literal, and a `-SimpleMatch` search for
the literal characters of a regex. All three erred toward false alarm, which is
the survivable direction, but none was free. Before believing a red result, run
the check against something you know is good.

**Until you have watched an assertion fail, you do not know whether it tests the
system or itself.** Write the check, then break the thing it checks and confirm it
goes red, then fix it. A suite that has only ever been green is not yet evidence —
this repo has a case of 17 tests passing over a dead code path. It costs one
minute and it is the difference between a test and a decoration.

⚠️ **A two-level traversal vector reads as SAFE against vulnerable code.**
`../../` from `/app/frontend/dist` lands on `/app/etc/passwd`, which never
existed. **Only 3+ levels reaches the real `/etc`** — the obvious test clears a
vulnerable service. This is a false negative built into the natural check.

**Then open it in the portal and watch the network tab.** The API calls must go
to `/reports/<id>/api/…`. A curl sweep cannot see client-side navigation or which
origin the JavaScript actually calls.

---

## 12. Variables

| Variable | Set by | Notes |
|---|---|---|
| `CGO_REPORTS_API_KEY` | us | one value shared by **all** reports. Unset = refuse. |
| `PORTAL_IDENTITY_ENABLED` | you | `false` until we confirm the key is flowing |
| `PORTAL_PREFIX` | optional | defaults to `/reports/<id>` |

**The key must not equal any other bearer secret your service accepts.** We
attach it to **every** proxied request — we cannot know which of your endpoints
care — so any endpoint authenticating off `Authorization` against a secret of its
own is handed a credential to compare. The coaching viewer nearly shipped this:
had its `UPLOAD_TOKEN` matched the reports key, **any portal visitor's browser
would have reached upload and delete.** Assert non-collision at startup.

**Never put this key anywhere a browser bundle is built.** It is a server secret.
Anything compiled into JavaScript is readable by everyone who loads the page.

**Keep our variables out of your service, and yours out of ours.** The portal's
`REPORTS_TICKET_SECRET` was once set on a report that never read it — and because
that report had a file-read bug, our gateway signing key for four reports was
readable from its container.

---

## 13. Two more that cost a day each

**`create_all` creates missing TABLES, never missing COLUMNS.** Adding a field to
a model on an existing database does nothing, and the first write fails with
`table X has no column named Y`. Write the migration. And if your migration
helper swallows exceptions and only prints, a failed migration boots healthy,
passes the healthcheck, and fails at runtime — make it verify itself and raise.

**SQLite cannot `ALTER TABLE ADD COLUMN … UNIQUE`** ("Cannot add a UNIQUE
column"). A separate unique index is the supported equivalent, and it still
permits many NULLs — which is what lets most accounts stay unmapped.

**When several faults produce the same symptom, name them in the log — and log
the FAULT, never the healthy state.** On `qc`, three distinct misconfigurations
all ended at one login screen: the portal's key unset, the portal's key set to a
different value, and the report's own key unset. Identical from outside, and
whoever debugs it reads the code first, because the code is the visible part.

Two rules keep that logging useful rather than noise. **Do not log the healthy
case** — "no bearer token, not a portal request" is true of every direct visitor
and every asset request, so logging it buries the mismatch line sitting next to
it. And **log once per process, not per request**: a misconfiguration affects
every request, so repetition adds nothing and hides everything. Name both
variables and point at the disagreement rather than at the code — and never log
the key, its length, or a prefix of it.

**A monitor whose only output channel is unconfigured is worse than no monitor,
because the team believes it is covered.** TAT had a daily token-expiry check
whose alert function returned `False` on its first line because the SMTP
variables were never set. The token died and nothing said so. If your alerting
cannot send, log that loudly **at startup**, not at the moment it is needed.

---

## What to send us when you are ready

1. Your service's public URL and your chosen id.
2. Confirmation that `curl -s <url>/reports/<id>/ | grep -oE '(src|href)="/[^"]*"'`
   shows every local path under the prefix.
3. Confirmation that your own root still works, `/health` included.
4. Whether your report scopes per user, and whether `PORTAL_IDENTITY_ENABLED` is
   wired but off.

We set one Railway variable, `REPORTS_UPSTREAM_<ID>`, and your report appears in
the portal on the next page load. No portal deploy, and the card keeps working
throughout.

---

## Where the rules came from

Nothing here is style. Each line is an incident:

| Rule | What happened |
|---|---|
| Prefix your assets | TAT rendered as a blank frame in the portal |
| Basename your router | PPP loaded, then vanished on the first click |
| One `apiFetch` | six components bypassed the shared base constant |
| Answer at both roots | a prefix-only move fails the Railway healthcheck |
| `root_path`, don't strip the path | `/reports/<id>/assets/*` 404s while `/api/*` works |
| `get_route_path` in gates | anonymous `POST …/api/delete-company` returned 200 |
| Contain, don't blocklist | four reports shipped an arbitrary file read |
| Test 3+ traversal levels | the two-level vector clears vulnerable code |
| Never reflect into HTML | reflected XSS, on the portal's own origin |
| Check stored data too | forged webhook data reached a client's inbox |
| `compare_digest` on bytes | 500 instead of 401, in two services |
| Fail closed on unset secrets | an unset webhook secret returned "valid" |
| No literal secret defaults | a signing key published in a repository |
| Document every variable | the undocumented ones were the fail-open ones |
| No real values in `.env.example` | a live API key, in the image, readable |
| Case-insensitive `ms_email` | two rows, ambiguous login |
| Empty access = see nothing | the fail-open default people reach for |
| Migrate columns explicitly | `create_all` silently does nothing |
| Alert channels log at startup | a dead token nobody was told about |
| Next.js `basePath` needs redirects | it 404s your own root; every bookmark breaks |
| Redirect with an exact list, not `/:path*` | the wildcard matches its own output and swallows assets |
| Add `"/"` to the middleware matcher | the bare prefix went ungated, so default-deny became default-allow |
| `proxy.ts`, not `middleware.ts`, in Next 16 | grepping the old name concluded a report had no gate at all |
| `unstable_doesMiddlewareMatch` | Next's own docs name an export that does not exist |
| Portal branch at the edge too | the edge redirected portal requests before the app could recognise them |
| Test that anonymous is DENIED | "the app loads" looks the same with the gate off |
| Watch every assertion fail once | otherwise you don't know if it tests the system or itself |
| Verify a red result before believing it | three checks failed on correct code in one onboarding |
| A portal request is a third caller shape | "Refresh now" 401'd for exactly the portal's users |
| One resolver, every decision point | two copies of "is this the portal" drift silently |
| Name same-symptom faults in the log | three misconfigurations, one identical login screen |
| basePath does not rewrite `fetch` | QC's login died in production; 11 files, whole interactive surface |
| Grep the client for absolute paths | the one class every server-side check is blind to |
| Load it in a browser before saying done | our post-deploy list was all curl, and passed a total outage |
| "The report renders" is not a smoke test | two regressions hid behind a healthy-looking read path |
| Instants need an explicit `timeZone` | React #418, and USA/Europe users were shown the wrong date |
| Calendar dates must NOT be pinned | pinning shifts the day for any viewer east of the pinned zone |
| Test east of the zone you pin to | UTC, IST and New York all show no difference — the check passes for free |
| When a check and the code disagree, suspect the check | five times in two days it was the check; verifying it is cheaper |
| Two restricted users, different slices | "sees less" cannot tell a correct scope from a broken query |
