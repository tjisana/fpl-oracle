/** Print a moment the coupon's way: "12 Sept 04:11 UTC". Every figure on the
 *  page is pinned to when it was true, so this is used everywhere. */
export function stampTime(iso: string): string {
  const d = new Date(iso)
  return (
    d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' }) +
    ' ' +
    d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }) +
    ' UTC'
  )
}

/** "Sat 12 Sept 12:30 UTC" for the header strip. */
export function deadlineLabel(iso: string): string {
  const d = new Date(iso)
  const day = d.toLocaleDateString('en-GB', {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    timeZone: 'UTC',
  })
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })
  return `${day} ${time} UTC`
}

/** A signed £m change in print terms: "+0.1", "−0.1". */
export function signedTenths(n: number): string {
  const v = Math.abs(n).toFixed(1)
  return n > 0 ? `+${v}` : `−${v}`
}
