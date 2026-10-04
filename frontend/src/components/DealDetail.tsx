import { ArrowLeft } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiFetch, ApiError } from '../api'
import { longDate, OBJECTIVE_LABEL, shortDate, timeIST } from '../format'
import type { Action, DealDetail as Detail, HistoryWeek, SavedDecision } from '../types'
import { Flask } from './Flask'
import { IngredientBar, trustNote } from './IngredientBar'

function Field({ label, value }: { label: string; value: string | null | undefined }) {
  if (!value) return null
  return (
    <div className="flex items-baseline gap-2">
      <dt className="t-label">{label}</dt>
      <dd className="m-0">{value}</dd>
    </div>
  )
}

const MAX_VISIBLE_WEEKS = 4 // earlier weeks shown before "Show older weeks"

/** One action with its tick. Ticked, the flask fills and the left rule turns gold (DESIGN.md). `locked`
 *  is a saved decision: the tick is shown but cannot change. */
function ActionItem({ action, index, ticked, locked, onToggle }: {
  action: Action; index: number; ticked: boolean; locked: boolean; onToggle: () => void
}) {
  const label = `${OBJECTIVE_LABEL[action.objective] ?? action.objective}: ${action.action}`
  return (
    <article className={`border-t border-line py-4 pl-4 ${ticked ? 'shadow-[inset_3px_0_0_var(--gold)]' : 'shadow-[inset_3px_0_0_var(--line)]'}`}>
      <div className="flex items-start gap-2.5">
        <Flask objective={action.objective} level={ticked ? 1 : 0.55} />
        <span className="t-label whitespace-nowrap pt-0.5">{OBJECTIVE_LABEL[action.objective] ?? action.objective}</span>
        <p className="m-0 flex-1">{action.action}</p>
        <input type="checkbox" checked={ticked} disabled={locked} onChange={onToggle} aria-label={`Select action ${index + 1}. ${label}`}
          className="mt-1 h-5 w-5 shrink-0 cursor-pointer accent-teal disabled:cursor-default" />
      </div>
      <dl className="m-0 mt-2 grid grid-cols-[88px_1fr] gap-x-3 gap-y-2">
        <dt className="t-label leading-6">Why now</dt>
        <dd className="m-0">{action.why_now}</dd>
        <dt className="t-label leading-6">Evidence</dt>
        <dd className="m-0 flex flex-wrap gap-1.5">
          {action.evidence.map((e) => (
            <span key={e.fact_id} title={e.excerpt} tabIndex={0}
              aria-label={`${e.source_label} ${e.ref}${e.date ? `, ${shortDate(e.date)}` : ''}: ${e.excerpt}`}
              className="rounded-md border border-line bg-paper px-2 text-[13px] leading-[22px]">
              <b>{e.source_label}</b> {e.ref}{e.date ? ` ${shortDate(e.date)}` : ''}
            </span>
          ))}
        </dd>
        {action.proof?.name && (
          <>
            <dt className="t-label leading-6">Proof</dt>
            <dd className="m-0">{action.proof.name}{action.proof.why ? `: ${action.proof.why}` : ''}</dd>
          </>
        )}
        {action.sme && (
          <>
            <dt className="t-label leading-6">SME</dt>
            <dd className="m-0">{action.sme}</dd>
          </>
        )}
        {action.effort && (
          <>
            <dt className="t-label leading-6">Effort</dt>
            <dd className="m-0 capitalize">{action.effort}</dd>
          </>
        )}
      </dl>
    </article>
  )
}

function icpLine(icp: Detail['icp']): string | null {
  if (!icp) return null
  if (icp.status === 'gate_1_stopped') return 'Could not score: company not identified'
  const lenses = icp.provisional
    ? `client ${icp.client_verdict?.toLowerCase()}, provisional`
    : `client ${icp.client_total}/50, Practus ${icp.practus_total}/50`
  return `${icp.recommendation} (${lenses})`
}

const SAVE_ERROR_FALLBACK = 'Could not save the decision. Try again in a moment.'

/** The saved decision, read-only, with Edit decision. Editing appends a new version; the ones it replaced
 *  stay listed underneath. */
