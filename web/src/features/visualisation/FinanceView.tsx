import { useQueries, useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useApp } from '@/app/context'
import { DataTable } from '@/components/DataTable'
import { Badge, Empty, ErrorNote, Ledger, Panel, Segmented, Select, Spinner } from '@/components/ui'
import { api } from '@/lib/api'
import { cn, inr, lakh, pct, short } from '@/lib/format'
import { report } from '@/lib/status'
import type { Detail, EvalCase, Evaluation } from '@/lib/types'
import { AXIS, ChartTip, DotPlot, GROUP_NAME, LegendRow, METHOD_COLOR, METHOD_NAME, METHODS } from './charts'

const FLOW = [
  { key: 'liened', label: 'Liened for the victim', color: '#1f7d48' },
  { key: 'held', label: 'Held, not liened', color: '#a9d9b8' },
  { key: 'cashed', label: 'Cashed out or left the bank', color: '#eb6834' },
  { key: 'unfollowed', label: 'Not followed (too small)', color: '#c3d3c8' },
] as const

function MoneyFlow() {
  const { bank, bankName } = useApp()
  const invs = useQuery({ queryKey: ['investigations', bank], queryFn: () => api.investigations(bank), enabled: !!bank })
  const list = (invs.data ?? []).slice(0, 24)
  const ds = useQueries({ queries: list.map(i => ({ queryKey: ['detail', bank, i.id], queryFn: () => api.detail(bank, i.id), staleTime: 60000 })) })
    .map(q => q.data).filter(Boolean) as Detail[]
  const rows = ds.map(d => {
    const r = report(d)
    if (!r.trace) return null
    const liened = d.liens.reduce((s, l) => s + (l.amount ?? 0), 0)
    return { case: `${d.investigation.complaint_id} ${d.investigation.id.slice(9, 13)}`, id: d.investigation.id, disputed: r.trace.disputed_paise,
      liened: lakh(liened), held: lakh(Math.max(0, r.held - liened)), cashed: lakh(r.trace.unrecoverable_paise), unfollowed: lakh(r.trace.not_followed_paise) }
  }).filter(Boolean) as { case: string; id: string; disputed: number; liened: number; held: number; cashed: number; unfollowed: number }[]
  return (
    <Panel title={`Where the stolen money went, case by case at ${bankName}`} subtitle="Each bar is one complaint's disputed money, split the way the Tracer's books split it. Rupees in lakh (1 lakh = 1,00,000)."
      actions={<LegendRow items={FLOW.map(f => ({ key: f.key, label: f.label, color: f.color }))} />} id="tour-finance-flow">
      {rows.length === 0 ? <p className="text-[13px] text-ink-3">No traced case at this bank yet.</p> : (
        <div style={{ height: Math.max(180, rows.length * 34 + 40) }}>
          <ResponsiveContainer>
            <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 8 }} barCategoryGap={6}>
              <CartesianGrid stroke="var(--color-rule)" horizontal={false} />
              <XAxis type="number" {...AXIS} tickFormatter={v => `${v} L`} />
              <YAxis type="category" dataKey="case" {...AXIS} width={110} />
              <Tooltip cursor={{ fill: 'var(--color-brand-50)' }} content={<ChartTip format={v => `₹${Number(v).toFixed(2)} L`} footer={p => `Disputed ${inr(Number(p[0]?.payload?.disputed ?? 0))}`} />} />
              {FLOW.map((f, i) => <Bar key={f.key} dataKey={f.key} name={f.label} stackId="m" fill={f.color} stroke="#fff" strokeWidth={1} radius={i === FLOW.length - 1 ? [0, 4, 4, 0] : 0} isAnimationActive={false} />)}
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </Panel>
  )
}

type Agg = { method: string; group: string; complaints: number; innocent: number; innocent_accounts: number; beyond_taint: number; stolen_held: number;
  liens: number; liened_mules: number; reviews: number; trail_mules: number; caught: number; mules_liened: number;
  precision: number | null; recall: number | null; lien_recall: number | null; reviews_per_case: number }

