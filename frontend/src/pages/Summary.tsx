import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiFetch } from '../api'
import { longDate } from '../format'
import type { SummaryCount, SummaryView } from '../types'

const SHOWN = 8 // deals named under a count; the rest are one click away in the list

function Row({ title, filter, what, data, detail }: {
  title: string
  filter: 'new' | 'moved' | 'pending'
  what: string
  data: SummaryCount
  detail?: (d: SummaryCount['deals'][number]) => string
}) {
  return (
    <section aria-labelledby={`${filter}-heading`} className="border-t border-line py-5">
      <div className="flex items-baseline gap-4">
        <p className="t-page m-0 w-16 shrink-0" aria-hidden="true">{data.count}</p>
        <div className="min-w-0 flex-1">
          <h2 id={`${filter}-heading`} className="t-section m-0">{title}</h2>
          <p className="m-0">{what}</p>
          {data.count > 0 && (
            <Link to={`/?filter=${filter}`} className="t-label mt-1 inline-block text-teal hover:text-teal-deep">
              Open the {data.count === 1 ? 'deal' : `${data.count} deals`} on My week
            </Link>
          )}
        </div>
      </div>
      {data.count > 0 && (
        <ul className="m-0 mt-3 list-none p-0 pl-20">
          {data.deals.slice(0, SHOWN).map((d) => (
            <li key={d.id} className="py-0.5">
              <Link to={`/deals/${d.id}?board=${d.board}&filter=${filter}`} className="text-navy">{d.name}</Link>
              <span className="t-meta ml-2">{detail ? detail(d) : d.stage}</span>
            </li>
          ))}
          {data.count > SHOWN && <li className="t-meta py-0.5">and {data.count - SHOWN} more</li>}
        </ul>
      )}
    </section>
  )
}

/** The week at a glance: new deals, stage moves, decisions pending. Each count opens the filtered list. */
export function Summary() {
  const [view, setView] = useState<SummaryView | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    apiFetch<SummaryView>('/api/summary').then(setView).catch((e: Error) => setError(e.message))
  }, [])

  if (error) return <p className="p-10">{error}</p>
  if (!view) return <p className="t-meta p-10">Loading the summary.</p>
  return (
    <div className="max-w-[760px] px-4 py-6 md:px-12 md:py-10">
      <h1 className="t-page m-0">Summary</h1>
      <p className="t-meta m-0 mb-6">Week of {longDate(view.week_start)}</p>
      {!view.comparison && (
        <p className="mb-4">New deals and stage moves show up once a second week of runs gives us something to compare with.</p>
      )}
      <Row title="New deals" filter="new" what="Deals that first appeared this week." data={view.new} />
      <Row title="Stage moves" filter="moved" what="Deals now at a different stage than last week." data={view.moved}
        detail={(d) => `${d.from} to ${d.to}`} />
      <Row title="Decisions pending" filter="pending" what="Deals with actions this week that nobody has decided yet." data={view.pending} />
      <p className="t-meta m-0 border-t border-line pt-4">
        {view.decided} {view.decided === 1 ? 'deal' : 'deals'} decided this week.
      </p>
    </div>
  )
}
