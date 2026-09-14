import { useEffect, useState } from 'react'

/** The date stamp: time left until the coupon closes, in whole units.
 *  Re-stamps once a minute; past the deadline it reads CLOSED. */
export function Countdown({ to }: { to: string }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 60_000)
    return () => window.clearInterval(id)
  }, [])

  const ms = new Date(to).getTime() - now
  const text = ms <= 0 ? 'Closed' : `Closes in ${remaining(ms)}`
  const spoken = ms <= 0 ? 'Coupon closed' : `Coupon ${text.toLowerCase()}`

  return (
    <span className="stamp on-spot" aria-live="polite" aria-label={spoken}>
      {text}
    </span>
  )
}

function remaining(ms: number): string {
  const mins = Math.floor(ms / 60_000)
  const d = Math.floor(mins / 1440)
  const h = Math.floor((mins % 1440) / 60)
  const m = mins % 60
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m.toString().padStart(2, '0')}m`
  return `${m}m`
}
