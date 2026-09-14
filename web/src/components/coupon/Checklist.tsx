import { useEffect, useState } from 'react'
import { Box } from '@/components/coupon/Box'
import type { PlanningState } from '@/data/planning'

interface Item {
  id: string
  label: string
  detail: string
  /** What the coupon reads after this box is marked — the test strip. */
  after: string
}

/** The week's changes, derived from the run: one box per decision. */
export function Checklist({ state }: { state: PlanningState }) {
  const items = deriveItems(state)
  const [done, setDone] = useStoredSet(`checklist:${state.runId}`)

  return (
    <div>
      <h2 className="head m-0 mb-3 rule-strong-b pb-2">This week's changes</h2>
      <ol className="list-none m-0 p-0 flex flex-col">
        {items.map((it) => {
          const checked = done.has(it.id)
          return (
            <li key={it.id} className="flex gap-3 py-3 rule-b items-start">
              <Box
                id={`chk-${it.id}`}
                checked={checked}
                onChange={(c) => setDone(it.id, c)}
                labelledBy={`lbl-${it.id}`}
              />
              <div className="min-w-0">
                <p id={`lbl-${it.id}`} className="m-0 font-semibold leading-tight">
                  {it.label}
                </p>
                <p className="m-0 mt-1 text-[length:var(--t-sm)] text-ink-soft">{it.detail}</p>
                <p className="m-0 mt-2 print text-[length:var(--t-sm)] leading-snug">
                  <span className="head me-2">{checked ? 'Marked' : 'After'}</span>
                  {it.after}
                </p>
              </div>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

function deriveItems(s: PlanningState): Item[] {
  const items: Item[] = []
  const owned = s.players
  const mostBacked = [...owned].sort((a, b) => b.backers - a.backers)
  const mostCaptained = [...owned].sort((a, b) => b.captainVotes - a.captainVotes)[0]

  if (s.transfer.verdict === 'ROLL') {
    const b = s.transfer.best
    items.push({
      id: 'transfer',
      label: 'Bank the free transfer',
      detail: b
        ? `Best legal move, ${b.out} → ${b.in}, has ${b.inBackers} creator${b.inBackers === 1 ? '' : 's'} backing it. Not worth the slot.`
        : 'No move worth the slot this week.',
      after: `bank £${s.bank.toFixed(1)}m · ${Math.min(s.freeTransfers + 1, 5)} free transfers next week`,
    })
  } else if (s.transfer.best) {
    const b = s.transfer.best
    items.push({
      id: 'transfer',
      label: `Transfer ${b.out} → ${b.in}`,
      detail: `${b.inBackers} creators back ${b.in}; consensus gain +${b.delta.toFixed(2)}.`,
      after: `bank £${(s.bank - b.priceDelta).toFixed(1)}m · ${Math.max(s.freeTransfers - 1, 0)} free transfers left`,
    })
  }

  items.push({
    id: 'captain',
    label: `Captain ${mostBacked[0]?.name ?? '—'}, vice ${mostBacked[1]?.name ?? '—'}`,
    detail:
      mostCaptained && mostCaptained.captainVotes > 0
        ? `Most-backed of your fifteen: ${mostBacked[0].name} (${mostBacked[0].backers}). Most captain votes among yours: ${mostCaptained.name} (${mostCaptained.captainVotes}). The room's captain this week is not in your squad.`
        : `Most-backed of your fifteen: ${mostBacked[0].name} (${mostBacked[0].backers}). No creator captains anyone you own this week.`,
    after: 'armband set in the FPL app',
  })

  if (s.chipsRemaining.includes('Wildcard')) {
    const g = s.wildcardGap
    items.push({
      id: 'wildcard',
      label: 'Decide the wildcard',
      detail: `Your squad sits ${g.gain.toFixed(2)} behind the consensus £${g.solverSquadCost.toFixed(1)}m squad, about ${g.transfersEquivalent.toFixed(0)} transfers' worth. It keeps ${g.keptCount} of your 15. Consensus support, not points; the call is yours.`,
      after: `played: ${g.changesNeeded} changes, squad rebuilt to £${g.solverSquadCost.toFixed(1)}m · held: Wildcard stays available`,
    })
  }

  if (s.transfer.unavailableOwned.length > 0) {
    items.push({
      id: 'unavailable',
      label: `Check the bench: ${s.transfer.unavailableOwned.join(', ')} cannot play`,
      detail: 'Benched already, so it costs autosub cover, not points. Weigh against upgrading a starter.',
      after: 'bench order confirmed',
    })
  }

  return items
}

/** A set of ids persisted per run in this browser only. */
function useStoredSet(key: string): [Set<string>, (id: string, on: boolean) => void] {
  const [set, setSet] = useState<Set<string>>(() => {
    try {
      const raw = window.localStorage.getItem(key)
      return new Set(raw ? (JSON.parse(raw) as string[]) : [])
    } catch {
      return new Set()
    }
  })
  useEffect(() => {
    try {
      window.localStorage.setItem(key, JSON.stringify([...set]))
    } catch {
      /* private mode: the marks simply do not persist */
    }
  }, [key, set])
  return [
    set,
    (id, on) =>
      setSet((prev) => {
        const next = new Set(prev)
        if (on) next.add(id)
        else next.delete(id)
        return next
      }),
  ]
}