function DecisionView({ decision, justSaved, onEdit }: { decision: SavedDecision; justSaved: boolean; onEdit: () => void }) {
  return (
    <section aria-labelledby="decision-heading" className="mt-8 border-t border-line pt-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 id="decision-heading" className="t-section m-0">Your decision</h3>
          <p role="status" className="t-label m-0 mt-1">
            <span aria-hidden="true" className="mr-1.5 inline-block h-2 w-2 rounded-full bg-green align-middle" />
            {justSaved ? 'Decision saved' : 'Decided'}{' '}
            <span className="font-normal text-slate">
              {timeIST(decision.decided_at)}{decision.version > 1 ? `, version ${decision.version}` : ''}
            </span>
          </p>
        </div>
        <button type="button" onClick={onEdit}
          className="t-label shrink-0 cursor-pointer rounded-md border border-line bg-transparent px-4 py-2 text-navy hover:bg-line/40">
          Edit decision
        </button>
      </div>
      <p className="m-0 mt-3 whitespace-pre-wrap">{decision.rationale}</p>
      {decision.own_action && (
        <p className="m-0 mt-3 whitespace-pre-wrap"><b>Your own action</b><br />{decision.own_action}</p>
      )}
      {decision.earlier.length > 0 && (
        <details className="mt-4">
          <summary className="t-label cursor-pointer">Earlier versions ({decision.earlier.length})</summary>
          {decision.earlier.map((v) => (
            <div key={v.version} className="mt-3 border-t border-line pt-3">
              <p className="t-meta m-0">Version {v.version}, {timeIST(v.decided_at)}, {v.selected_ids.length} ticked</p>
              <p className="m-0 mt-1 whitespace-pre-wrap">{v.rationale}</p>
              {v.own_action && <p className="m-0 mt-1 whitespace-pre-wrap"><b>Own action</b><br />{v.own_action}</p>}
            </div>
          ))}
        </details>
      )}
    </section>
  )
}

/** Your decision: why these and why not the others, an optional action of your own, and Save. Opened
 *  for an edit it starts from the current decision; saving then appends the next version. */
function DecisionForm({ deal, ticked, current, onSaved, onCancel }: {
  deal: Detail; ticked: Set<string>; current: SavedDecision | null
  onSaved: (d: Detail) => void; onCancel: () => void
}) {
  const [rationale, setRationale] = useState(current?.rationale ?? '')
  const [own, setOwn] = useState(current?.own_action ?? '')
  const [saving, setSaving] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const rationaleRef = useRef<HTMLTextAreaElement>(null)

  const save = useCallback(async () => {
    if (!rationale.trim()) {
      setProblem('Add a line of rationale to save this decision.')
      rationaleRef.current?.focus()
      return
    }
    setSaving(true)
    setProblem(null)
    try {
      const saved = await apiFetch<Detail>(`/api/deals/${deal.id}/decisions`, {
        method: 'POST',
        body: JSON.stringify({ selected: [...ticked], rationale, own_action: own.trim() || null }),
      })
      onSaved(saved)
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : SAVE_ERROR_FALLBACK)
    } finally {
      setSaving(false)
    }
  }, [deal.id, ticked, rationale, own, onSaved])

  const onKey = (event: React.KeyboardEvent) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault()
      void save()
    }
  }
  const field = 'mt-1 block w-full rounded-md border border-line bg-white px-3 py-2 font-sans text-[15px] leading-6 text-navy'
  return (
    <section aria-labelledby="decision-heading" className="mt-8 border-t border-line pt-5">
      <h3 id="decision-heading" className="t-section m-0 mb-3">{current ? 'Edit your decision' : 'Your decision'}</h3>
      <label className="t-label block" htmlFor="rationale">Why these, and why not the others?</label>
      <textarea id="rationale" ref={rationaleRef} rows={3} value={rationale} required maxLength={4000} onKeyDown={onKey}
        onChange={(e) => { setRationale(e.target.value); if (problem) setProblem(null) }}
        aria-invalid={problem ? true : undefined} aria-describedby={problem ? 'decision-problem' : undefined}
        className={field} />
      <label className="t-label mt-4 block" htmlFor="own-action">Your own action (optional)</label>
      <textarea id="own-action" rows={2} value={own} maxLength={2000} onKeyDown={onKey}
        onChange={(e) => setOwn(e.target.value)} className={field} />
      {problem && <p id="decision-problem" role="alert" className="m-0 mt-3">{problem}</p>}
      <div className="sticky bottom-0 -mx-4 mt-4 flex items-center justify-between gap-3 bg-paper px-4 py-3 md:static md:mx-0 md:bg-transparent md:px-0">
        <span className="t-meta">{ticked.size} of {deal.actions.length} ticked. Ctrl or Cmd + Enter saves.</span>
        <span className="flex shrink-0 items-center gap-2">
          {current && (
            <button type="button" onClick={onCancel} disabled={saving}
              className="t-label cursor-pointer rounded-md border border-line bg-transparent px-4 py-2 text-navy hover:bg-line/40">
              Cancel
            </button>
          )}
          <button type="button" onClick={() => void save()} disabled={saving}
            className="t-label cursor-pointer rounded-md border-0 bg-gold-web px-4 py-2 text-navy disabled:cursor-default disabled:bg-line">
            {saving ? 'Saving' : 'Save decision'}
          </button>
        </span>
      </div>
    </section>
  )
}

