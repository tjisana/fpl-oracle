import { useState } from 'react'
import { Countdown } from '@/components/coupon/Countdown'
import { MySquad } from '@/tabs/MySquad'
import fixture from '@/fixtures/gw4-planning.json'
import { deadlineLabel, stampTime } from '@/lib/format'
import type { PlanningState } from '@/data/planning'

const TABS = ['My squad', 'Report', 'Creator ownership', 'Evidence trail'] as const
type Tab = (typeof TABS)[number]

// Until the API exists the page reads one real run record, frozen as a fixture.
const planning = fixture as PlanningState

function App() {
  const [tab, setTab] = useState<Tab>('My squad')

  return (
    <div className="min-h-dvh flex flex-col">
      <header className="strip">
        <div className="mx-auto max-w-6xl px-4 py-3 flex items-center gap-4 flex-wrap">
          <h1 className="print text-[length:var(--t-lg)] font-bold tracking-wide uppercase m-0">
            fpl-oracle
          </h1>
          <p className="print m-0 text-[length:var(--t-sm)] uppercase tracking-wider opacity-90">
            Gameweek {planning.gameweek} coupon · closes {deadlineLabel(planning.deadline)}
          </p>
          <div className="ms-auto">
            <Countdown to={planning.deadline} />
          </div>
        </div>
      </header>

      <nav aria-label="Sections" className="mx-auto w-full max-w-6xl px-4">
        <div role="tablist" className="tabbar">
          {TABS.map((t) => (
            <button
              key={t}
              role="tab"
              id={`tab-${slug(t)}`}
              aria-selected={tab === t}
              aria-controls={`panel-${slug(t)}`}
              className="tab"
              onClick={() => setTab(t)}
            >
              {t}
            </button>
          ))}
        </div>
      </nav>

      <main className="mx-auto w-full max-w-6xl px-4 pb-16 flex-1">
        {TABS.map((t) => (
          <section
            key={t}
            role="tabpanel"
            id={`panel-${slug(t)}`}
            aria-labelledby={`tab-${slug(t)}`}
            hidden={tab !== t}
          >
            {t === 'My squad' ? <MySquad state={planning} /> : <Unprinted section={t} />}
          </section>
        ))}
      </main>

      <footer className="rule-t">
        <div className="mx-auto max-w-6xl px-4 py-4 pinned">
          Prices as of {stampTime(planning.pricesAsOf)} · {planning.contributingCreators} creators
          contributed · entry no. printed under the stub. This site never writes to FPL.
        </div>
      </footer>
    </div>
  )
}

function Unprinted({ section }: { section: Tab }) {
  return (
    <div className="unprinted mt-8">
      <p className="head m-0 mb-2">Not on this week's coupon yet</p>
      <p className="m-0">
        The {section} section is next to be printed. It reads the same run record as My squad
        and inherits this coupon's vocabulary: printed heads, hairline rules, one spot colour,
        biro for anything you mark.
      </p>
    </div>
  )
}

function slug(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, '-')
}

export default App
