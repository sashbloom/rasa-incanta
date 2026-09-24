const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "2026-09-09" -> "9 Sep" */
export function shortDate(iso: string | null | undefined): string {
  if (!iso) return ''
  const [, m, d] = iso.slice(0, 10).split('-').map(Number)
  return m && d ? `${d} ${MONTHS[m - 1]}` : iso
}

/** "2026-09-21" -> "21 September 2026" */
export function longDate(iso: string): string {
  const full = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
    'October', 'November', 'December']
  const [y, m, d] = iso.split('-').map(Number)
  return `${d} ${full[m - 1]} ${y}`
}

export const OBJECTIVE_LABEL: Record<string, string> = {
  advance: 'Advance',
  unblock: 'Unblock',
  reframe: 'Reframe',
  nurture: 'Nurture',
  re_engage: 'Re-engage',
}