function HistoryItem({ week }: { week: HistoryWeek }) {
  return (
    <details className="border-t border-line py-3">
      <summary className="cursor-pointer">
        <span className="t-label">Week of {longDate(week.week_start)}</span>{' '}
        <span className="t-meta">
          {week.decided ? 'Decided' : 'Not decided'}, {week.actions.length} {week.actions.length === 1 ? 'action' : 'actions'}
        </span>
      </summary>
      <ul className="m-0 mt-2 list-none p-0">
        {week.actions.map((a) => (
          <li key={a.id} className="flex items-start gap-2.5 py-1.5">
            <Flask objective={a.objective} level={a.selected ? 1 : 0.55} size={18} />
            <span className="flex-1">
              <span className="t-label">{OBJECTIVE_LABEL[a.objective] ?? a.objective}</span> {a.action}
              <span className="t-meta block">{a.why_now}</span>
            </span>
            <span className="t-meta shrink-0">{a.selected === null ? 'Not decided' : a.selected ? 'Ticked' : 'Not ticked'}</span>
          </li>
        ))}
      </ul>
      {week.rationale && <p className="m-0 mt-2 whitespace-pre-wrap"><b>Rationale</b><br />{week.rationale}</p>}
      {week.own_action && <p className="m-0 mt-2 whitespace-pre-wrap"><b>Own action</b><br />{week.own_action}</p>}
    </details>
  )
}

/** Earlier weeks, newest first: four visible, the rest behind "Show older weeks". Nothing is ever deleted. */
function History({ weeks }: { weeks: HistoryWeek[] }) {
  const [all, setAll] = useState(false)
  if (weeks.length === 0) return null
  const shown = all ? weeks : weeks.slice(0, MAX_VISIBLE_WEEKS)
  return (
    <section aria-labelledby="history-heading" className="mt-8">
      <h3 id="history-heading" className="t-section m-0 mb-1">
        Earlier weeks <span className="font-normal text-slate">({weeks.length})</span>
      </h3>
      {shown.map((w) => <HistoryItem key={w.week_start} week={w} />)}
      {weeks.length > MAX_VISIBLE_WEEKS && !all && (
        <button type="button" onClick={() => setAll(true)}
          className="t-label mt-2 cursor-pointer border-0 bg-transparent p-0 text-teal hover:text-teal-deep">
          Show older weeks ({weeks.length - MAX_VISIBLE_WEEKS})
        </button>
      )}
    </section>
  )
}

