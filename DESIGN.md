# Rasa Incanta: UI direction

## The idea
An apothecary's cabinet in Practus colours. The page is calm off-white with a navy rail; each deal
is a labelled bottle on a shelf; opening one shows its ingredients (the five input signals) and this
week's actions. It should feel authentic (strictly Practus brand), chic (restrained, lots of air,
one bold element) and quietly magical: the potion lives in the visuals, never in the wording.

## Tokens
Colours are the Practus brand hex values, no variants.

| Token | Hex | Use |
|---|---|---|
| `navy` | #123250 | Rail, headings, body text. The dominant colour. |
| `paper` | #FBFBFB | Page background (never pure white) |
| `line` | #E0E0E0 | Hairlines, dividers, hollow gap segments, disabled |
| `teal` | #206D81 | Links, tabs, focus ring, structure |
| `teal-deep` | #1D5169 | Hover and active states, the "going cold" mark |
| `teal-web` | #228899 | Stay-warm objectives (nurture, re-engage) |
| `gold` | #FDC13D | Next-level objectives (advance, unblock), selected action rule |
| `gold-web` | #FDB81A | Primary button fill, always with navy text |
| `green` | #8FCC54 | Positive only: decision saved, stage moved up |
| `slate` | #536A80 | Metadata text (navy at 72% on paper, passes AA) |

Rules: gold is never a background area or text on paper; it is only a fill for small marks and the
primary button. No red anywhere; problems are shown in navy with a clear message.

Type: Merriweather 700 for the page title and deal names only; Lato 400 and 700 for everything else.

| Role | Face | Size / line height |
|---|---|---|
| Page title | Merriweather 700 | 28 / 36 |
| Deal name | Merriweather 700 | 22 / 30 |
| Section heading | Lato 700 | 17 / 26 |
| Body | Lato 400 | 15 / 24 |
| Label | Lato 700 | 13 / 20 |
| Metadata | Lato 400, `slate` | 12 / 18 |

Sentence case everywhere. Radius 6px on inputs and buttons, 10px on the detail pane, nothing else rounded.

## The one bold element: the ingredient bar
At the top of every deal, a five-segment bar shows what this week's actions were brewed from:
Fit, Contact, Conversation, Proof, Deal state.
- A filled segment means the signal is present. Hover or focus shows the source and date.
- A hollow, dashed segment is a gap, with its reason ("No call logged in 90 days").
- It tells users at a glance how much to trust the actions below. Nothing else on the page competes with it.

## Objective marks
Each action carries a small flask glyph plus its objective name as text (colour is never the only cue):
- `gold` fill: Advance, Unblock (move to the next level)
- `teal-web` fill: Nurture, Re-engage (stay warm)
- half gold, half teal: Reframe (reposition)

When a user ticks an action, its flask fills and its left rule turns `gold`.

## Layout
Desktop is a three-part workspace, all content left-aligned:

```
+-------------+---------------------------+----------------------------------------------+
| Rasa        | Pipeline  Pre-Pipeline    | Northwind Foods - Working capital            |
| Incanta     | Prospect                  | Stage  Qualified Prospect    Owner  Rao      |
|             |---------------------------| EP  A. Mehta    In stage  34 days            |
| My week     | Qualified Prospect (18)   | Last touch  9 Sep                            |
| Summary     |   Northwind Foods       o |                                              |
| Admin       |   Blue Harbour Retail     | [ Fit ][ Contact ][ Conversation ][ Proof ][ Deal state ]
|             | Proposal Sent (3)         |                                              |
|             |   Kestrel Logistics     * | This week's actions                          |
|             |                           | [flask] Advance  Share a phased close plan  [ ]
|             |                           |   Why now  CFO asked for phasing on the call |
|             |                           |   Evidence  Call 9 Sep   Setu case   Zoho    |
|             |                           | [flask] Reframe  ...                        [ ]
|             |                           | [flask] Nurture  ...                        [ ]
|             |                           |                                              |
|             |                           | Your decision                                |
|             |                           | Why these, and why not the others? [       ] |
|             |                           | Your own action (optional)         [       ] |
|             |                           |                          [ Save decision ]   |
|             |                           | Earlier weeks (4)                        [+] |
+-------------+---------------------------+----------------------------------------------+
```

- Rail (navy, 220px): product mark, My week, Summary, Admin (admins only).
- Deal list (360px): rows grouped by stage, not cards. Right edge shows state marks (new, moved,
  outlier, parked, going cold) and whether the user has decided this week.
- Detail (fluid, max 760px): actions separated by hairlines, not boxed.
- Mobile: list, then detail with a back link; "Save decision" sticks to the bottom.

## Screens in v1
1. **Sign in.**
2. **My week:** the three boards, counts per board, and "decisions pending" for this week. Admins get owner and EP filters.
3. **Deal detail:**
   - header fields;
   - ingredient bar;
   - 3–4 actions, each with a tick, why now, evidence chips (source and date), proof, SME and effort;
   - rationale (required to save) and own action;
   - earlier weeks: four visible, older behind "Show older weeks".
4. **Summary:** new deals, stage moves, lost deals, outliers, going cold, decisions pending. Each count opens the filtered list.
5. **Admin:**
   - Run now;
   - source health: Zoho, Read.ai, Outlook, Setu and ICP bot, each with last sync and status;
   - users and their Zoho names.
6. **Export:** Excel or CSV of a board, keeping the old workbook's columns plus actions, ticks and rationale.

## Copy
Plain, specific and in sentence case. The interface says "actions", not "NBAs" or "potions".
- Button "Save decision"; toast "Decision saved".
- Rationale placeholder: "Why these, and why not the others?"
- Validation: "Add a line of rationale to save this decision."
- Gap: "No call logged in 90 days. These actions use CRM and Setu only."
- Source error: "Zoho didn't respond at 07:00. Showing last week's deals; retrying at 07:30."
- Empty board: "No deals on this board this week."

## Motion and accessibility
- One orchestrated moment: opening a deal fills the ingredient bar left to right (about 600ms in total). Ticking an action fills its flask. Nothing else animates.
- Respect `prefers-reduced-motion`: no animation at all.
- WCAG AA contrast.
- Visible 2px `teal` focus ring.
- Keyboard: `j` / `k` move between deals, `1`–`4` tick actions, Ctrl/Cmd+Enter saves.
- Screen-reader labels on bar segments and flasks, for example "Conversation: no call logged".

## Build notes
- React + Vite + Tailwind, with the tokens as CSS variables in `:root`.
- Merriweather and Lato from Google Fonts, with Georgia and system-ui fallbacks.
- Icons from lucide-react. The flask and ingredient bar are small custom SVG components with `fill` and `level` props.

## Avoid
Grids of identical rounded cards, gradients, shadows under everything, all-caps labels, meta strings
joined with dots, emoji, red, gold text on paper, and potion puns in labels.
