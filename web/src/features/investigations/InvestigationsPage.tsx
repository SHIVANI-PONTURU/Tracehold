import { Plus, Search } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router'
import { useApp } from '@/app/context'
import { Badge, Button, Empty, ErrorNote, Segmented, Spinner } from '@/components/ui'
import { ago, cn, shortInv } from '@/lib/format'
import { FINAL, isLive, STAGES, stageState, STATUS_INFO } from '@/lib/status'
import type { Investigation } from '@/lib/types'
import { CaseDetail } from './CaseDetail'
import { StartDialog } from './StartDialog'
import { useInvestigations } from './hooks'

type Filter = 'all' | 'live' | 'gate' | 'done'

function Dots({ i }: { i: Investigation }) {
  return (
    <span className="flex gap-1" aria-hidden>
      {STAGES.map(s => {
        const st = stageState(i.status, s)
        return <span key={s.key} className={cn('h-1.5 w-4 rounded-full', st === 'done' ? 'bg-brand-500' : st === 'working' ? 'bg-brand-300 animate-pulse-soft' : st === 'failed' ? 'bg-danger' : 'bg-rule')} />
      })}
    </span>
  )
}

export function InvestigationsPage() {
  const { bank } = useApp()
  const { inv } = useParams()
  const nav = useNavigate()
  const list = useInvestigations(bank)
  const [filter, setFilter] = useState<Filter>('all')
  const [text, setText] = useState('')
  const [starting, setStarting] = useState(false)
  const rows = useMemo(() => (list.data ?? []).filter(i =>
    (filter === 'all' || (filter === 'live' && isLive(i.status)) || (filter === 'gate' && i.status === 'at_gate') || (filter === 'done' && FINAL.includes(i.status)))
    && (!text || `${i.id} ${i.complaint_id}`.toLowerCase().includes(text.toLowerCase()))), [list.data, filter, text])
  useEffect(() => {
    if (!inv && list.data?.length) nav(`/investigations/${list.data[0].id}`, { replace: true })
  }, [inv, list.data, nav])
  const known = !inv || (list.data ?? []).some(i => i.id === inv)
  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-[264px_minmax(0,1fr)]">
      <aside className="xl:sticky xl:top-[88px] xl:self-start">
        <div className="rounded-[14px] border border-rule/70 bg-card shadow-card" id="tour-queue">
          <div className="space-y-3 p-4">
            <Button variant="primary" className="w-full" icon={<Plus className="size-4" />} onClick={() => setStarting(true)} id="tour-start">Start an investigation</Button>
            <label className="relative block">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-ink-4" aria-hidden />
              <input value={text} onChange={e => setText(e.target.value)} placeholder="Find a case" aria-label="Find a case"
                className="h-8 w-full rounded-[var(--radius-control)] border border-rule-2 bg-card pl-8 pr-3 text-[13px] placeholder:text-ink-4 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200" />
            </label>
            <Segmented size="sm" value={filter} onChange={setFilter} className="w-full justify-between"
              options={[{ value: 'all', label: 'All' }, { value: 'live', label: 'Running' }, { value: 'gate', label: 'At gate' }, { value: 'done', label: 'Done' }]} />
          </div>
          <ul className="max-h-[calc(100vh-300px)] overflow-y-auto border-t border-rule scroll-thin" aria-label="Cases">
            {list.isLoading && <li><Spinner /></li>}
            {!list.isLoading && rows.length === 0 && <li className="px-4 py-6 text-center text-[13px] text-ink-3">No case matches.</li>}
            {rows.map(i => (
              <li key={i.id}>
                <button onClick={() => nav(`/investigations/${i.id}`)}
                  className={cn('block w-full border-b border-rule/70 px-4 py-3 text-left transition-colors last:border-b-0',
                    i.id === inv ? 'bg-brand-50 shadow-[inset_3px_0_0_var(--color-brand-600)]' : 'hover:bg-paper/70')}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[14px] font-semibold text-ink">{i.complaint_id}</span>
                    <Badge tone={STATUS_INFO[i.status].tone} pulse={isLive(i.status)}>{STATUS_INFO[i.status].label}</Badge>
                  </div>
                  <div className="mt-1 flex items-center justify-between gap-2">
                    <span className="id text-[11.5px] text-ink-3">{shortInv(i.id)}</span>
                    <span className="text-[12px] text-ink-3">{ago(i.started_at)}</span>
                  </div>
                  <div className="mt-2"><Dots i={i} /></div>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </aside>
      <section className="min-w-0">
        {list.error && <ErrorNote error={list.error} />}
        {!list.isLoading && !list.data?.length ? (
          <Empty title="No investigations at this bank yet" action={<Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setStarting(true)}>Start an investigation</Button>}>
            Pick a complaint reported to this bank; the Tracer, Adjudicator and Verifier take it from there.
          </Empty>
        ) : inv && known ? <CaseDetail key={inv} inv={inv} /> : inv && !list.isLoading ? (
          <Empty title="This case is not at the selected bank">Pick it from the list, or switch to the bank that handles it.</Empty>
        ) : <Spinner />}
      </section>
      <StartDialog open={starting} onOpenChange={setStarting} />
    </div>
  )
}
