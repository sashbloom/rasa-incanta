import { ArrowLeft } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiFetch } from '../api'
import { OBJECTIVE_LABEL, shortDate } from '../format'
import type { Action, DealDetail as Detail } from '../types'
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

function ActionItem({ action }: { action: Action }) {
  return (
    <article className="border-t border-line py-4 pl-4 shadow-[inset_3px_0_0_var(--line)]">
      <div className="flex items-start gap-2.5">
        <Flask objective={action.objective} />
        <span className="t-label whitespace-nowrap pt-0.5">{OBJECTIVE_LABEL[action.objective] ?? action.objective}</span>
        <p className="m-0">{action.action}</p>
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

export function DealDetail({ dealId, backTo }: { dealId: string; backTo: string }) {
  const [deal, setDeal] = useState<Detail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setDeal(null)
    setError(null)
    apiFetch<Detail>(`/api/deals/${dealId}`)
      .then((d) => live && setDeal(d))
      .catch((e: Error) => live && setError(e.message))
    return () => {
      live = false
    }
  }, [dealId])

  if (error) return <p className="mt-8">{error === 'Deal not found.' ? 'This deal is not on your boards.' : error}</p>
  if (!deal) return <p className="t-meta mt-8">Loading the deal.</p>

  const note = trustNote(deal.segments)
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
      </dl>

      <IngredientBar key={deal.id} segments={deal.segments} />
      {note && <p className="mt-3 mb-7">{note}</p>}

      <section aria-labelledby="actions-heading" className="mt-7">
        <h3 id="actions-heading" className="t-section m-0 mb-2">This week's actions</h3>
        {deal.actions.length === 0 ? (
          <p className="border-t border-line pt-4">No actions for this deal yet this week.</p>
        ) : (
          deal.actions.map((a) => <ActionItem key={a.id} action={a} />)
        )}
      </section>
    </div>
  )
}
