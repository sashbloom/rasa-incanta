import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch, ApiError } from '../api'
import { timeIST } from '../format'
import type { RunView } from '../types'

const POLL_MS = 2000

/** The latest run, a way to start one, and polling while one is going. `onFinished` fires once
 *  when a run this page watched stops running, so the board can reload its deals. */
export function useRun(onFinished: () => void) {
  const [run, setRun] = useState<RunView | null>(null)
  const [starting, setStarting] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const finished = useRef(onFinished)
  finished.current = onFinished

  useEffect(() => {
    apiFetch<RunView>('/api/run').then(setRun).catch(() => setRun(null)) // 404 = no runs yet
  }, [])

  const running = run?.status === 'running'
  const runId = run?.id
  useEffect(() => {
    if (!running) return
    let live = true
    let timer: number
    const poll = async () => {
      try {
        const next = await apiFetch<RunView>('/api/run')
        if (!live) return
        setProblem(null)
        setRun(next)
        if (next.status === 'running') timer = window.setTimeout(poll, POLL_MS)
        else finished.current()
      } catch {
        if (!live) return
        setProblem('Lost contact with the server. Still checking.')
        timer = window.setTimeout(poll, POLL_MS)
      }
    }
    timer = window.setTimeout(poll, POLL_MS)
    return () => {
      live = false
      window.clearTimeout(timer)
    }
  }, [running, runId])

  const start = useCallback(async () => {
    setStarting(true)
    setProblem(null)
    try {
      setRun(await apiFetch<RunView>('/api/run', { method: 'POST' }))
    } catch (e) {
      const already = e instanceof ApiError && e.status === 409 ? (e.body as { run?: RunView } | null)?.run : null
      if (already) setRun(already) // someone else's run is going: follow that one
      else setProblem(e instanceof ApiError ? e.message : 'Could not reach the server. Try again in a moment.')
    } finally {
      setStarting(false)
    }
  }, [])

  return { run, running, starting, problem, start }
}

export function RunNowButton({ running, starting, onClick }: { running: boolean; starting: boolean; onClick: () => void }) {
  const busy = running || starting
  return (
    <button type="button" onClick={onClick} disabled={busy}
      className="t-label shrink-0 cursor-pointer rounded-md border-0 bg-gold-web px-4 py-2 text-navy disabled:cursor-default disabled:bg-line">
      {starting ? 'Starting' : running ? 'Running' : 'Run now'}
    </button>
  )
}

function Progress({ run }: { run: RunView }) {
  const total = run.stats.nba_to_draft
  const done = run.stats.nba_done ?? 0
  const known = total !== undefined
  const share = known ? (total === 0 ? 1 : Math.min(done / total, 1)) : 0
  const label = known ? `Drafting actions: ${done} of ${total} deals` : 'Pulling deals from Zoho and building context cards'
  return (
    <div>
      <div role="progressbar" aria-label="Run progress" aria-valuemin={0} aria-valuemax={known ? total : undefined}
        aria-valuenow={known ? done : undefined} aria-valuetext={label} className="h-1.5 w-full bg-line">
        <div className="h-full bg-teal" style={{ width: `${Math.round(share * 100)}%` }} />
      </div>
      <p className="t-meta m-0 mt-1.5">{label}.</p>
    </div>
  )
}

function outcome(run: RunView): string {
  const at = timeIST(run.finished_at ?? run.started_at)
  const created = run.stats.nba_created ?? 0
  const skipped = run.stats.nba_skipped ?? []
  const actions = `${created} ${created === 1 ? 'action' : 'actions'}`
  if (run.status === 'failed') return `The run at ${at} did not finish. ${run.error ?? ''}`.trim()
  if (run.status === 'partial') {
    const reasons = [...new Set(skipped.map((s) => s.reason))].slice(0, 2).join(' ')
    return `Last run ${at}: ${actions} drafted, ${skipped.length} ${skipped.length === 1 ? 'deal' : 'deals'} got none. ${reasons}`.trim()
  }
  return `Last run ${at}: ${actions} drafted for ${run.stats.deals ?? 0} deals.`
}

/** Progress while a run is going; the outcome of the latest run otherwise. */
export function RunStatus({ run, running, problem }: { run: RunView | null; running: boolean; problem: string | null }) {
  return (
    <div aria-live="polite" className="mt-3">
      {run && running && <Progress run={run} />}
      {run && !running && <p className="t-meta m-0">{outcome(run)}</p>}
      {problem && <p className="m-0 mt-1.5 text-[13px] leading-5">{problem}</p>}
    </div>
  )
}
