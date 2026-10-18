import { useQueries, useQuery } from '@tanstack/react-query'
import { AlertTriangle, Archive, ArrowRight, CheckCircle2, CircleDashed, Gavel, Loader2, ScanSearch, Scale, ShieldCheck, Workflow } from 'lucide-react'
import { type ReactNode, useMemo, useState } from 'react'
import { Link } from 'react-router'
import { useApp } from '@/app/context'
import { NetworkMotif } from '@/components/Motif'
import { Badge, Button, Empty, Panel, Segmented, Spinner } from '@/components/ui'
import { StartDialog } from '@/features/investigations/StartDialog'
import { api } from '@/lib/api'
import { ago, cn, inr, short } from '@/lib/format'
import { AGENT_INFO, isLive, report, STAGES, stageState, STATUS_INFO, type Tone } from '@/lib/status'
import type { Detail, Investigation, Status } from '@/lib/types'

const ICON: Record<string, ReactNode> = {
  tracer: <ScanSearch className="size-5" />, adjudicator: <Workflow className="size-5" />, verifier: <ShieldCheck className="size-5" />, gate: <Gavel className="size-5" />,
}

function Books({ details }: { details: Detail[] }) {
  const t = useMemo(() => {
    let disputed = 0, held = 0, cashed = 0, unfollowed = 0, liened = 0, cases = 0
    for (const d of details) {
      const r = report(d)
      if (!r.trace) continue
      cases++
      disputed += r.trace.disputed_paise
      held += r.held
      cashed += r.trace.unrecoverable_paise
      unfollowed += r.trace.not_followed_paise
      liened += d.liens.reduce((s, l) => s + (l.amount ?? 0), 0)
    }
    return { disputed, held, cashed, unfollowed, liened, cases, notLiened: Math.max(0, held - liened) }
  }, [details])
  const parts = [
    { key: 'liened', label: 'Liened for the victims', v: t.liened, color: 'var(--color-brand-600)' },
    { key: 'held', label: 'Held, not liened', v: t.notLiened, color: 'var(--color-brand-200)' },
    { key: 'cashed', label: 'Cashed out or left the bank', v: t.cashed, color: '#eb6834' },
    { key: 'unfollowed', label: 'On branches too small to follow', v: t.unfollowed, color: 'var(--color-rule-2)' },
  ]
  const diff = t.disputed - (t.held + t.cashed + t.unfollowed)
  return (
    <Panel title="Where the disputed money is" subtitle={`Across the ${t.cases} traced cases at this bank. Every rupee a victim disputed ends up in exactly one of these.`}
      id="tour-books" raised className="relative isolate overflow-hidden">
      <NetworkMotif className="right-4 top-3 hidden h-[150px] w-[262px] md:block" />
      {t.cases === 0 ? <p className="text-[13px] text-ink-3">No traced case yet.</p> : (
        <>
          <div className="flex items-baseline gap-3">
            <span className="text-[32px] font-semibold tracking-tight text-ink">{inr(t.disputed, { paise: false })}</span>
            <span className="text-[13px] text-ink-3">disputed in all</span>
          </div>
          <div className="mt-4 flex h-4 w-full overflow-hidden rounded-full bg-paper-2" role="img" aria-label="How the disputed money divides">
            {parts.filter(p => p.v > 0).map(p => (
              <div key={p.key} style={{ width: `${(p.v / Math.max(1, t.disputed)) * 100}%`, background: p.color }} className="h-full border-r-2 border-card last:border-r-0" title={`${p.label}: ${inr(p.v)}`} />
            ))}
          </div>
          <dl className="mt-4 grid gap-4 sm:grid-cols-4">
            {parts.map(p => (
              <div key={p.key} className="border-l-2 pl-3" style={{ borderColor: p.color }}>
                <dt className="text-[12.5px] text-ink-3">{p.label}</dt>
                <dd className="num mt-0.5 text-[16px] font-semibold text-ink">{short(p.v)}</dd>
                <dd className="num text-[12px] text-ink-3">{t.disputed ? `${((p.v / t.disputed) * 100).toFixed(1)}%` : '-'}</dd>
              </div>
            ))}
          </dl>
          <p className={cn('mt-4 flex items-center gap-1.5 text-[12.5px]', diff === 0 ? 'text-brand-700' : 'text-rejected')}>
            <Scale className="size-3.5" />{diff === 0 ? 'The books balance to the paisa across every case.' : `The books are off by ${inr(diff)}.`}
            <span className="text-ink-3">A lien holds only the victim's share of an account, never the holder's own money.</span>
          </p>
        </>
      )}
    </Panel>
  )
}

