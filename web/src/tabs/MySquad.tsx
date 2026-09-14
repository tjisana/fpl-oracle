import { useState } from 'react'
import { ExternalLink } from 'lucide-react'
import { Checklist } from '@/components/coupon/Checklist'
import { PitchPanel } from '@/components/coupon/PitchPanel'
import { SquadRows } from '@/components/coupon/SquadRows'
import { stampTime } from '@/lib/format'
import type { PlanningState } from '@/data/planning'

const FPL_TRANSFERS = 'https://fantasy.premierleague.com/transfers'

/** The My squad tab in its planning state: the coupon for this gameweek. */
export function MySquad({ state }: { state: PlanningState }) {
  const [view, setView] = useState<'rows' | 'pitch'>('rows')

  return (
    <div className="pt-6">
      <PermLine state={state} />

      <div className="mt-8 grid gap-x-10 gap-y-8 md:grid-cols-12">
        <div className="min-w-0 md:col-span-4 md:col-start-1 md:row-start-1">
          <Checklist state={state} />
        </div>

        <div className="min-w-0 md:col-span-8 md:col-start-5 md:row-start-1 md:row-span-2">
          <div className="flex items-baseline justify-between rule-strong-b pb-2 mb-3">
            <h2 className="head m-0">The fifteen</h2>
            <button className="textbtn" onClick={() => setView(view === 'rows' ? 'pitch' : 'rows')}>
              {view === 'rows' ? 'Pitch view' : 'Coupon rows'}
            </button>
          </div>
          {view === 'rows' ? <SquadRows state={state} /> : <PitchPanel state={state} />}
          <p className="pinned mt-3 m-0 print">
            Backers counted from {state.contributingCreators} creators' videos for gameweek{' '}
            {state.gameweek}. Availability and prices as of {stampTime(state.pricesAsOf)}.
          </p>
        </div>

        {/* The stub: last on a phone, under the checklist on a desktop. */}
        <div className="stub min-w-0 md:col-span-4 md:col-start-1 md:row-start-2">
          <a className="stub-action" href={FPL_TRANSFERS} target="_blank" rel="noreferrer">
            Hand in at FPL <ExternalLink size={16} strokeWidth={2.25} aria-hidden="true" />
          </a>
          <p className="pinned mt-3 m-0">
            Opens the official transfers page in a new tab. Your marks here are yours alone;
            this site never writes to FPL.
          </p>
          <dl className="m-0 mt-6 print">
            <dt className="head">Entry No.</dt>
            <dd className="figure m-0 mt-1 break-all">{state.runId}</dd>
            <dd className="pinned m-0 mt-0.5">the run this coupon was printed from</dd>
          </dl>
        </div>
      </div>
    </div>
  )
}

/** The perm line: the figures the week is played with, as printed column heads. */
function PermLine({ state }: { state: PlanningState }) {
  const cells: { head: string; value: string; note?: string }[] = [
    { head: 'Bank', value: `£${state.bank.toFixed(1)}m` },
    { head: 'Free transfers', value: String(state.freeTransfers) },
    {
      head: 'Chips left',
      value: state.chipsRemaining.length ? state.chipsRemaining.join(' · ') : 'None',
      note: state.chipsUsed.length
        ? `used: ${state.chipsUsed.map((c) => `${c.chip} GW${c.gameweek}`).join(', ')}`
        : undefined,
    },
    { head: 'Team value', value: `£${state.teamValue.toFixed(1)}m` },
    {
      head: 'Overall',
      value: `${state.overallPoints} pts`,
      note: `rank ${state.overallRank.toLocaleString('en-GB')}`,
    },
  ]
  return (
    <dl className="grid grid-cols-2 md:grid-cols-5 gap-x-6 gap-y-4 m-0 rule-strong-b pb-4">
      {cells.map((c) => (
        <div key={c.head}>
          <dt className="head">{c.head}</dt>
          <dd className="figure m-0 mt-1">{c.value}</dd>
          {c.note && <dd className="pinned m-0 mt-0.5">{c.note}</dd>}
        </div>
      ))}
      <p className="pinned m-0 col-span-full">
        As of run {stampTime(state.runAt)}. Nothing here is predicted.
      </p>
    </dl>
  )
}
