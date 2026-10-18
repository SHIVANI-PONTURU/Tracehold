import { useQuery } from '@tanstack/react-query'
import { Database, Pause, Play, RotateCcw, SkipForward } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { useApp } from '@/app/context'
import { Badge, Button, Empty, Ledger, Mono, Panel, Segmented, Select, Spinner, Tip } from '@/components/ui'
import { TrailGraph, TrailLegend } from '@/features/investigations/TrailGraph'
import { api } from '@/lib/api'
import { cn, duration, num, usd } from '@/lib/format'
import { AGENT_HEX, AGENT_INFO, STATUS_INFO } from '@/lib/status'
import type { GraphStats, QueryEntry, Step } from '@/lib/types'
import { layerAt, maskTrail, phaseOf, stepTools, TOOL_QUERY } from './graphModel'

const PHASE_TEXT = {
  trace: 'The Tracer follows the money, one layer per judgement', adjudicate: 'The Adjudicator labels each account and proposes liens',
  verify: 'The Verifier checks every proposed lien', gate: 'The gate: a person decides, approved liens go to the bank', done: 'Replay finished',
}

const graphName = (g: string, bank: string) => (g === 'memory' ? `memory_${bank}` : g === 'scratch' ? 'a scratch graph' : g)

/** What FalkorDB holds right now: the network graph the agents read, this bank's memory, and how busy the server is. */
function Glance({ bank, stats }: { bank: string; stats?: GraphStats }) {
  const act = useQuery({ queryKey: ['activity-totals'], queryFn: () => api.activity(0, undefined, undefined, 1), refetchInterval: 10000 })
  const net = stats?.network ?? {}
  const n = (v?: number) => (v == null ? '...' : num(v))
  const mem = Object.values(stats?.memory?.[bank] ?? {}).reduce((s, v) => s + v, 0)
  const queries = Object.values(act.data?.totals ?? {}).reduce((s, t) => s + t.queries, 0)
  return (
    <Ledger items={[
      { label: 'Accounts in the network graph', value: n(net.Account), note: `${n(net.Bank)} banks, ${n(net.Customer)} customers`, tone: 'brand' },
      { label: 'Transfers between them', value: n(net.TRANSFERRED), note: 'the edges the Tracer follows' },
      { label: 'Devices, phones and IPs', value: stats ? num((net.Device ?? 0) + (net.Phone ?? 0) + (net.IP ?? 0)) : '...', note: 'the links the Adjudicator checks' },
      { label: 'This bank\'s memory', value: stats ? num(mem) : '...', note: `nodes in memory_${bank}` },
      { label: 'Queries since the server started', value: act.data ? num(queries) : '...', note: `${stats ? stats.graphs.length : '...'} graphs on the server` },
    ]} />
  )
}

