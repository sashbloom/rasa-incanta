import { useId } from 'react'
import type { Objective } from '../types'

const BODY = 'M9 2h6M10 2v6.2L4.6 18.4A2.4 2.4 0 0 0 6.7 22h10.6a2.4 2.4 0 0 0 2.1-3.6L14 8.2V2'
const GLASS = 'M10 2v6.2L4.6 18.4A2.4 2.4 0 0 0 6.7 22h10.6a2.4 2.4 0 0 0 2.1-3.6L14 8.2V2z'

/** Objective mark. Gold for next level, teal for stay warm, half and half for reframe.
 *  `level` 0-1 is how full it is; a ticked action (Brick 5) fills to 1. */
export function Flask({ objective, level = 0.55, size = 22 }: { objective: Objective; level?: number; size?: number }) {
  const id = useId()
  const top = 22 - 14 * Math.min(Math.max(level, 0), 1)
  const warm = objective === 'nurture' || objective === 're_engage'
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" className="shrink-0">
      <defs>
        <clipPath id={`${id}-level`}>
          <rect x="0" y={top} width="24" height={24 - top} />
        </clipPath>
        <clipPath id={`${id}-glass`}>
          <path d={GLASS} />
        </clipPath>
      </defs>
      <g clipPath={`url(#${id}-glass)`}>
        <g clipPath={`url(#${id}-level)`}>
          {objective === 'reframe' ? (
            <>
              <rect x="0" y="0" width="12" height="24" fill="var(--gold)" />
              <rect x="12" y="0" width="12" height="24" fill="var(--teal-web)" />
            </>
          ) : (
            <rect x="0" y="0" width="24" height="24" fill={warm ? 'var(--teal-web)' : 'var(--gold)'} />
          )}
        </g>
      </g>
      <path d={BODY} fill="none" stroke="var(--navy)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