function Agents({ invs }: { invs: Investigation[] }) {
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4" id="tour-agents">
      {STAGES.map(s => {
        const working = invs.filter(i => stageState(i.status, s) === 'working').length
        return (
          <div key={s.key} className="rounded-[14px] border border-rule/70 bg-card p-4 shadow-card">
            <div className="flex items-center gap-2.5">
              <span className="grid size-9 place-items-center rounded-[10px] bg-brand-50 text-brand-700">{ICON[s.key]}</span>
              <div className="min-w-0">
                <div className="text-[14px] font-semibold text-ink">{AGENT_INFO[s.key].name}</div>
                <div className="text-[12px] text-ink-3">{AGENT_INFO[s.key].role}</div>
              </div>
              <span className={cn('ml-auto rounded-full px-2 py-0.5 text-[12px] font-medium', working ? 'bg-brand-100 text-brand-800' : 'bg-paper-2 text-ink-3')}>
                {working ? `${working} ${s.key === 'gate' ? 'waiting' : 'working'}` : 'Idle'}
              </span>
            </div>
            <p className="mt-3 text-[12.5px] leading-relaxed text-ink-2">{AGENT_INFO[s.key].does}</p>
            {s.key === 'gate' && <p className="mt-3 border-t border-rule pt-2.5 text-[12px] text-ink-3">You. Nothing reaches the bank before you or the Verifier's reviewed verdict.</p>}
          </div>
        )
      })}
    </div>
  )
}

type Row = Investigation & { disputed: number; liened: number }

const TINT: Record<Tone, string> = {
  approved: 'bg-approved-tint text-approved', proposed: 'bg-proposed-tint text-proposed', rejected: 'bg-rejected-tint text-rejected',
  review: 'bg-review-tint text-review', victim: 'bg-victim-tint text-victim', none: 'bg-none-tint text-none', danger: 'bg-danger-tint text-danger', brand: 'bg-brand-100 text-brand-800',
}

function StatusIcon({ status }: { status: Status }) {
  const c = 'size-5'
  if (status === 'at_gate') return <Gavel className={c} />
  if (status === 'executed') return <CheckCircle2 className={c} />
  if (status === 'closed') return <Archive className={c} />
  if (status === 'failed' || status === 'checks_failed') return <AlertTriangle className={c} />
  if (isLive(status)) return <Loader2 className={cn(c, 'animate-spin')} />
  return <CircleDashed className={c} />
}

function Field({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn('min-w-0', className)}>
      <div className="text-[11.5px] text-ink-3">{label}</div>
      <div className="num truncate text-[13.5px] font-semibold text-ink">{children}</div>
    </div>
  )
}