/** The step just played: the agent, the tools it called and the Cypher those tools ran on FalkorDB. */
function NowPlaying({ steps, k, bank }: { steps: Step[]; k: number; bank: string }) {
  const s = k > 0 ? steps[Math.min(k, steps.length) - 1] : undefined
  const tools = s ? stepTools(s) : []
  const cy = tools.filter(t => TOOL_QUERY[t]?.cypher)
  const byAgent = (a: string) => steps.filter(x => x.agent === a).length
  return (
    <div className="mt-4 grid min-h-[132px] grid-cols-1 gap-3 rounded-[12px] border border-rule bg-paper/50 p-3 md:grid-cols-[230px_minmax(0,1fr)]" aria-live="polite">
      {!s ? (
        <div className="md:col-span-2">
          <p className="text-[13px] font-medium text-ink">{steps.length} recorded steps in this run</p>
          <p className="mt-0.5 text-[12.5px] text-ink-3">Tracer {byAgent('tracer')}, Adjudicator {byAgent('adjudicator')}, Verifier {byAgent('verifier')}{byAgent('gate') ? `, HITL gate ${byAgent('gate')}` : ''}. Press play or step through: each step shows the agent, the tools it called and the Cypher those tools ran on FalkorDB, while the trail below grows layer by layer.</p>
        </div>
      ) : (
        <>
          <div className="min-w-0">
            <p className="num text-[11.5px] text-ink-3">{k >= steps.length ? 'Last step' : `Step ${k} of ${steps.length}`}, {duration(s.latency_ms)}</p>
            <div className="mt-1 flex items-center gap-2">
              <span className="size-2.5 shrink-0 rounded-full" style={{ background: AGENT_HEX[s.agent] ?? '#8aa293' }} aria-hidden />
              <span className="text-[15px] font-semibold text-ink">{AGENT_INFO[s.agent]?.name ?? s.agent}</span>
              <span className="truncate text-[12.5px] text-ink-3">{s.node}</span>
            </div>
            <div className="mt-2 flex flex-wrap gap-1">
              {tools.length ? tools.map(t => (
                <span key={t} className={cn('id rounded px-1.5 py-px text-[11px]', t === 'model' ? 'bg-[#fff1e9] text-[#9a3f12]' : 'bg-card text-ink-2 ring-1 ring-rule')}>{t}</span>
              )) : <span className="text-[12px] text-ink-3">No tool in this step</span>}
            </div>
          </div>
          <div className="min-w-0">
            {cy.length ? (
              <div className="rounded-[8px] bg-ink px-3 py-2">
                <div className="text-[11.5px] text-brand-200">{cy[0]} on {graphName(TOOL_QUERY[cy[0]].graph, bank)}: {TOOL_QUERY[cy[0]].does}</div>
                <pre className="mt-1 max-h-[96px] overflow-auto whitespace-pre-wrap font-mono text-[11.5px] leading-snug text-white/90 scroll-thin">{TOOL_QUERY[cy[0]].cypher}</pre>
                {cy.length > 1 && <div className="mt-1 text-[11px] text-white/60">and {cy.slice(1).join(', ')} in the same step</div>}
              </div>
            ) : (
              <p className="flex h-full items-center rounded-[8px] border border-dashed border-rule-2 px-3 py-2 text-[12.5px] text-ink-3">
                {tools.includes('model') ? 'Claude Opus 4.6 decides in this step; no graph query.' : 'No graph query in this step.'}
              </p>
            )}
          </div>
        </>
      )}
    </div>
  )
}

