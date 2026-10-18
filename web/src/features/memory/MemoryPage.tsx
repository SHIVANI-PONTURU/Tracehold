import { useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { useMemo } from 'react'
import { Link } from 'react-router'
import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useApp } from '@/app/context'
import { DataTable } from '@/components/DataTable'
import { Badge, Empty, ErrorNote, Ledger, Meter, Mono, Panel, Spinner } from '@/components/ui'
import { api } from '@/lib/api'
import { inr, when } from '@/lib/format'
import { LABEL_INFO, STATUS_INFO } from '@/lib/status'
import type { Decision, Lesson, Memory } from '@/lib/types'
import { ChartTip, SERIES } from '@/features/visualisation/charts'

const EVENT: Record<string, string> = { created: 'Created', confirmed: 'Confirmed', contradicted: 'Contradicted', deactivated: 'Deactivated', superseded: 'Superseded' }

export function MemoryPage() {
  const { bank, bankName } = useApp()
  const m = useQuery({ queryKey: ['memory', bank], queryFn: () => api.memory(bank), enabled: !!bank, refetchInterval: 20000 })
  if (m.isLoading) return <Spinner />
  if (m.error || !m.data) return <ErrorNote error={m.error} />
  return <MemoryBody data={m.data} bank={bank} bankName={bankName} />
}