function aggregate(cases: EvalCase[], scope: 'sample' | 'all_test', exclude: Set<string>): Agg[] {
  const keep = cases.filter(c => (scope === 'all_test' || c.in_sample) && !exclude.has(c.complaint_id))
  const out: Agg[] = []
  for (const m of METHODS) for (const g of ['shallow', 'medium', 'deep', 'held_out', 'all_buckets']) {
    const sel = keep.filter(c => c.method === m && (c.group === g || (g === 'all_buckets' && c.group !== 'held_out')))
    if (!sel.length) continue
    const s = (k: keyof EvalCase) => sel.reduce((t, c) => t + (Number(c[k]) || 0), 0)
    const liens = s('liens'), tm = s('trail_mules')
    out.push({ method: m, group: g, complaints: sel.length, innocent: s('innocent'), innocent_accounts: s('innocent_accounts'), beyond_taint: s('beyond_taint'),
      stolen_held: s('stolen_held'), liens, liened_mules: s('liened_mules'), reviews: s('reviews'), trail_mules: tm, caught: s('caught'), mules_liened: s('mules_liened'),
      precision: liens ? s('liened_mules') / liens : null, recall: tm ? s('caught') / tm : null, lien_recall: tm ? s('mules_liened') / tm : null,
      reviews_per_case: s('reviews') / sel.length })
  }
  return out
}

function Methods() {
  const files = useQuery({ queryKey: ['evaluations'], queryFn: api.evaluations, staleTime: 60000 })
  const [file, setFile] = useState<string>('')
  const chosen = file || files.data?.[0]?.file || ''
  const res = useQuery({ queryKey: ['evaluation', chosen], queryFn: () => api.evaluation(chosen), enabled: !!chosen, staleTime: Infinity })
  const [scope, setScope] = useState<'sample' | 'all_test'>('sample')
  const [exclude, setExclude] = useState<Set<string>>(new Set())
  if (files.isLoading || res.isLoading) return <Spinner label="Reading the evaluation" />
  if (!files.data?.length) return <Empty title="No evaluation results yet">Run the Phase 4 evaluation on the server; its results file appears here.</Empty>
  if (res.error || !res.data) return <ErrorNote error={res.error} />
  return <MethodsBody res={res.data} files={files.data} chosen={chosen} setFile={f => { setFile(f); setExclude(new Set()) }} scope={scope} setScope={setScope} exclude={exclude} setExclude={setExclude} />
}