export function GraphView() {
  const { bank, setBank, banks } = useApp()
  const [params, setParams] = useSearchParams()
  const qBank = params.get('bank')
  useEffect(() => { if (qBank && qBank !== bank && banks.some(b => b.bank_id === qBank)) setBank(qBank) }, [qBank, bank, banks, setBank])
  const invs = useQuery({ queryKey: ['investigations', bank], queryFn: () => api.investigations(bank), enabled: !!bank })
  const inv = params.get('inv') ?? invs.data?.find(i => i.status !== 'new')?.id ?? ''
  const detail = useQuery({ queryKey: ['detail', bank, inv], queryFn: () => api.detail(bank, inv), enabled: !!inv })
  const trail = useQuery({ queryKey: ['trail', bank, inv], queryFn: () => api.trail(bank, inv), enabled: !!inv })
  const stats = useQuery({ queryKey: ['graph-stats', bank], queryFn: () => api.graphStats([bank]), staleTime: 600000 })
  const steps = useMemo(() => [...(detail.data?.steps ?? [])].sort((a, b) => a.started_ms - b.started_ms), [detail.data])
  const maxLayer = Math.max(0, ...(trail.data?.nodes ?? []).map(n => n.layer))
  const [k, setK] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<'1' | '2' | '4'>('2')
  useEffect(() => { setK(0); setPlaying(false) }, [inv])
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => {
    if (!playing) return
    if (k >= steps.length) { setPlaying(false); return }
    const s = steps[k]
    const wait = Math.max(450, Math.min(1800, (s?.latency_ms ?? 600) / 2)) / Number(speed)
    timer.current = window.setTimeout(() => setK(x => x + 1), wait)
    return () => window.clearTimeout(timer.current)
  }, [playing, k, steps, speed])
  const phase = phaseOf(steps, k)
  const visible = trail.data ? layerAt(steps, k, maxLayer) : 0
  const shown = useMemo(() => (trail.data ? maskTrail(trail.data, phase) : null), [trail.data, phase])
  const net = stats.data?.network ?? {}
  const listRef = useRef<HTMLOListElement>(null)
  useEffect(() => {
    const list = listRef.current
    const el = list?.querySelector<HTMLElement>('[data-current="true"]')
    if (list && el) list.scrollTo({ top: Math.max(0, el.offsetTop - list.clientHeight / 2), behavior: 'smooth' })
  }, [k])
  const pick = (id: string) => setParams({ bank, inv: id })
  return (
    <div className="space-y-6">
      <Glance bank={bank} stats={stats.data} />
      <Panel raised id="tour-replay">
        <div className="flex flex-wrap items-end gap-3">
          <label className="min-w-[240px] flex-1">
            <span className="text-[12.5px] text-ink-3">Run to replay</span>
            <Select className="mt-1" value={inv} onChange={e => pick(e.target.value)}>
              {(invs.data ?? []).map(i => <option key={i.id} value={i.id}>{i.complaint_id}, {i.id}, {STATUS_INFO[i.status]?.label}</option>)}
            </Select>
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="primary" icon={playing ? <Pause className="size-4" /> : <Play className="size-4" />} disabled={!steps.length}
              onClick={() => { if (k >= steps.length) setK(0); setPlaying(p => !p) }}>{playing ? 'Pause' : k > 0 && k < steps.length ? 'Resume' : 'Play the run'}</Button>
            <Tip content="Next step"><Button icon={<SkipForward className="size-4" />} aria-label="Next step" disabled={k >= steps.length} onClick={() => { setPlaying(false); setK(x => Math.min(steps.length, x + 1)) }} /></Tip>
            <Tip content="Back to the start"><Button icon={<RotateCcw className="size-4" />} aria-label="Restart" onClick={() => { setPlaying(false); setK(0) }} /></Tip>
            <Segmented size="sm" value={speed} onChange={setSpeed} options={[{ value: '1', label: '1x' }, { value: '2', label: '2x' }, { value: '4', label: '4x' }]} />
          </div>
        </div>
        <div className="mt-4">
          <input type="range" min={0} max={Math.max(1, steps.length)} value={k} onChange={e => { setPlaying(false); setK(Number(e.target.value)) }}
            className="w-full accent-[var(--color-brand-600)]" aria-label="Replay position" />
          <div className="mt-1 flex justify-between gap-3 text-[12px] text-ink-3"><span>{k === 0 ? 'Ready to play' : PHASE_TEXT[phase]}</span><span className="num shrink-0">step {Math.min(k, steps.length)} of {steps.length}</span></div>
        </div>
        <NowPlaying steps={steps} k={k} bank={bank} />
      </Panel>
      {!inv ? <Empty title="No run to replay at this bank yet" /> : detail.isLoading || trail.isLoading ? <Spinner /> : (
        <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
          <Panel title="The trail as the agents built it" subtitle="Money moves along the transfers into the newest layer. Labels appear when the Adjudicator runs, lien outcomes after the gate." bodyClass="pt-3">
            <div className="mb-3"><TrailLegend /></div>
            {shown && shown.nodes.length ? <TrailGraph trail={shown} visibleLayer={visible} flowLayer={k > 0 && phase === 'trace' ? visible : null} height={460} />
              : <p className="py-10 text-center text-[13px] text-ink-3">This run has no trail yet.</p>}
          </Panel>
          <Panel title="Steps" subtitle={`${steps.length} recorded steps; click one to jump to it`} bodyClass="pt-2">
            <ol ref={listRef} className="max-h-[520px] space-y-1.5 overflow-y-auto pr-1 scroll-thin">
              {steps.map((s, i) => {
                const ts = stepTools(s)
                const current = i === k - 1
                return (
                  <li key={s.id} data-current={current} className={cn('rounded-[10px] border px-3 py-2 transition-colors', current ? 'border-brand-500 bg-brand-50 shadow-card' : i < k ? 'border-rule bg-card' : 'border-rule/60 bg-paper/40 opacity-70')}>
                    <button className="w-full text-left" onClick={() => { setPlaying(false); setK(i + 1) }}>
                      <div className="flex items-center gap-2">
                        <span className="size-2 rounded-full" style={{ background: AGENT_HEX[s.agent] ?? '#8aa293' }} />
                        <span className="text-[13px] font-medium text-ink">{AGENT_INFO[s.agent]?.name ?? s.agent}</span>
                        <span className="text-[12.5px] text-ink-3">{s.node}</span>
                        <span className="num ml-auto text-[11.5px] text-ink-3">{duration(s.latency_ms)}{s.usd ? `, ${usd(s.usd, 3)}` : ''}</span>
                      </div>
                      {ts.length > 0 && <div className="mt-1 flex flex-wrap gap-1">{ts.map(t => <span key={t} className={cn('id rounded px-1.5 py-px text-[11px]', t === 'model' ? 'bg-[#fff1e9] text-[#9a3f12]' : 'bg-paper-2 text-ink-2')}>{t}</span>)}</div>}
                    </button>
                  </li>
                )
              })}
            </ol>
          </Panel>
        </div>
      )}
      <LiveQueries bank={bank} stats={net} />
    </div>
  )
}