function MemoryBody({ data, bank, bankName }: { data: Memory; bank: string; bankName: string }) {
  const c = data.counts
  const lessonCols = useMemo<ColumnDef<Lesson, any>[]>(() => [
    { header: 'Lesson', accessorKey: 'id', cell: x => <Mono className="text-[11.5px]">{x.getValue()}</Mono> },
    { header: 'Advice', accessorKey: 'stance', cell: x => <Badge tone={x.getValue() === 'release' ? 'approved' : 'rejected'} dot={false}>{x.getValue() === 'release' ? 'Do not lien' : 'Lien'}</Badge> },
    { header: 'For accounts labelled', id: 'label', accessorFn: l => l.label ?? '', cell: x => x.row.original.label ? LABEL_INFO[x.row.original.label]?.label : 'Any' },
    { header: 'Confidence', accessorKey: 'confidence', meta: { align: 'right', width: '150px' }, cell: x => (
      <div className="flex items-center justify-end gap-2"><Meter value={x.getValue()} className="w-16" label="Confidence" /><span className="num w-9 font-medium text-ink">{Number(x.getValue()).toFixed(2)}</span></div>) },
    { header: 'Confirmed', accessorKey: 'times_confirmed', meta: { align: 'right' } },
    { header: 'Contradicted', accessorKey: 'times_contradicted', meta: { align: 'right' } },
    { header: 'Active', accessorKey: 'active', cell: x => x.getValue() ? <Badge tone="approved">Active</Badge> : <Badge tone="none">{x.row.original.superseded_by ? 'Superseded' : 'Inactive'}</Badge> },
    { header: 'Taught by', accessorKey: 'complaint_id', cell: x => <span>{x.getValue()} <span className="text-ink-3">({x.row.original.source === 'human' ? 'a person' : 'the Verifier'})</span></span> },
    { header: 'What it says', accessorKey: 'text', meta: { wrap: true }, cell: x => <span className="leading-snug">{x.getValue()}</span> },
  ], [])
  const decisionCols = useMemo<ColumnDef<Decision, any>[]>(() => [
    { header: 'Account', accessorKey: 'account_id', cell: x => <Mono>{x.getValue()}</Mono> },
    { header: 'Case', accessorKey: 'complaint_id', cell: x => <Link className="font-medium text-brand-700 hover:underline" to={`/investigations/${x.row.original.investigation_id}`}>{x.getValue()}</Link> },
    { header: 'Label', accessorKey: 'label', cell: x => <Badge tone={LABEL_INFO[x.getValue() as keyof typeof LABEL_INFO]?.tone}>{LABEL_INFO[x.getValue() as keyof typeof LABEL_INFO]?.label}</Badge> },
    { header: 'Lien', accessorKey: 'lien_amount', meta: { align: 'right' }, cell: x => inr(x.getValue()) },
    { header: 'Gate', accessorKey: 'approved', cell: x => <Badge tone={x.getValue() ? 'approved' : 'rejected'}>{x.getValue() ? 'Approved' : 'Rejected'}</Badge> },
    { header: 'Decided by', accessorKey: 'decided_by', cell: x => x.getValue() === 'human' ? 'A person' : 'The Verifier' },
    { header: 'Reason', accessorKey: 'gate_reason', meta: { wrap: true }, cell: x => <span className="line-clamp-2 leading-snug">{x.getValue()}</span> },
  ], [])
  const lessons = data.lessons
  const shown = lessons.slice(0, SERIES.length)
  const points = useMemo(() => {
    const rows: Record<string, number | string>[] = []
    for (const l of shown) for (const h of l.history ?? []) if (h.confidence != null) rows.push({ ts: h.ts, [l.id]: h.confidence, event: h.event })
    rows.sort((a, b) => Number(a.ts) - Number(b.ts))
    return rows
  }, [shown])
  const decided = data.decisions.filter(d => d.approved !== null && d.approved !== undefined)
  return (
    <div className="space-y-6">
      <Ledger items={[
        { label: 'Cases remembered', value: c.Investigation ?? 0 },
        { label: 'Lien decisions', value: c.Decision ?? 0 },
        { label: 'Lessons', value: c.Lesson ?? 0, tone: 'brand', note: `${lessons.filter(l => l.active).length} active` },
        { label: 'Automatic actions', value: c.Action ?? 0 },
        { label: 'Agent steps', value: c.Step ?? 0 },
        { label: 'Accounts and devices', value: c.EntityRef ?? 0 },
      ]} />
      <p className="-mt-3 text-[12.5px] text-ink-3">{bankName}'s own memory graph in FalkorDB (<span className="id">memory_{bank}</span>). No other bank can read it.</p>
      <Panel title="Lessons" subtitle="A lesson is made at the gate when a lien is rejected with a reason, or when a person overturns the Verifier. Later decisions on the same accounts move its confidence: up 0.1 when they agree, down 0.2 when they do not; below 0.3 it stops being used.">
        {lessons.length === 0 ? <Empty title="No lessons yet">Reject a lien at the gate with a reason, and this bank remembers it.</Empty>
          : <DataTable data={lessons} columns={lessonCols} rowId={l => l.id} initialSort={[{ id: 'confidence', desc: true }]} pageSize={8} />}
      </Panel>
      {points.length > 0 && (
        <Panel title="How lesson confidence changed" subtitle={lessons.length > SERIES.length ? `The ${SERIES.length} most trusted lessons; the table lists all ${lessons.length}.` : 'Each point is an event: created, confirmed or contradicted by a later decision or resolved review.'}>
          <div className="h-[260px]">
            <ResponsiveContainer>
              <LineChart data={points} margin={{ top: 8, right: 24, bottom: 4, left: 0 }}>
                <CartesianGrid stroke="var(--color-rule)" vertical={false} />
                <XAxis dataKey="ts" type="number" domain={['dataMin', 'dataMax']} scale="time" tickFormatter={v => when(v)} tick={{ fill: 'var(--color-ink-3)', fontSize: 12 }} stroke="var(--color-rule-2)" />
                <YAxis domain={[0, 1]} ticks={[0, 0.3, 0.5, 0.7, 1]} tick={{ fill: 'var(--color-ink-3)', fontSize: 12 }} stroke="var(--color-rule-2)" width={36} />
                <ReferenceLine y={0.3} stroke="var(--color-ink-4)" strokeOpacity={0.7} label={{ value: 'stops being used below 0.3', fill: 'var(--color-ink-3)', fontSize: 11, position: 'insideBottomRight' }} />
                <Tooltip content={<ChartTip format={v => Number(v).toFixed(2)} labelFormat={v => when(Number(v))} />} />
                <Legend iconType="plainline" wrapperStyle={{ fontSize: 12, color: 'var(--color-ink-2)' }} />
                {shown.map((l, i) => (
                  <Line key={l.id} dataKey={l.id} name={`${l.complaint_id}: ${l.stance === 'release' ? 'do not lien' : 'lien'} ${l.account_id}`} stroke={SERIES[i]}
                    strokeWidth={2} connectNulls dot={{ r: 4, strokeWidth: 2, stroke: '#fff', fill: SERIES[i] }} activeDot={{ r: 5 }} isAnimationActive={false} />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
          <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[12px] text-ink-3">
            {shown.flatMap(l => (l.history ?? []).map((h, i) => <li key={`${l.id}-${i}`}><span className="id text-ink-2">{l.complaint_id}</span> {EVENT[h.event] ?? h.event} {when(h.ts)}{h.confidence != null ? `, ${h.confidence.toFixed(2)}` : ''}</li>))}
          </ul>
        </Panel>
      )}
      <Panel title="Decisions at the gate" subtitle="Every lien decision this bank's gate recorded, newest first.">
        <DataTable data={decided} columns={decisionCols} rowId={d => d.id} pageSize={8} search="Search decisions" />
      </Panel>
      <Panel title="Cases in memory">
        <div className="flex flex-wrap gap-2">
          {data.investigations.map(i => (
            <Link key={i.id} to={`/investigations/${i.id}`} className="inline-flex items-center gap-2 rounded-full border border-rule bg-card px-3 py-1.5 text-[12.5px] shadow-card hover:border-brand-300 hover:bg-brand-50">
              <span className="font-medium text-ink">{i.complaint_id}</span><Badge tone={STATUS_INFO[i.status]?.tone ?? 'none'} className="text-[11px]">{STATUS_INFO[i.status]?.label ?? i.status}</Badge>
            </Link>
          ))}
        </div>
      </Panel>
    </div>
  )
}
