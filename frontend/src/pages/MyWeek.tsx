import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { apiFetch } from '../api'
import { DealDetail } from '../components/DealDetail'
import { RunNowButton, RunStatus, useRun } from '../components/RunNow'
import { longDate } from '../format'
import type { BoardView, WeekView } from '../types'

const BOARDS: BoardView['id'][] = ['pipeline', 'pre_pipeline', 'prospect']

/** My week: board tabs, the deal list grouped by stage, and the open deal's detail. */
export function MyWeek() {
  const { dealId } = useParams()
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const [week, setWeek] = useState<WeekView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0) // bumped when a run finishes: reload deals and detail
  const runner = useRun(() => setReloadKey((k) => k + 1))

  useEffect(() => {
    apiFetch<WeekView>('/api/week').then(setWeek).catch((e: Error) => setError(e.message))
  }, [reloadKey])

  const boardParam = params.get('board') as BoardView['id'] | null
  const boardId = boardParam && BOARDS.includes(boardParam) ? boardParam : 'pipeline'
  const board = week?.boards.find((b) => b.id === boardId)
  const flat = useMemo(() => board?.stages.flatMap((s) => s.deals) ?? [], [board])
  const query = `?board=${boardId}`

  // j / k move between deals on the open board.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement
      if (target.closest('input, textarea, select, [contenteditable]') || event.metaKey || event.ctrlKey) return
      if (event.key !== 'j' && event.key !== 'k') return
      if (!flat.length) return
      const at = flat.findIndex((d) => d.id === dealId)
      const next = event.key === 'j' ? Math.min(at + 1, flat.length - 1) : Math.max(at - 1, 0)
      navigate(`/deals/${flat[at === -1 ? 0 : next].id}${query}`)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [flat, dealId, navigate, query])

  if (error) return <p className="p-10">{error}</p>

  return (
    <div className="flex min-h-full flex-col md:flex-row">
      <section aria-label="Deals"
        className={`w-full shrink-0 border-line md:w-[360px] md:border-r ${dealId ? 'hidden md:block' : ''}`}>
        <div className="px-4 pt-6 pb-3 md:px-6 md:pt-10">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h1 className="t-page m-0">My week</h1>
              {week && <p className="t-meta m-0">Week of {longDate(week.week_start)}</p>}
            </div>
            <RunNowButton running={runner.running} starting={runner.starting} onClick={runner.start} />
          </div>
          <RunStatus run={runner.run} running={runner.running} problem={runner.problem} />
        </div>
        {week?.notice && <p className="mx-4 my-2 md:mx-6">{week.notice}</p>}
        <nav aria-label="Boards" className="flex gap-5 border-b border-line px-4 md:px-6">
          {week?.boards.map((b) => (
            <button key={b.id} type="button" onClick={() => setParams({ board: b.id })}
              aria-current={b.id === boardId ? 'page' : undefined}
              className={`t-label -mb-px cursor-pointer border-0 border-b-2 bg-transparent px-0 py-2.5 text-navy ${
                b.id === boardId ? 'border-teal' : 'border-transparent hover:border-line'}`}>
              {b.label} <span className="font-normal text-slate">{b.count}</span>
            </button>
          ))}
        </nav>
        {!week ? (
          <p className="t-meta px-6 py-4">Loading this week's deals.</p>
        ) : !board || board.count === 0 ? (
          <p className="px-4 py-5 md:px-6">No deals on this board this week.</p>
        ) : (
          board.stages.map((stage) => (
            <div key={stage.stage} className="pt-4">
              <h2 className="t-label m-0 px-4 pb-1 md:px-6">{stage.stage} <span className="font-normal text-slate">({stage.count})</span></h2>
              <ul className="m-0 list-none p-0">
                {stage.deals.map((d) => (
                  <li key={d.id}>
                    <Link to={`/deals/${d.id}${query}`} aria-current={d.id === dealId ? 'true' : undefined}
                      className={`flex items-center justify-between gap-3 px-4 py-2 text-navy no-underline hover:bg-line/40 md:px-6 ${
                        d.id === dealId ? 'bg-line/60 shadow-[inset_3px_0_0_var(--teal)]' : ''}`}>
                      <span className="min-w-0">
                        <span className="block truncate">{d.name}</span>
                        {d.owner_name && <span className="t-meta block">{d.owner_name}</span>}
                      </span>
                      {d.has_actions && (
                        <span className="t-meta shrink-0" title="Actions ready this week">
                          <span aria-hidden="true" className="mr-1 inline-block h-2 w-2 rounded-full bg-teal-web align-middle" />
                          Actions
                        </span>
                      )}
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))
        )}
      </section>

      <section aria-label="Deal detail" className={`min-w-0 flex-1 px-4 py-6 md:px-12 md:py-10 ${dealId ? '' : 'hidden md:block'}`}>
        <div className="max-w-[760px]">
          {dealId ? (
            <DealDetail dealId={dealId} backTo={`/${query}`} reloadKey={reloadKey} />
          ) : (
            <p className="t-meta mt-16">Choose a deal to see this week's actions. Use j and k to move between deals.</p>
          )}
        </div>
      </section>
    </div>
  )
}
