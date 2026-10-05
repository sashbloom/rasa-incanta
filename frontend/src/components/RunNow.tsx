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

type Step = { key: string; doing: string; done: string; noun: string }

// In the order the run works through them (api/engine/run.py).
const STEPS: Step[] = [
  { key: 'pull', doing: 'Pulling deals from Zoho', done: 'Pulled deals from Zoho', noun: 'deals' },
  { key: 'mail', doing: 'Searching Outlook mail', done: 'Searched Outlook mail', noun: 'deals' },
  { key: 'capability', doing: 'Matching Setu case studies', done: 'Matched Setu case studies', noun: 'deals' },
  { key: 'icp', doing: 'Scoring ICP', done: 'Scored ICP', noun: 'companies' },
  { key: 'persona', doing: 'Researching contacts', done: 'Researched contacts', noun: 'contacts' },
  { key: 'cards', doing: 'Building context cards', done: 'Built context cards', noun: 'deals' },
  { key: 'nba', doing: 'Drafting actions', done: 'Drafted actions', noun: 'deals' },
]

/** done / total for the phase that is running, when that phase counts anything. */
function phaseCounts(run: RunView, key: string): [number, number] | null {
  const s = run.stats
  if (key === 'capability' && s.capability_to_do !== undefined) return [s.capability_done ?? 0, s.capability_to_do]
  if (key === 'icp' && s.icp_to_score) return [s.icp_done ?? 0, s.icp_to_score]
  if (key === 'persona' && s.persona_to_do) return [s.persona_done ?? 0, s.persona_to_do]
  if (key === 'nba' && s.nba_to_draft !== undefined) return [s.nba_done ?? 0, s.nba_to_draft]
  return null
}

function seconds(from?: string, to?: string | number): number | null {
  if (!from) return null
  const end = typeof to === 'number' ? to : to ? Date.parse(to) : NaN
  return Number.isNaN(end) ? null : Math.max(0, Math.round((end - Date.parse(from)) / 1000))
}

function duration(s: number): string {
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
}

/** Seconds left in a step, once at least one item has finished to measure the pace by. */
function secondsLeft(startedAt: string | undefined, counts: [number, number] | null, now: number): number | null {
  const elapsed = seconds(startedAt, now)
  if (!counts || elapsed === null || counts[0] === 0 || counts[0] >= counts[1]) return null
  return Math.round((elapsed / counts[0]) * (counts[1] - counts[0]))
}

function leftLabel(left: number): string {
  return left < 60 ? '~1 min left' : `~${Math.round(left / 60)} min left`
}

/** Thin gold bar on a line-colour track; a small flask rides the leading edge while it fills.
 *  Once the step is done the bar and flask fade out. Reduced motion: no flask, no easing. */
function StepBar({ pct, done }: { pct: number; done: boolean }) {
  const shown = done ? 100 : Math.min(100, Math.max(0, pct))
  return (
    <div aria-hidden
      className={`relative ml-6 h-1 rounded-full bg-line transition-opacity duration-500 motion-reduce:transition-none ${done ? 'opacity-0' : 'opacity-100'}`}>
      <div className="h-full rounded-full bg-gold transition-[width] duration-700 ease-out motion-reduce:transition-none"
        style={{ width: `${shown}%` }} />
      <svg viewBox="0 0 12 14" width="12" height="14"
        className="absolute -top-2 -translate-x-1/2 transition-[left] duration-700 ease-out motion-reduce:hidden"
        style={{ left: `${shown}%` }}>
        <path d="M4.5 1h3M5 1v4L1.5 11.2A1.2 1.2 0 0 0 2.5 13h7a1.2 1.2 0 0 0 1-1.8L7 5V1"
          fill="none" stroke="var(--gold)" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M3.2 9h5.6l1.2 2.4a.6.6 0 0 1-.5.9H2.5a.6.6 0 0 1-.5-.9z" fill="var(--gold)" />
      </svg>
    </div>
  )
}

function Progress({ run }: { run: RunView }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), POLL_MS)
    return () => window.clearInterval(t)
  }, [])
  const barSteps = useRef(new Set<string>()) // steps that showed a bar, so it can fade rather than vanish
  const phase = run.stats.phase ?? 'pull'
  const current = STEPS.findIndex((s) => s.key === phase)
  const phases = run.stats.phases ?? {}
  const sources = run.stats.sources ?? {}
  const elapsed = seconds(run.started_at ?? undefined, now)
  const left = secondsLeft(phases[phase]?.started_at, phaseCounts(run, phase), now)
  return (
    <ol aria-label="Run progress" className="m-0 list-none p-0">
      {STEPS.map((step, i) => {
        const state = i < current ? 'done' : i === current ? 'current' : 'todo'
        const counts = state === 'current' ? phaseCounts(run, step.key) : null
        const took = state === 'done' ? seconds(phases[step.key]?.started_at, phases[step.key]?.finished_at) : null
        const stepLeft = state === 'current' ? secondsLeft(phases[step.key]?.started_at, counts, now) : null
        const eta = stepLeft !== null ? leftLabel(stepLeft) : null
        const showBar = state === 'current' ? counts !== null : state === 'done' && barSteps.current.has(step.key)
        if (counts) barSteps.current.add(step.key)
        const skipped = state === 'done' && step.key === 'mail' && sources.outlook && sources.outlook !== 'ok'
        const label = state === 'done' ? step.done : step.doing
        return (
          <li key={step.key} aria-current={state === 'current' ? 'step' : undefined}
            className={`t-meta m-0 py-0.5 ${state === 'todo' ? 'opacity-60' : ''}`}>
            <div className="flex items-baseline gap-2">
            <span aria-hidden className="w-4 shrink-0 text-center">{state === 'done' ? '✓' : state === 'current' ? '●' : '○'}</span>
            <span className="flex-1">
              {label}
              {counts ? `: ${counts[0]} of ${counts[1]} ${step.noun}` : ''}
              {skipped ? ` (${sources.outlook})` : ''}
            </span>
            <span className="shrink-0 tabular-nums">{took !== null ? duration(took) : eta ?? ''}</span>
            </div>
            {showBar && <StepBar pct={counts ? (counts[0] / counts[1]) * 100 : 100} done={state === 'done'} />}
          </li>
        )
      })}
      {phase === 'icp' && (
        <li className="t-meta m-0 mt-1 pl-6">Each new company takes several minutes; scores are reused for four weeks.</li>
      )}
      {elapsed !== null && (
        <li className="t-meta m-0 mt-2 flex justify-between border-t border-line pt-1.5 tabular-nums">
          <span>Elapsed {duration(elapsed)}</span>
          <span>{left !== null ? `${leftLabel(left)} in this step` : 'Estimating time left'}</span>
        </li>
      )}
    </ol>
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