export function DealDetail({ dealId, backTo, reloadKey = 0 }: { dealId: string; backTo: string; reloadKey?: number }) {
  const [deal, setDeal] = useState<Detail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ticked, setTicked] = useState<Set<string>>(new Set())
  const [editing, setEditing] = useState(false)
  const [justSaved, setJustSaved] = useState(false)

  useEffect(() => {
    let live = true
    setDeal(null)
    setError(null)
    setTicked(new Set())
    setEditing(false)
    setJustSaved(false)
    apiFetch<Detail>(`/api/deals/${dealId}`)
      .then((d) => live && setDeal(d))
      .catch((e: Error) => live && setError(e.message))
    return () => {
      live = false
    }
  }, [dealId, reloadKey])

  const locked = !!deal?.decision && !editing // a saved decision is read-only until Edit decision
  const toggle = useCallback((id: string) => {
    setTicked((current) => {
      const next = new Set(current)
      if (!next.delete(id)) next.add(id)
      return next
    })
  }, [])

  // 1 to 4 tick the actions, as long as the focus is not in a field.
  useEffect(() => {
    if (!deal || locked) return
    const actions = deal.actions
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement
      if (target.closest('input, textarea, select, [contenteditable]') || event.metaKey || event.ctrlKey || event.altKey) return
      const n = Number(event.key)
      if (n >= 1 && n <= 4 && actions[n - 1]) toggle(actions[n - 1].id)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [deal, locked, toggle])

  const startEdit = () => {
    setTicked(new Set(deal?.decision?.selected_ids ?? []))
    setEditing(true)
    setJustSaved(false)
  }
  const saved = (d: Detail) => {
    setDeal(d)
    setEditing(false)
    setJustSaved(true)
  }

  if (error) return <p className="mt-8">{error === 'Deal not found.' ? 'This deal is not on your boards.' : error}</p>
  if (!deal) return <p className="t-meta mt-8">Loading the deal.</p>

  const note = trustNote(deal.segments)
  const isTicked = (a: Action) => (locked ? a.selected === true : ticked.has(a.id))
  return (
    <div>
      <Link to={backTo} className="t-label mb-4 inline-flex items-center gap-1 text-teal hover:text-teal-deep md:hidden">
        <ArrowLeft size={16} aria-hidden="true" /> Back to deals
      </Link>
      <h2 className="t-deal m-0">{deal.name}</h2>
      <dl className="m-0 mt-3 mb-6 flex flex-wrap gap-x-7 gap-y-1">
        <Field label="Stage" value={deal.stage} />
        <Field label="Owner" value={deal.owner_name} />
        <Field label="EP" value={deal.ep_involved.join(', ')} />
        <Field label="In stage" value={deal.days_in_stage != null ? `${deal.days_in_stage} days` : null} />
        <Field label="Last touch" value={shortDate(deal.last_touch)} />
        <Field label="Company" value={deal.account_name} />
        <Field label="Contact" value={deal.contact_name} />
        <Field label="ICP" value={icpLine(deal.icp)} />
      </dl>

      <IngredientBar key={deal.id} segments={deal.segments} />
      {note && <p className="mt-3 mb-7">{note}</p>}

      <section aria-labelledby="actions-heading" className="mt-7">
        <h3 id="actions-heading" className="t-section m-0 mb-2">
          This week's actions
          {deal.actions_week && <span className="t-meta ml-2 font-normal">Week of {longDate(deal.actions_week)}</span>}
        </h3>
        {deal.actions.length === 0 ? (
          <p className="border-t border-line pt-4">No actions for this deal yet this week.</p>
        ) : (
          deal.actions.map((a, i) => (
            <ActionItem key={a.id} action={a} index={i} ticked={isTicked(a)} locked={locked} onToggle={() => toggle(a.id)} />
          ))
        )}
      </section>

      {deal.actions.length > 0 && (deal.decision && !editing ? (
        <DecisionView decision={deal.decision} justSaved={justSaved} onEdit={startEdit} />
      ) : (
        <DecisionForm key={`${deal.id}-${editing}`} deal={deal} ticked={ticked} current={deal.decision}
          onSaved={saved} onCancel={() => { setEditing(false); setTicked(new Set()) }} />
      ))}
      <History weeks={deal.history} />
    </div>
  )
}
