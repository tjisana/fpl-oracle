import { useEffect, useState } from 'react'
import type { PlanningState, Position, SquadPlayer } from '@/data/planning'

const ROWS: Position[] = ['GK', 'DEF', 'MID', 'FWD']

/** The printed pitch: a line drawing in spot ink, the eleven as numbered
 *  discs in their formation, the bench on a rail beneath. */
export function PitchPanel({ state }: { state: PlanningState }) {
  const starters = state.players.filter((p) => p.isStarting)
  const bench = state.players.filter((p) => !p.isStarting)
  // each line in coupon order, so the pitch and the rows number the same player the same
  const lines = ROWS.map((pos) =>
    starters.filter((p) => p.position === pos).sort((a, b) => indexOf(state, a) - indexOf(state, b)),
  )
  // On a phone the drawing is narrower and taller so the 12-unit type stays
  // ~12px on screen instead of shrinking with a 600-unit viewBox.
  const phone = usePhone()
  const W = phone ? 360 : 600
  const H = phone ? 520 : 420
  // rows sit clear of the centre circle: GK, DEF above the halfway line; MID, FWD below
  const rowY = phone ? [70, 170, 330, 440] : [58, 132, 268, 350]

  return (
    <figure className="pitch-panel m-0">
      <svg viewBox={`0 0 ${W} ${H + 90}`} role="img" aria-label="Your starting eleven and bench on a pitch">
        {/* pitch lines */}
        <rect x={20} y={20} width={W - 40} height={H - 40} className="line" />
        <line x1={20} y1={H / 2} x2={W - 20} y2={H / 2} className="line" />
        <circle cx={W / 2} cy={H / 2} r={40} className="line" />
        <rect x={W / 2 - 90} y={20} width={180} height={50} className="line" />
        <rect x={W / 2 - 90} y={H - 70} width={180} height={50} className="line" />

        {lines.map((line, r) =>
          line.map((p, i) => {
            const gap = (W - 40) / (line.length + 1)
            const x = 20 + gap * (i + 1)
            return <Disc key={p.id} p={p} x={x} y={rowY[r]} n={indexOf(state, p)} slot={gap} />
          }),
        )}

        {/* bench rail */}
        <line x1={20} y1={H + 12} x2={W - 20} y2={H + 12} className="line" strokeDasharray="4 4" />
        <text x={20} y={H + 30} fontSize={11} fill="var(--ink-soft)">
          BENCH
        </text>
        {bench.map((p, i) => {
          const gap = (W - 40) / (bench.length + 1)
          return (
            <Disc key={p.id} p={p} x={20 + gap * (i + 1)} y={H + 58} n={indexOf(state, p)} slot={gap} />
          )
        })}
      </svg>
    </figure>
  )
}

function usePhone(): boolean {
  const [phone, setPhone] = useState(() => window.matchMedia('(max-width: 767px)').matches)
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 767px)')
    const on = (e: MediaQueryListEvent) => setPhone(e.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  return phone
}

function Disc({ p, x, y, n, slot }: { p: SquadPlayer; x: number; y: number; n: number; slot: number }) {
  // A name wider than its slot is squeezed like condensed print, never clipped.
  const size = 11
  const estimated = p.name.length * size * 0.56
  const room = slot - 6
  const squeeze = estimated > room ? { textLength: room, lengthAdjust: 'spacingAndGlyphs' as const } : {}
  return (
    <g>
      <circle cx={x} cy={y} r={16} className={p.isCaptain ? 'disc disc-c' : 'disc'} />
      <text x={x} y={y + 4} textAnchor="middle" fontSize={12} fill={p.isCaptain ? 'var(--paper)' : 'var(--ink)'}>
        {n}
      </text>
      <text x={x} y={y + 32} textAnchor="middle" fontSize={size} fill="var(--ink)" {...squeeze}>
        {p.name}
      </text>
    </g>
  )
}

/** Row number as printed on the coupon: starters first, then bench. */
function indexOf(state: PlanningState, p: SquadPlayer): number {
  const order = { GK: 0, DEF: 1, MID: 2, FWD: 3 } as const
  const sorted = [
    ...state.players.filter((q) => q.isStarting).sort((a, b) => order[a.position] - order[b.position] || b.price - a.price),
    ...state.players.filter((q) => !q.isStarting).sort((a, b) => order[a.position] - order[b.position] || b.price - a.price),
  ]
  return sorted.findIndex((q) => q.id === p.id) + 1
}
