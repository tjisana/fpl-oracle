import { signedTenths } from '@/lib/format'
import type { PlanningState, SquadPlayer } from '@/data/planning'

const POS_ORDER = { GK: 0, DEF: 1, MID: 2, FWD: 3 } as const

/** The fifteen as numbered coupon rows: starters, a bench band, the bench. */
export function SquadRows({ state }: { state: PlanningState }) {
  const starters = state.players.filter((p) => p.isStarting).sort(byPosition)
  const bench = state.players.filter((p) => !p.isStarting).sort(byPosition)
  const max = state.contributingCreators

  return (
    <div>
      <table className="coupon">
        <caption className="sr-only">Your fifteen for gameweek {state.gameweek}</caption>
        <thead>
          <tr>
            <th scope="col" className="w-8">No.</th>
            <th scope="col" className="w-12">Pos</th>
            <th scope="col">Player</th>
            <th scope="col" className="w-14 hidden md:table-cell">Club</th>
            <th scope="col" className="right w-14">£m</th>
            <th scope="col" className="right w-12 hidden md:table-cell">
              <span aria-hidden="true">±</span>
              <span className="sr-only">Change since the last deadline</span>
            </th>
            <th scope="col" className="w-24">Avail.</th>
            <th scope="col" className="hidden md:table-cell">
              Backers <span className="normal-case tracking-normal font-normal">of {max}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {starters.map((p, i) => (
            <Row key={p.id} p={p} n={i + 1} max={max} />
          ))}
          <tr className="bench-band">
            <td colSpan={8}>Bench, in order</td>
          </tr>
          {bench.map((p, i) => (
            <Row key={p.id} p={p} n={starters.length + i + 1} max={max} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Row({ p, n, max }: { p: SquadPlayer; n: number; max: number }) {
  const out = p.status === 'i' || p.status === 'u' || p.status === 's' || p.status === 'n'
  return (
    <tr className={out ? 'out' : undefined}>
      <td className="num">{n}</td>
      <td>{p.position}</td>
      <td>
        <span className="name font-semibold">{p.name}</span>
        {p.isCaptain && <span className="mark c" title="Captain">C</span>}
        {p.isVice && <span className="mark" title="Vice-captain">V</span>}
        {/* on a phone the tally prints under the name; the column is desktop only */}
        <div className="md:hidden mt-1 flex items-center gap-2 num">
          <span>{p.team}</span>
          <Tally n={p.backers} max={max} />
          <span>{p.backers}</span>
        </div>
      </td>
      <td className="hidden md:table-cell">{p.team}</td>
      <td className="right whitespace-nowrap">
        {p.price.toFixed(1)}
        {/* on a phone the change prints under the price (rows are two-line there already) */}
        {p.priceChange !== 0 && (
          <span className="block md:hidden num leading-tight">{signedTenths(p.priceChange)}</span>
        )}
      </td>
      <td className="right num hidden md:table-cell" title={p.priceChange ? `${signedTenths(p.priceChange)}m since the last deadline` : undefined}>
        {p.priceChange !== 0 ? signedTenths(p.priceChange) : ''}
      </td>
      <td>
        <Availability p={p} />
      </td>
      <td className="hidden md:table-cell">
        <Tally n={p.backers} max={max} />
        <span className="ms-2 num">{p.backers}</span>
      </td>
    </tr>
  )
}

function Availability({ p }: { p: SquadPlayer }) {
  switch (p.status) {
    case 'a':
      return <span className="num">—</span>
    case 'd':
      return (
        <span className="avail-doubt" title={p.news ?? undefined}>
          Doubt{p.chance != null ? ` ${p.chance}%` : ''}
        </span>
      )
    case 'i':
      return <span className="avail-out" title={p.news ?? undefined}>Injured</span>
    case 's':
      return <span className="avail-out" title={p.news ?? undefined}>Suspended</span>
    default:
      return <span className="avail-out" title={p.news ?? undefined}>Out</span>
  }
}

/** One cell per contributing creator; filled cells are the backers. */
function Tally({ n, max }: { n: number; max: number }) {
  const cells = Array.from({ length: Math.max(max, 1) }, (_, i) => i < n)
  return (
    <span className="tally" role="img" aria-label={`${n} of ${max} creators back this player`}>
      {cells.map((on, i) => (
        <i key={i} className={on ? 'on' : undefined} />
      ))}
    </span>
  )
}

function byPosition(a: SquadPlayer, b: SquadPlayer): number {
  return POS_ORDER[a.position] - POS_ORDER[b.position] || b.price - a.price
}