type Source = 'tool' | 'agent' | 'ui' | 'all'
const SRC_TEXT: Record<string, string> = { tool: 'Agent tool', agent: 'Agent polling', ui: 'This UI', other: 'Other' }

function LiveQueries({ bank, stats }: { bank: string; stats: Record<string, number> }) {
  const [source, setSource] = useState<Source>('tool')
  const [rows, setRows] = useState<QueryEntry[]>([])
  const since = useRef(0)
  const [totals, setTotals] = useState<Record<string, { queries: number; ms: number; errors: number }>>({})
  useEffect(() => { since.current = 0; setRows([]) }, [source])
  useEffect(() => {
    let stop = false
    const tick = async () => {
      try {
        const r = await api.activity(since.current, source === 'all' ? undefined : source, undefined, 150)
        if (stop) return
        if (r.entries.length) {
          since.current = r.entries[r.entries.length - 1].seq
          setRows(prev => [...prev, ...r.entries].slice(-150))
        }
        setTotals(r.totals)
      } catch { /* the next tick tries again */ }
    }
    tick()
    const id = window.setInterval(tick, 2000)
    return () => { stop = true; window.clearInterval(id) }
  }, [source])
  const now = Date.now()
  const perSec = useMemo(() => {
    const b = Array.from({ length: 60 }, () => ({ network: 0, memory: 0, other: 0 }))
    for (const e of rows) {
      const age = Math.floor((now - e.ts_ms) / 1000)
      if (age >= 0 && age < 60) b[59 - age][e.graph === 'network' ? 'network' : e.graph.startsWith('memory_') ? 'memory' : 'other']++
    }
    return b
  }, [rows, now])
  const peak = Math.max(1, ...perSec.map(x => x.network + x.memory + x.other))
  const newest = [...rows].reverse().slice(0, 40)
  const all = Object.entries(totals).sort((a, b) => b[1].queries - a[1].queries)
  return (
    <Panel title="FalkorDB right now" subtitle="Every query the tool server sends to FalkorDB, as it happens. Agents' tools read the network graph; their polling and this page read the banks' memory graphs."
      actions={<Segmented size="sm" value={source} onChange={setSource} options={[{ value: 'tool', label: 'Agent tools' }, { value: 'agent', label: 'Agent polling' }, { value: 'ui', label: 'This UI' }, { value: 'all', label: 'All' }]} />}
      id="tour-live">
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1fr)_280px]">
        <div>
          <div className="flex h-16 items-end gap-[2px] rounded-[10px] bg-paper/70 px-2 pb-1.5 pt-2" role="img" aria-label="Queries per second, last minute">
            {perSec.map((x, i) => (
              <div key={i} className="flex flex-1 flex-col-reverse overflow-hidden rounded-t-[2px]" style={{ height: `${((x.network + x.memory + x.other) / peak) * 100}%` }}
                title={`${59 - i} s ago: ${x.network} network, ${x.memory} memory, ${x.other} other`}>
                {x.network > 0 && <div style={{ flex: x.network, background: '#2a78d6' }} />}
                {x.memory > 0 && <div style={{ flex: x.memory, background: '#eb6834' }} className="border-t border-white" />}
                {x.other > 0 && <div style={{ flex: x.other, background: '#1baf7a' }} className="border-t border-white" />}
              </div>
            ))}
          </div>
          <div className="mt-1.5 flex items-center justify-between text-[12px] text-ink-3">
            <span className="flex gap-3"><span className="inline-flex items-center gap-1"><span className="size-2.5 rounded-[3px] bg-[#2a78d6]" />network</span><span className="inline-flex items-center gap-1"><span className="size-2.5 rounded-[3px] bg-[#eb6834]" />memory graphs</span><span className="inline-flex items-center gap-1"><span className="size-2.5 rounded-[3px] bg-[#1baf7a]" />scratch and other</span></span>
            <span>last 60 seconds, queries per second</span>
          </div>
          <div className="mt-4 overflow-hidden rounded-[12px] border border-rule">
            <table className="w-full table-fixed text-left text-[12.5px]">
              <thead className="bg-paper-2 text-ink-3"><tr>
                <th className="w-[76px] px-3 py-2 font-medium">Time</th><th className="w-[110px] px-3 py-2 font-medium">Graph</th><th className="w-[170px] px-3 py-2 font-medium">Who</th>
                <th className="px-3 py-2 font-medium">Query</th><th className="w-[70px] px-3 py-2 text-right font-medium">ms</th><th className="w-[56px] px-3 py-2 text-right font-medium">Rows</th>
              </tr></thead>
              <tbody>
                {newest.length === 0 && <tr><td colSpan={6} className="px-3 py-6 text-center text-ink-3">{source === 'tool' ? 'No agent tool query since this page opened. Start an investigation to watch the agents query the graph.' : 'Waiting for queries...'}</td></tr>}
                {newest.map(e => (
                  <tr key={e.seq} className="border-t border-rule/70 align-top">
                    <td className="num px-3 py-1.5 text-ink-3">{new Date(e.ts_ms).toLocaleTimeString('en-GB')}</td>
                    <td className="px-3 py-1.5"><span className={cn('id rounded px-1.5 py-px text-[11px]', e.graph === 'network' ? 'bg-proposed-tint text-proposed' : e.graph.startsWith('memory_') ? 'bg-rejected-tint text-rejected' : 'bg-approved-tint text-approved')}>{e.graph}</span></td>
                    <td className="truncate px-3 py-1.5 text-ink-2" title={e.investigation_id}>{SRC_TEXT[e.source]}: <span className="id">{e.name}</span></td>
                    <td className="truncate px-3 py-1.5 font-mono text-[11.5px] text-ink-2" title={e.query}>{e.query}</td>
                    <td className="num px-3 py-1.5 text-right text-ink-2">{e.ms.toFixed(1)}</td>
                    <td className="num px-3 py-1.5 text-right text-ink-3">{e.rows ?? '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <aside className="space-y-4">
          <div className="rounded-[12px] border border-rule p-3.5">
            <div className="flex items-center gap-2 text-[13px] font-semibold text-ink"><Database className="size-4 text-brand-600" />The network graph</div>
            <dl className="mt-2 space-y-1 text-[12.5px]">
              {Object.entries(stats).sort((a, b) => b[1] - a[1]).map(([k, v]) => (
                <div key={k} className="flex justify-between gap-2"><dt className="id text-ink-3">{k}</dt><dd className="num font-medium text-ink">{num(v)}</dd></div>
              ))}
              {!Object.keys(stats).length && <div className="text-ink-3">Counting...</div>}
            </dl>
          </div>
          <div className="rounded-[12px] border border-rule p-3.5">
            <div className="text-[13px] font-semibold text-ink">Since the tool server started</div>
            <dl className="mt-2 space-y-1 text-[12.5px]">
              {all.slice(0, 8).map(([g, t]) => (
                <div key={g} className="flex justify-between gap-2"><dt className="id truncate text-ink-3">{g}</dt><dd className="num text-ink">{num(t.queries)} <span className="text-ink-3">({(t.ms / Math.max(1, t.queries)).toFixed(1)} ms avg)</span></dd></div>
              ))}
            </dl>
            <p className="mt-2 text-[11.5px] text-ink-3">Bank {bank}'s memory is <Mono className="text-[11px]">memory_{bank}</Mono>; no other bank's agents can reach it.</p>
          </div>
          <Badge tone="brand" dot className="text-[11.5px]">Updates every 2 seconds</Badge>
        </aside>
      </div>
    </Panel>
  )
}