/** The latest cases as statement rows: a status tile, then label-over-value columns, and how much was liened. */
function CaseRows({ rows }: { rows: Row[] }) {
  const [sort, setSort] = useState<'newest' | 'largest'>('newest')
  const shown = [...rows].sort((a, b) => (sort === 'newest' ? b.started_at - a.started_at : b.disputed - a.disputed)).slice(0, 6)
  return (
    <Panel title="Recent cases" subtitle="Open a case to see its trail, evidence and gate."
      actions={<Segmented size="sm" value={sort} onChange={setSort} options={[{ value: 'newest', label: 'Newest' }, { value: 'largest', label: 'Largest' }]} />}>
      <ul className="space-y-2">
        {shown.map(r => {
          const info = STATUS_INFO[r.status]
          const share = r.disputed ? Math.min(1, r.liened / r.disputed) : 0
          return (
            <li key={r.id}>
              <Link to={`/investigations/${r.id}`}
                className="grid grid-cols-[44px_minmax(0,1fr)_auto] items-center gap-3 rounded-[12px] border border-rule/70 bg-card px-3 py-2.5 transition-colors hover:border-brand-200 hover:bg-brand-50/40 md:grid-cols-[44px_minmax(0,1.25fr)_minmax(0,0.9fr)_minmax(0,1.2fr)_minmax(0,0.8fr)_auto]">
                <span className={cn('grid size-11 place-items-center rounded-[10px]', TINT[info.tone])} aria-hidden><StatusIcon status={r.status} /></span>
                <div className="min-w-0">
                  <div className="flex items-center gap-2"><span className="text-[14px] font-semibold text-ink">{r.complaint_id}</span><Badge tone={info.tone} pulse={isLive(r.status)} className="text-[11px]">{info.label}</Badge></div>
                  <div className="id truncate text-[11.5px] text-ink-3">{r.id}</div>
                </div>
                <Field label="Disputed" className="hidden md:block">{r.disputed ? short(r.disputed) : '-'}</Field>
                <div className="hidden min-w-0 md:block">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-[11.5px] text-ink-3">Liened for the victim</span>
                    <span className="num text-[11.5px] text-ink-3">{r.disputed ? `${Math.round(share * 100)}%` : ''}</span>
                  </div>
                  <div className="num truncate text-[13.5px] font-semibold text-ink">{r.liened ? short(r.liened) : <span className="font-normal text-ink-4">none yet</span>}</div>
                  <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-paper-2"><div className="h-full rounded-full bg-brand-600" style={{ width: `${share * 100}%` }} /></div>
                </div>
                <Field label="Started" className="hidden md:block"><span className="font-medium text-ink-2">{ago(r.started_at)}</span></Field>
                <span className="inline-flex items-center gap-1 text-[12.5px] font-medium text-brand-700">Open<ArrowRight className="size-3.5" /></span>
              </Link>
            </li>
          )
        })}
      </ul>
      {rows.length > shown.length && <Link to="/investigations" className="mt-3 inline-flex items-center gap-1 text-[13px] font-medium text-brand-700 hover:underline">All {rows.length} cases<ArrowRight className="size-3.5" /></Link>}
    </Panel>
  )
}

const WEEK = [
  { key: 'through', label: 'Reached the gate', color: '#1baf7a' },
  { key: 'other', label: 'Running or stopped early', color: '#2a78d6' },
] as const
const THROUGH: Status[] = ['at_gate', 'executed', 'closed']

/** Investigations started per day over the last seven days, by whether they made it through all three agents. */
function ThisWeek({ invs }: { invs: Investigation[] }) {
  const days = useMemo(() => {
    const today = new Date()
    today.setHours(0, 0, 0, 0)
    return Array.from({ length: 7 }, (_, k) => {
      const start = new Date(today)
      start.setDate(today.getDate() - (6 - k))
      const s = start.getTime() / 1000, e = s + 86400
      const day = invs.filter(x => x.started_at >= s && x.started_at < e)
      const through = day.filter(x => THROUGH.includes(x.status)).length
      return { key: start.toISOString(), wd: start.toLocaleDateString('en-GB', { weekday: 'short' }), date: start.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }),
        through, other: day.length - through, today: k === 6 }
    })
  }, [invs])
  const max = Math.max(1, ...days.map(d => d.through + d.other))
  const total = days.reduce((s, d) => s + d.through + d.other, 0)
  const through = days.reduce((s, d) => s + d.through, 0)
  return (
    <Panel title="This week" subtitle="Investigations started per day at this bank.">
      <div className="flex h-[136px] items-end justify-between gap-1.5 px-1" role="img"
        aria-label={`Last seven days: ${days.map(d => `${d.wd} ${d.through + d.other}`).join(', ')}`}>
        {days.map(d => {
          const n = d.through + d.other
          return (
            <div key={d.key} className="flex flex-1 flex-col items-center gap-1.5" title={`${d.date}: ${d.through} reached the gate, ${d.other} running or stopped early`}>
              <span className="num h-4 text-[11px] font-medium text-ink-2">{n || ''}</span>
              <div className="flex h-[92px] w-3 flex-col-reverse overflow-hidden rounded-full bg-paper-2">
                {d.through > 0 && <div style={{ height: `${(d.through / max) * 100}%`, background: WEEK[0].color }} />}
                {d.other > 0 && <div style={{ height: `${(d.other / max) * 100}%`, background: WEEK[1].color }} className="border-b-2 border-card" />}
              </div>
              <span className={cn('text-[11.5px]', d.today ? 'font-semibold text-ink' : 'text-ink-3')}>{d.wd}</span>
            </div>
          )
        })}
      </div>
      <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-ink-2">
        {WEEK.map(w => <li key={w.key} className="inline-flex items-center gap-1.5"><span className="size-2.5 rounded-[3px]" style={{ background: w.color }} />{w.label}</li>)}
      </ul>
      <p className="mt-2 text-[12.5px] text-ink-3">{total ? `${total} started this week, ${through} reached the gate.` : 'No investigation started in the last seven days.'}</p>
    </Panel>
  )
}

