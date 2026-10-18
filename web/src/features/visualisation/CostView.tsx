import { useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useApp } from '@/app/context'
import { DataTable } from '@/components/DataTable'
import { ErrorNote, Ledger, Mono, Panel, Select, Spinner } from '@/components/ui'
import { api } from '@/lib/api'
import { num, usd, when } from '@/lib/format'
import { AGENT_HEX, AGENT_INFO } from '@/lib/status'
import type { Call } from '@/lib/types'
import { AXIS, ChartTip, LegendRow } from './charts'

const AGENTS = ['tracer', 'adjudicator', 'verifier', 'chat'] as const
type Run = { id: string; complaint: string; first: number; last: number; calls: number; usd: number; tokens: number } & Record<(typeof AGENTS)[number], number>

function median(v: number[]) {
  if (!v.length) return 0
  const s = [...v].sort((a, b) => a - b)
  const m = Math.floor(s.length / 2)
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
}

export function CostView() {
  const { config } = useApp()
  const calls = useQuery({ queryKey: ['calls'], queryFn: () => api.calls(), refetchInterval: 20000 })
  const data = calls.data ?? []
  const runs = useMemo(() => {
    const m = new Map<string, Run>()
    for (const c of data) {
      if (!c.investigation_id?.startsWith('INV-')) continue
      const r = m.get(c.investigation_id) ?? { id: c.investigation_id, complaint: c.investigation_id.replace(/^INV-(\d+)-.*/, 'CMP-$1'), first: c.ts, last: c.ts,
        calls: 0, usd: 0, tokens: 0, tracer: 0, adjudicator: 0, verifier: 0, chat: 0 }
      r.calls++; r.usd += c.usd; r.tokens += c.input_tokens + c.output_tokens
      r.first = Math.min(r.first, c.ts); r.last = Math.max(r.last, c.ts)
      if ((AGENTS as readonly string[]).includes(c.agent)) r[c.agent as (typeof AGENTS)[number]] += c.usd
      m.set(c.investigation_id, r)
    }
    return [...m.values()].sort((a, b) => a.first - b.first)
  }, [data])
  const recent = runs.slice(-36)
  const [sel, setSel] = useState<string>('')
  const run = runs.find(r => r.id === sel) ?? runs[runs.length - 1]
  const runCalls = useMemo(() => data.filter(c => c.investigation_id === run?.id).sort((a, b) => a.id - b.id).map((c, i) => ({ ...c, n: i + 1 })), [data, run])
  const byAgent = useMemo(() => AGENTS.map(a => {
    const cs = data.filter(c => c.agent === a)
    return { agent: a, usd: cs.reduce((s, c) => s + c.usd, 0), calls: cs.length, tokens: cs.reduce((s, c) => s + c.input_tokens + c.output_tokens, 0),
      cached: cs.reduce((s, c) => s + c.cached_input_tokens, 0), input: cs.reduce((s, c) => s + c.input_tokens, 0) }
  }).filter(x => x.calls), [data])
  const byModel = useMemo(() => {
    const m = new Map<string, { model: string; calls: number; usd: number }>()
    for (const c of data) { const x = m.get(c.model) ?? { model: c.model, calls: 0, usd: 0 }; x.calls++; x.usd += c.usd; m.set(c.model, x) }
    return [...m.values()].sort((a, b) => b.usd - a.usd)
  }, [data])
  const cumulative = useMemo(() => {
    let s = 0
    return [...data].sort((a, b) => a.id - b.id).map(c => ({ ts: c.ts, usd: (s += c.usd) })).filter((_, i, arr) => i % Math.max(1, Math.floor(arr.length / 300)) === 0 || i === arr.length - 1)
  }, [data])
  const total = data.reduce((s, c) => s + c.usd, 0)
  const cap = config?.budget.total_usd ?? 70
  const perRun = config?.budget.per_run_usd ?? 3
  // Spend over time: round dollar ticks under the cap line (which carries its own label), and one tick per local
  // midnight, so the labels never crowd where the runs cluster.
  const yMax = Math.ceil(Math.max(cap, total * 1.1))
  const yStep = yMax > 150 ? 50 : yMax > 40 ? 20 : 10
  const yTicks = Array.from({ length: Math.floor(yMax / yStep) + 1 }, (_, i) => i * yStep)
  const dayTicks = useMemo(() => {
    const out: number[] = []
    if (cumulative.length < 2) return out
    const end = cumulative[cumulative.length - 1].ts
    const d = new Date(cumulative[0].ts * 1000)
    d.setHours(24, 0, 0, 0)                      // the first local midnight after the first call
    while (d.getTime() / 1000 <= end) { out.push(d.getTime() / 1000); d.setDate(d.getDate() + 1) }
    return out
  }, [cumulative])
  const callCols = useMemo<ColumnDef<Call & { n: number }, any>[]>(() => [
    { header: '#', accessorKey: 'n', meta: { align: 'right', width: '44px' } },
    { header: 'Time', accessorKey: 'ts', cell: c => <span className="text-ink-3">{new Date(c.getValue() * 1000).toLocaleTimeString('en-GB')}</span> },
    { header: 'Agent', accessorKey: 'agent', cell: c => <span className="inline-flex items-center gap-1.5"><span className="size-2 rounded-full" style={{ background: AGENT_HEX[c.getValue()] }} />{AGENT_INFO[c.getValue()]?.name ?? c.getValue()}</span> },
    { header: 'Model route', accessorKey: 'model', cell: c => <Mono className="text-[11.5px] text-ink-2">{c.getValue()}</Mono> },
    { header: 'Input', accessorKey: 'input_tokens', meta: { align: 'right' }, cell: c => num(c.getValue()) },
    { header: 'Cached', accessorKey: 'cached_input_tokens', meta: { align: 'right' }, cell: c => num(c.getValue()) },
    { header: 'Output', accessorKey: 'output_tokens', meta: { align: 'right' }, cell: c => num(c.getValue()) },
    { header: 'Thinking', accessorKey: 'reasoning_tokens', meta: { align: 'right' }, cell: c => num(c.getValue()) },
    { header: 'Cost', accessorKey: 'usd', meta: { align: 'right' }, cell: c => <span className="font-medium text-ink">{usd(c.getValue(), 4)}</span> },
  ], [])
  if (calls.isLoading) return <Spinner />
  if (calls.error) return <ErrorNote error={calls.error} />
  return (
    <div className="space-y-6">
      <Ledger items={[
        { label: 'Model spend so far', value: usd(total), note: `of the ${usd(cap, 0)} cap`, tone: 'brand' },
        { label: 'Model calls', value: num(data.length), note: `${num(runs.length)} complaint runs` },
        { label: 'Median cost per run', value: usd(median(runs.map(r => r.usd)), 3), note: `cap ${usd(perRun, 0)} per run` },
        { label: 'Most expensive run', value: usd(Math.max(0, ...runs.map(r => r.usd)), 3), note: runs.length ? [...runs].sort((a, b) => b.usd - a.usd)[0].complaint : '' },
        { label: 'Tokens in and out', value: `${(data.reduce((s, c) => s + c.input_tokens + c.output_tokens, 0) / 1e6).toFixed(2)} M` },
      ]} />
      <Panel title="What each complaint run cost, by agent" subtitle="The latest 36 runs, by complaint number. Click a bar to see that run's model calls." id="tour-cost-runs"
        actions={<LegendRow items={AGENTS.map(a => ({ key: a, label: AGENT_INFO[a].name, color: AGENT_HEX[a] }))} />}>
        <div className="h-[280px]">
          <ResponsiveContainer>
            <BarChart data={recent} margin={{ top: 8, right: 8, bottom: 4, left: 0 }} onClick={(s: unknown) => { const p = (s as { activePayload?: { payload?: Run }[] })?.activePayload?.[0]?.payload; if (p) setSel(p.id) }}>
              <CartesianGrid stroke="var(--color-rule)" vertical={false} />
              <XAxis dataKey="complaint" {...AXIS} interval="preserveStartEnd" minTickGap={14} tickMargin={6} tickFormatter={v => String(v).replace(/^CMP-/, '')} />
              <YAxis {...AXIS} tickFormatter={v => `$${v}`} width={44} />
              <ReferenceLine y={perRun} stroke="var(--color-danger)" strokeOpacity={0.55} label={{ value: `per-run cap $${perRun}`, fill: 'var(--color-ink-3)', fontSize: 11, position: 'insideTopRight' }} ifOverflow="hidden" />
              <Tooltip cursor={{ fill: 'var(--color-brand-50)' }} content={<ChartTip format={v => usd(Number(v), 3)} labelFormat={(l, p) => `${l}, ${(p?.[0]?.payload?.id as string) ?? ''}`}
                footer={p => `Total ${usd(Number(p[0]?.payload?.usd ?? 0), 3)} for ${p[0]?.payload?.calls} calls`} />} />
              {AGENTS.map((a, i) => (
                <Bar key={a} dataKey={a} name={AGENT_INFO[a].name} stackId="run" fill={AGENT_HEX[a]} stroke="#fff" strokeWidth={1}
                  radius={i === AGENTS.length - 1 ? [4, 4, 0, 0] : 0} cursor="pointer" isAnimationActive={false} />
              ))}
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Panel>
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_380px]">
        <Panel title="Every model call in one run" subtitle={run ? `${run.complaint}: ${run.calls} calls, ${usd(run.usd, 3)}, ${when(run.first)}` : undefined}
          actions={<Select className="h-8 w-72 text-[13px]" value={run?.id ?? ''} onChange={e => setSel(e.target.value)} aria-label="Run">
            {[...runs].reverse().map(r => <option key={r.id} value={r.id}>{r.complaint}, {r.id}, {usd(r.usd, 3)}</option>)}
          </Select>}>
          <div className="h-[220px]">
            <ResponsiveContainer>
              <BarChart data={runCalls} margin={{ top: 8, right: 8, bottom: 4, left: 0 }}>
                <CartesianGrid stroke="var(--color-rule)" vertical={false} />
                <XAxis dataKey="n" {...AXIS} tickMargin={6} />
                <YAxis {...AXIS} tickFormatter={v => `$${Number(v).toFixed(2)}`} width={48} />
                <Tooltip cursor={{ fill: 'var(--color-brand-50)' }} content={<ChartTip format={v => usd(Number(v), 4)} labelFormat={(l, p) => `Call ${l}: ${AGENT_INFO[(p?.[0]?.payload?.agent as string) ?? '']?.name ?? ''}`}
                  footer={p => { const c = p[0]?.payload as unknown as Call; return c ? `${num(c.input_tokens)} in (${num(c.cached_input_tokens)} cached), ${num(c.output_tokens)} out` : null }} />} />
                <Bar dataKey="usd" name="Cost" radius={[4, 4, 0, 0]} isAnimationActive={false}>
                  {runCalls.map(c => <Cell key={c.id} fill={AGENT_HEX[c.agent] ?? '#8aa293'} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="mt-4"><DataTable data={runCalls} columns={callCols} rowId={c => String(c.id)} pageSize={8} dense /></div>
        </Panel>
        <div className="space-y-6">
          <Panel title="By agent, every run" subtitle="Where the money goes across the whole project.">
            <ul className="space-y-3">
              {byAgent.map(a => (
                <li key={a.agent}>
                  <div className="flex items-baseline justify-between text-[13px]">
                    <span className="inline-flex items-center gap-1.5 font-medium text-ink"><span className="size-2.5 rounded-[3px]" style={{ background: AGENT_HEX[a.agent] }} />{AGENT_INFO[a.agent].name}</span>
                    <span className="num font-semibold text-ink">{usd(a.usd)}</span>
                  </div>
                  <div className="mt-1 h-2 overflow-hidden rounded-full bg-paper-2"><div className="h-full rounded-full" style={{ width: `${(a.usd / Math.max(...byAgent.map(x => x.usd))) * 100}%`, background: AGENT_HEX[a.agent] }} /></div>
                  <p className="num mt-1 text-[12px] text-ink-3">{num(a.calls)} calls, {usd(a.usd / a.calls, 4)} each, {a.input ? Math.round((a.cached / a.input) * 100) : 0}% of input from the prompt cache</p>
                </li>
              ))}
            </ul>
          </Panel>
          <Panel title="By model route">
            <ul className="space-y-1.5 text-[13px]">
              {byModel.map(m => (
                <li key={m.model} className="flex items-center justify-between gap-3"><Mono className="truncate text-[12px] text-ink-2">{m.model}</Mono><span className="num shrink-0 text-ink">{num(m.calls)} calls, <b className="font-semibold">{usd(m.usd)}</b></span></li>
              ))}
            </ul>
            <p className="mt-3 text-[12px] leading-snug text-ink-3">The same Claude Opus 4.6 serves the US and the global Bedrock route; the global one has its own daily token quota. The other entries are one-off checks.</p>
          </Panel>
        </div>
      </div>
      <Panel title="Spend over time" subtitle="Cumulative model spend in US dollars, against the project's cap.">
        <div className="h-[220px]">
          <ResponsiveContainer>
            <AreaChart data={cumulative} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="var(--color-rule)" vertical={false} />
              <XAxis dataKey="ts" type="number" domain={['dataMin', 'dataMax']} scale="time" ticks={dayTicks.length ? dayTicks : undefined}
                tickFormatter={v => dayTicks.length ? new Date(v * 1000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) : when(v)} {...AXIS} tickMargin={6} />
              <YAxis {...AXIS} domain={[0, yMax]} ticks={yTicks} tickFormatter={v => `$${v}`} width={44} />
              <ReferenceLine y={cap} stroke="var(--color-danger)" strokeOpacity={0.55} label={{ value: `cap ${usd(cap, 0)}`, fill: 'var(--color-ink-3)', fontSize: 11, position: 'insideTopLeft' }} />
              <Tooltip content={<ChartTip format={v => usd(Number(v))} labelFormat={v => when(Number(v))} />} />
              <Area dataKey="usd" name="Spend" type="monotone" stroke="var(--color-brand-600)" strokeWidth={2} fill="var(--color-brand-100)" fillOpacity={0.7} dot={false} activeDot={{ r: 4, stroke: '#fff', strokeWidth: 2 }} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
        <p className="mt-2 text-[12px] text-ink-3">Every call is priced from its token usage and booked in the tool server's spend ledger; the agents stop before a run passes {usd(perRun, 0)} or the project passes {usd(cap, 0)}. <Link to="/visualise/graph" className="text-brand-700 hover:underline">See the calls on the graph replay</Link>.</p>
      </Panel>
    </div>
  )
}