function MethodsBody({ res, files, chosen, setFile, scope, setScope, exclude, setExclude }: {
  res: Evaluation; files: { file: string; sample: string; rubric: string; written: string; complaints: number | null }[]; chosen: string; setFile: (f: string) => void
  scope: 'sample' | 'all_test'; setScope: (s: 'sample' | 'all_test') => void; exclude: Set<string>; setExclude: (s: Set<string>) => void
}) {
  const rows = useMemo(() => aggregate(res.cases, scope, exclude), [res, scope, exclude])
  const pick = (m: string, g = 'all_buckets') => rows.find(r => r.method === m && r.group === g)
  const th = pick('tracehold'), ff = pick('full_freeze'), gr = pick('greedy'), ro = pick('rules_only')
  const less = (a?: Agg, b?: Agg) => (a && b && b.innocent ? 1 - a.innocent / b.innocent : null)
  const heavy = useMemo(() => {
    const t = res.cases.filter(c => c.method === 'tracehold' && c.in_sample && c.innocent > 0).sort((a, b) => b.innocent - a.innocent)
    const total = t.reduce((s, c) => s + c.innocent, 0)
    return t.slice(0, 4).map(c => ({ ...c, share: total ? c.innocent / total : 0 }))
  }, [res])
  const groups = ['shallow', 'medium', 'deep', 'held_out']
  const recallRows = groups.map(g => {
    const o: Record<string, string | number | null> = { group: GROUP_NAME[g] }
    for (const m of METHODS) o[m] = pick(m, g)?.recall ?? null
    return o
  })
  const meta = files.find(f => f.file === chosen)
  const cols = useMemo<ColumnDef<Agg, any>[]>(() => [
    { header: 'Method', accessorKey: 'method', cell: c => <span className="inline-flex items-center gap-1.5 font-medium text-ink"><span className="size-2.5 rounded-[3px]" style={{ background: METHOD_COLOR[c.getValue()] }} />{METHOD_NAME[c.getValue()]}</span> },
    { header: 'Group', accessorKey: 'group', cell: c => GROUP_NAME[c.getValue()] },
    { header: 'Cases', accessorKey: 'complaints', meta: { align: 'right' } },
    { header: 'Innocent money', accessorKey: 'innocent', meta: { align: 'right' }, cell: c => short(c.getValue()) },
    { header: 'Innocent accounts', accessorKey: 'innocent_accounts', meta: { align: 'right' } },
    { header: 'Beyond the stolen money', accessorKey: 'beyond_taint', meta: { align: 'right' }, cell: c => short(c.getValue()) },
    { header: 'Stolen money held', accessorKey: 'stolen_held', meta: { align: 'right' }, cell: c => short(c.getValue()) },
    { header: 'Mule precision', accessorKey: 'precision', meta: { align: 'right' }, cell: c => pct(c.getValue()) },
    { header: 'Mules caught', accessorKey: 'recall', meta: { align: 'right' }, cell: c => pct(c.getValue()) },
    { header: 'Mules liened', accessorKey: 'lien_recall', meta: { align: 'right' }, cell: c => pct(c.getValue()) },
    { header: 'Reviews per case', accessorKey: 'reviews_per_case', meta: { align: 'right' }, cell: c => Number(c.getValue()).toFixed(2) },
  ], [])
  const labels = res.labels_vs_ibm?.tracehold ?? {}
  return (
    <div className="space-y-6" id="tour-methods">
      <Panel title="TraceHold against the alternatives" subtitle="The same complaints, four ways of freezing money: a full freeze of every account on the trail, a greedy trace that freezes up to the disputed amount at every hop, fixed rules on the signals, and TraceHold's agents."
        actions={<div className="flex flex-wrap items-center gap-2">
          <Select className="h-8 w-80 text-[13px]" value={chosen} onChange={e => setFile(e.target.value)} aria-label="Results file">
            {files.map(f => <option key={f.file} value={f.file}>Sample {f.sample.toUpperCase()}, rubric {f.rubric}, {f.complaints ?? '?'} complaints, {f.written}</option>)}
          </Select>
          <Segmented size="sm" value={scope} onChange={setScope} options={[{ value: 'sample', label: 'TraceHold\'s sample' }, { value: 'all_test', label: 'All test complaints' }]} />
        </div>}>
        <Ledger items={[
          { label: 'Less innocent money than a full freeze', value: pct(less(th, ff), 1), tone: 'brand' },
          { label: 'Less than the greedy trace', value: pct(less(th, gr), 1) },
          { label: 'Less than rules only', value: pct(less(th, ro), 1) },
          { label: 'Mules caught (lien or review)', value: pct(th?.recall), note: `rules only ${pct(ro?.recall)}` },
          { label: 'Mule precision of liens', value: pct(th?.precision), note: `rules only ${pct(ro?.precision)}` },
        ]} />
        <div className="mt-4 rounded-[12px] border border-rule bg-paper/50 px-4 py-3">
          <div className="flex flex-wrap items-center gap-2 text-[13px]">
            <span className="font-medium text-ink">Sensitivity check:</span>
            <span className="text-ink-3">leave out a complaint that dominates the innocent-money total</span>
            {heavy.map(c => (
              <button key={c.complaint_id} onClick={() => { const s = new Set(exclude); if (s.has(c.complaint_id)) s.delete(c.complaint_id); else s.add(c.complaint_id); setExclude(s) }}
                className={cn('inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[12.5px] transition-colors',
                  exclude.has(c.complaint_id) ? 'border-brand-600 bg-brand-600 text-white' : 'border-rule-2 bg-card text-ink-2 hover:border-brand-300')}>
                {c.complaint_id} <span className={exclude.has(c.complaint_id) ? 'text-brand-100' : 'text-ink-3'}>{pct(c.share)} of TraceHold's</span>
                {exclude.has(c.complaint_id) && <X className="size-3" />}
              </button>
            ))}
          </div>
          {exclude.size > 0 && <p className="mt-2 text-[12.5px] text-review">A check chosen after seeing the results, not the headline. Left out: {[...exclude].join(', ')}.</p>}
          {meta?.sample === 'b' && exclude.size === 0 && <p className="mt-2 text-[12.5px] text-ink-3">On sample B one deep complaint, CMP-0216, holds Rs 5,085 crore of stolen money in an account IBM labels genuine; every method liens it, so it sets the innocent-money totals.</p>}
        </div>
      </Panel>
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        <Panel title="Innocent money frozen, by trail depth" subtitle="Rupees frozen on accounts IBM labels genuine. Log scale: each gridline is ten times the one before. TraceHold's figure is labelled; hover a dot for all four."
          actions={<LegendRow items={METHODS.map(m => ({ key: m, label: METHOD_NAME[m], color: METHOD_COLOR[m] }))} />}>
          <DotPlot ariaLabel="Innocent money frozen by each method, per trail depth group, on a log scale"
            series={METHODS.map(m => ({ key: m, label: METHOD_NAME[m], color: METHOD_COLOR[m], labelled: m === 'tracehold' }))}
            rows={groups.map(g => ({ key: g, label: GROUP_NAME[g], values: Object.fromEntries(METHODS.map(m => [m, pick(m, g)?.innocent ?? null])) }))} />
        </Panel>
        <Panel title="Mules caught, by trail depth" subtitle="Share of the true trail's mules each method liens or sends to review."
          actions={<LegendRow items={METHODS.map(m => ({ key: m, label: METHOD_NAME[m], color: METHOD_COLOR[m] }))} />}>
          <div className="h-[280px]">
            <ResponsiveContainer>
              <BarChart data={recallRows} margin={{ top: 8, right: 8, bottom: 4, left: 0 }} barGap={2}>
                <CartesianGrid stroke="var(--color-rule)" vertical={false} />
                <XAxis dataKey="group" {...AXIS} tickMargin={6} />
                <YAxis domain={[0, 1]} {...AXIS} width={44} tickFormatter={v => `${Math.round(v * 100)}%`} />
                <Tooltip cursor={{ fill: 'var(--color-brand-50)' }} content={<ChartTip format={v => pct(Number(v), 1)} />} />
                <Legend content={() => null} />
                {METHODS.map(m => <Bar key={m} dataKey={m} name={METHOD_NAME[m]} fill={METHOD_COLOR[m]} radius={[4, 4, 0, 0]} isAnimationActive={false} />)}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Panel>
      </div>
      <Panel title="Every method in every group" subtitle={`From ${chosen}${exclude.size ? `, leaving out ${[...exclude].join(', ')}` : ''}. Money rounded; open the results file for the paisa.`}>
        <DataTable data={rows} columns={cols} rowId={r => `${r.method}-${r.group}`} pageSize={20} dense />
      </Panel>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <Panel title="Claude's labels against IBM's" subtitle="TraceHold's sample. IBM marks laundering money in general, not per complaint, so the two often differ.">
          <table className="w-full text-[13px]">
            <thead className="text-left text-[12.5px] text-ink-3"><tr><th className="py-1.5 font-medium">Claude said</th><th className="py-1.5 text-right font-medium">IBM mule</th><th className="py-1.5 text-right font-medium">IBM genuine</th></tr></thead>
            <tbody>
              {Object.entries(labels).map(([k, v]) => (
                <tr key={k} className="border-t border-rule/70"><td className="py-1.5"><Badge tone={k === 'mule' ? 'victim' : k === 'likely_genuine' ? 'approved' : 'review'}>{k.replace('_', ' ')}</Badge></td>
                  <td className="num py-1.5 text-right">{v.mule ?? 0}</td><td className="num py-1.5 text-right">{v.genuine ?? 0}</td></tr>
              ))}
            </tbody>
          </table>
        </Panel>
        <Panel title="Two complaints, one account" subtitle="When complaints share an account, the earlier report keeps its lien and a later one can lien only what is left.">
          <dl className="space-y-1.5 text-[13px]">
            {[['Overlapping sets', res.overlap.overlapping_sets], ['Shared accounts', res.overlap.shared_accounts], ['No account liened beyond its balance', res.overlap.account_limit_holds ? 'Holds' : 'Fails'],
              ['No complaint liened beyond its disputed amount', res.overlap.complaint_limit_holds ? 'Holds' : 'Fails'],
              ['Accounts a greedy trace over-claims', res.overlap_greedy?.claims_over_balance_before_rule ?? '-']].map(([k, v]) => (
              <div key={String(k)} className="flex justify-between gap-3"><dt className="text-ink-3">{k}</dt><dd className="num font-medium text-ink">{String(v)}</dd></div>
            ))}
          </dl>
        </Panel>
        <Panel title="Under load" subtitle={res.load_test ? `${res.load_test.complaints} complaints in parallel at bank ${res.load_test.bank_id}, ${res.load_test.wall_seconds} s` : 'No load test in this file.'}>
          {res.load_test && (
            <dl className="space-y-1.5 text-[13px]">
              {Object.entries(res.load_test.latency_ms).map(([k, v]) => (
                <div key={k} className="flex justify-between gap-3"><dt className="text-ink-3">{k.replaceAll('_', ' ')}</dt><dd className="num text-ink">p50 {v.p50} ms, p95 {v.p95} ms</dd></div>
              ))}
              <div className="flex justify-between gap-3"><dt className="text-ink-3">Duplicates, cross-bank leaks</dt><dd className="num font-medium text-ink">{res.load_test.duplicates.total}, {res.load_test.leaks.total}</dd></div>
            </dl>
          )}
        </Panel>
      </div>
    </div>
  )
}

export function FinanceView() {
  return (
    <div className="space-y-6">
      <MoneyFlow />
      <Methods />
    </div>
  )
}