export function DashboardPage() {
  const { bank, bankName } = useApp()
  const [starting, setStarting] = useState(false)
  const invs = useQuery({ queryKey: ['investigations', bank], queryFn: () => api.investigations(bank), enabled: !!bank,
    refetchInterval: q => ((q.state.data as Investigation[] | undefined)?.some(i => isLive(i.status)) ? 4000 : 15000) })
  const all = invs.data ?? []
  const list = all.slice(0, 30)
  const details = useQueries({ queries: list.map(i => ({ queryKey: ['detail', bank, i.id], queryFn: () => api.detail(bank, i.id), staleTime: isLive(i.status) ? 3000 : 60000 })) })
  const reviews = useQuery({ queryKey: ['reviews', bank, 'open'], queryFn: () => api.reviews(bank, 'open'), enabled: !!bank })
  const ds = details.map(d => d.data).filter(Boolean) as Detail[]
  const rows: Row[] = list.map(i => {
    const d = ds.find(x => x.investigation.id === i.id)
    const r = report(d)
    return { ...i, disputed: r.trace?.disputed_paise ?? 0, liened: (d?.liens ?? []).reduce((s, l) => s + (l.amount ?? 0), 0) }
  })
  const atGate = list.filter(i => i.status === 'at_gate')
  if (invs.isLoading) return <Spinner />
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-[22px] font-semibold tracking-tight text-ink">{bankName}</h2>
          <p className="text-[13px] text-ink-3">{list.length} cases, {list.filter(i => isLive(i.status)).length} running, {atGate.length} waiting at the gate, {reviews.data?.length ?? 0} accounts waiting for review</p>
        </div>
        <Button variant="primary" onClick={() => setStarting(true)}>Start an investigation</Button>
      </div>
      {list.length === 0 ? (
        <Empty title="No case at this bank yet" action={<Button variant="primary" onClick={() => setStarting(true)}>Start an investigation</Button>}>Pick a complaint reported to this bank and watch the three agents follow the money.</Empty>
      ) : (
        <>
          <Books details={ds} />
          <Agents invs={list} />
          <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
            <CaseRows rows={rows} />
            <div className="space-y-6">
              <ThisWeek invs={all} />
              <Panel title="Needs you" id="tour-needs-you">
                <ul className="space-y-2">
                  {atGate.map(i => (
                    <li key={i.id}>
                      <Link to={`/investigations/${i.id}`} className="flex items-center justify-between gap-2 rounded-[10px] border border-review/30 bg-review-tint/50 px-3 py-2.5 hover:border-review/60">
                        <span><span className="block text-[13.5px] font-semibold text-ink">{i.complaint_id} is at the gate</span><span className="text-[12px] text-ink-3">Approve or override the Verifier</span></span>
                        <ArrowRight className="size-4 text-review" />
                      </Link>
                    </li>
                  ))}
                  {(reviews.data ?? []).slice(0, 4).map(r => (
                    <li key={r.id}>
                      <Link to="/reviews" className="flex items-center justify-between gap-2 rounded-[10px] border border-rule px-3 py-2.5 hover:bg-paper">
                        <span className="min-w-0"><span className="block text-[13px] font-medium text-ink">Review <span className="id">{r.account_id}</span></span><span className="line-clamp-1 text-[12px] text-ink-3">{r.detail}</span></span>
                        <ArrowRight className="size-4 shrink-0 text-ink-3" />
                      </Link>
                    </li>
                  ))}
                  {atGate.length === 0 && !(reviews.data ?? []).length && <li className="text-[13px] text-ink-3">Nothing is waiting for you.</li>}
                </ul>
              </Panel>
            </div>
          </div>
        </>
      )}
      <StartDialog open={starting} onOpenChange={setStarting} />
    </div>
  )
}
