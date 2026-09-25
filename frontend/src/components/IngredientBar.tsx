import { shortDate } from '../format'
import type { Segment } from '../types'

/** The one bold element: five segments showing what this week's actions were brewed from.
 *  Filled = the signal is present (hover or focus shows source and date). Hollow and dashed =
 *  a gap, with its reason written underneath. */
export function IngredientBar({ segments }: { segments: Segment[] }) {
  return (
    <ul className="m-0 grid list-none grid-cols-1 gap-3 p-0 sm:grid-cols-5 sm:gap-1.5" aria-label="What these actions were brewed from">
      {segments.map((s, i) => {
        const note = s.present ? [s.source, shortDate(s.date)].filter(Boolean).join(', ') : s.reason ?? ''
        return (
          <li key={s.key} tabIndex={0} title={note} aria-label={`${s.name}: ${s.present ? note || 'present' : note}`}
            className="rounded-none">
            {s.present ? (
              <span className="segment-fill block h-2.5 bg-teal" style={{ animationDelay: `${i * 100}ms` }} />
            ) : (
              <span className="block h-2.5 border-[1.5px] border-dashed border-line" />
            )}
            <span className="t-label mt-1.5 block">{s.name}</span>
            <span className="t-meta block">{note}</span>
          </li>
        )
      })}
    </ul>
  )
}

export function trustNote(segments: Segment[]): string {
  const present = segments.filter((s) => s.present)
  if (present.length === segments.length) return ''
  if (present.length === 0) return 'No signals yet for this deal.'
  const sources = [...new Set(present.flatMap((s) => s.sources ?? []))].join(', ')
  const names = present.map((s) => s.name.toLowerCase()).join(' and ')
  return `These actions use ${names}${sources ? ` from ${sources}` : ''} only.`
}
