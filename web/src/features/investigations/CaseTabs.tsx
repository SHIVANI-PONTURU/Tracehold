import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Ban, Bot, CircleUser, FileSearch, Send, Undo2 } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useApp } from '@/app/context'
import { Badge, Button, Empty, ErrorNote, Input, Mono, SectionTitle } from '@/components/ui'
import { api } from '@/lib/api'
import { ago, cn, duration, inr, usd, when } from '@/lib/format'
import { ACTION_INFO, AGENT_HEX, AGENT_INFO, LABEL_INFO, report } from '@/lib/status'
import type { Detail } from '@/lib/types'

const SIGNAL: Record<string, string> = {
  pass_through_ratio: 'Pass-through ratio', median_minutes_to_forward: 'Minutes to forward', distinct_payers: 'Distinct payers',
  distinct_payees: 'Distinct payees', account_age_days: 'Account age', owner_kind: 'Owner kind', is_merchant: 'Registered merchant',
  inflow_count: 'Inflows', outflow_count: 'Outflows', tainted_share: 'Share of balance that is the victim\'s',
  accounts_sharing_device: 'Accounts sharing a device', accounts_sharing_phone: 'Accounts sharing a phone',
  accounts_sharing_ip: 'Accounts sharing an IP', devices_shared_with_known_mules: 'Devices shared with known mules',
  phones_shared_with_known_mules: 'Phones shared with known mules', related_cases: 'Related cases', prior_decisions: 'Earlier decisions',
  mule_community: 'Mule community', key_account: 'Key account in its community',
}
export const signalName = (s: string) => SIGNAL[s] ?? s.replaceAll('_', ' ')

export function EvidenceTab({ detail, account, onSelect }: { detail: Detail; account: string | null; onSelect: (id: string) => void }) {
  const r = useMemo(() => report(detail), [detail])
  const accounts = [...r.props.keys()]
  const acc = account && r.props.has(account) ? account : accounts[0]
  if (!acc) return <Empty title="No evidence yet" icon={<FileSearch className="size-6" />}>Evidence appears when the Adjudicator has judged the traced accounts.</Empty>
  const p = r.props.get(acc)!
  const t = r.trace?.accounts.find(a => a.account_id === acc)
  const v = p.lien_proposal_id ? r.verdicts.get(p.lien_proposal_id) : undefined
  const d = p.lien_proposal_id ? r.decisions.get(p.lien_proposal_id) : undefined
  const claims = detail.claims.filter(c => c.account_id === acc)
  const claim = claims[claims.length - 1]
  const acts = detail.actions.filter(a => a.entity_id === acc)
  return (
    <div className="grid grid-cols-1 gap-5 lg:grid-cols-[220px_1fr]">
      <ul className="space-y-1" aria-label="Accounts">
        {accounts.map(a => {
          const pa = r.props.get(a)!
          return (
            <li key={a}>
              <button onClick={() => onSelect(a)} className={cn('flex w-full items-center justify-between gap-2 rounded-[8px] px-2.5 py-2 text-left transition-colors',
                a === acc ? 'bg-brand-50 text-ink' : 'hover:bg-paper')}>
                <span className="id truncate">{a}</span>
                <Badge tone={LABEL_INFO[pa.label]?.tone ?? 'none'} dot={false} className="text-[11px]">{LABEL_INFO[pa.label]?.label}</Badge>
              </button>
            </li>
          )
        })}
      </ul>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <Mono className="text-[14px] font-medium">{acc}</Mono>
          <Badge tone={LABEL_INFO[p.label]?.tone ?? 'none'}>{LABEL_INFO[p.label]?.label}</Badge>
          <Badge tone={p.proposed_action === 'lien' ? 'proposed' : p.proposed_action === 'none' ? 'none' : 'review'} dot={false}>{ACTION_INFO[p.proposed_action]}</Badge>
          {p.lien_amount_paise != null && <span className="num text-[14px] font-semibold text-ink">{inr(p.lien_amount_paise)}</span>}
        </div>
        {t && (
          <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 rounded-[10px] bg-paper/70 px-4 py-3 text-[13px] sm:grid-cols-4">
            <div><dt className="text-ink-3">Layer</dt><dd className="num font-medium text-ink">{t.layer}</dd></div>
            <div><dt className="text-ink-3">Victim's money here</dt><dd className="num font-medium text-ink">{inr(t.tainted_paise)}</dd></div>
            <div><dt className="text-ink-3">Balance</dt><dd className="num font-medium text-ink">{inr(t.balance_paise)}</dd></div>
            <div><dt className="text-ink-3">Share of balance</dt><dd className="num font-medium text-ink">{t.balance_paise ? `${((t.tainted_paise / t.balance_paise) * 100).toFixed(2)}%` : '-'}</dd></div>
          </dl>
        )}
        <SectionTitle>Why the Adjudicator decided this</SectionTitle>
        <p className="-mt-1 max-w-[72ch] text-[13.5px] leading-relaxed text-ink-2">{claim?.text || p.rationale}</p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {p.evidence.signals.map(s => <span key={s} className="rounded-full border border-rule bg-card px-2 py-0.5 text-[12px] text-ink-2">{signalName(s)}</span>)}
        </div>
        <dl className="mt-4 space-y-1.5 text-[13px]">
          <div className="flex gap-2"><dt className="w-36 shrink-0 text-ink-3">Transfers cited</dt><dd className="id break-all text-ink-2">{p.evidence.transfer_ids.join(', ') || '-'}</dd></div>
          <div className="flex gap-2"><dt className="w-36 shrink-0 text-ink-3">Lessons applied</dt><dd className="id break-all text-ink-2">{p.lessons_applied.join(', ') || 'None'}</dd></div>
          {claim && <div className="flex gap-2"><dt className="w-36 shrink-0 text-ink-3">Claim</dt><dd className="id text-ink-2">{claim.id}</dd></div>}
        </dl>
        {v && (
          <div className="mt-4 rounded-[10px] border border-rule px-4 py-3">
            <div className="flex items-center gap-2 text-[13px] font-medium text-ink">
              Verifier <Badge tone={v.verdict === 'approve' ? 'approved' : 'rejected'}>{v.verdict}</Badge>
              {d?.approved != null && <Badge tone={d.approved ? 'approved' : 'rejected'} dot={false}>Gate: {d.approved ? 'approved' : 'rejected'} by {d.decided_by === 'human' ? 'a person' : 'the Verifier'}</Badge>}
            </div>
            <p className="mt-1.5 text-[13px] leading-relaxed text-ink-2">{v.reason}</p>
            {d?.gate_reason && d.decided_by === 'human' && <p className="mt-1 text-[12.5px] text-ink-3">Gate reason: {d.gate_reason}</p>}
          </div>
        )}
        {acts.length > 0 && (
          <p className="mt-3 text-[12.5px] text-ink-3">Automatic actions on this account: {acts.map(a => `${a.kind.replace('_', ' ')} (${a.status})`).join(', ')}</p>
        )}
      </div>
    </div>
  )
}

export function ActionsTab({ detail }: { detail: Detail }) {
  const { bank } = useApp()
  const qc = useQueryClient()
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const undo = useMutation({
    mutationFn: (a: { id: string; reason: string }) => api.undo(bank, detail.investigation.id, a.id, a.reason),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['detail', bank, detail.investigation.id] }),
  })
  if (!detail.actions.length) return <Empty title="No automatic actions">The Adjudicator opens reviews, asks for documents and flags shared devices on its own. They appear here, and each can be undone.</Empty>
  const KIND: Record<string, string> = { review: 'Review opened', info_request: 'Documents requested', flag: 'Entity flagged' }
  return (
    <div>
      <p className="mb-3 text-[13px] text-ink-3">Taken by the Adjudicator without a person, because none of them freezes money. Undo one with a reason.</p>
      <ul className="divide-y divide-rule rounded-[12px] border border-rule">
        {detail.actions.map(a => (
          <li key={a.id} className="flex flex-wrap items-start gap-x-4 gap-y-2 px-4 py-3">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[13.5px] font-medium text-ink">{KIND[a.kind] ?? a.kind}</span>
                <span className="text-[12.5px] text-ink-3">on {a.entity_kind}</span><Mono>{a.entity_id}</Mono>
                <Badge tone={a.status === 'undone' ? 'none' : a.status === 'cleared' ? 'approved' : a.status === 'escalated' ? 'rejected' : 'review'}>{a.status}</Badge>
              </div>
              <p className="mt-1 max-w-[80ch] text-[12.5px] leading-snug text-ink-2">{a.detail}</p>
              <p className="mt-0.5 text-[12px] text-ink-3"><span className="id">{a.id}</span>{a.external_id ? `, bank reference ${a.external_id}` : ''}{a.undo_reason ? `, undone: ${a.undo_reason}` : ''}{a.resolution_note ? `, resolved: ${a.resolution_note}` : ''}</p>
            </div>
            {a.status !== 'undone' && (
              <div className="flex w-full items-center gap-2 sm:w-auto">
                <Input className="h-8 w-56 text-[13px]" placeholder="Reason to undo" value={reasons[a.id] ?? ''} onChange={e => setReasons(r => ({ ...r, [a.id]: e.target.value }))} />
                <Button size="sm" icon={<Undo2 className="size-3.5" />} disabled={!(reasons[a.id] ?? '').trim()} busy={undo.isPending && undo.variables?.id === a.id}
                  onClick={() => undo.mutate({ id: a.id, reason: reasons[a.id].trim() })}>Undo</Button>
              </div>
            )}
          </li>
        ))}
      </ul>
      <div className="mt-2"><ErrorNote error={undo.error} /></div>
    </div>
  )
}

export function ChatTab({ detail }: { detail: Detail }) {
  const { bank } = useApp()
  const qc = useQueryClient()
  const inv = detail.investigation.id
  const chat = useQuery({
    queryKey: ['chat', bank, inv], queryFn: () => api.chat(bank, inv),
    refetchInterval: q => (q.state.data ?? []).some(m => m.role === 'human' && ['pending', 'answering'].includes(m.status)) ? 2500 : 15000,
  })
  const [text, setText] = useState('')
  const ask = useMutation({
    mutationFn: (t: string) => api.ask(bank, inv, t),
    onSuccess: () => { setText(''); qc.invalidateQueries({ queryKey: ['chat', bank, inv] }) },
  })
  const msgs = chat.data ?? []
  return (
    <div className="max-w-3xl">
      <p className="mb-3 text-[13px] text-ink-3">The Verifier answers from this case's record and cites evidence IDs. An answer that cites an ID that does not exist is rejected. Each answer is one model call, about 2 US cents.</p>
      <div className="space-y-3">
        {msgs.length === 0 && <Empty title="No questions yet">Try "Why was the largest account liened?" or "Which transfers brought the money to layer 2?"</Empty>}
        {msgs.map(m => {
          const human = m.role === 'human'
          return (
            <div key={m.id} className={cn('flex gap-2.5', human && 'flex-row-reverse')}>
              <span className={cn('grid size-7 shrink-0 place-items-center rounded-full', human ? 'bg-paper-2 text-ink-2' : 'bg-brand-100 text-brand-700')}>
                {human ? <CircleUser className="size-4" /> : <Bot className="size-4" />}
              </span>
              <div className={cn('max-w-[80%] rounded-[12px] px-3.5 py-2.5 text-[13.5px] leading-relaxed shadow-card',
                human ? 'bg-brand-600 text-white' : m.status === 'rejected' ? 'border border-danger/30 bg-danger-tint text-ink-2' : 'border border-rule bg-card text-ink-2')}>
                {m.status === 'rejected' && <p className="mb-1 flex items-center gap-1.5 text-[12px] font-medium text-danger"><Ban className="size-3.5" />Rejected by the ID check ({m.note})</p>}
                <p className="whitespace-pre-wrap">{m.text}</p>
                {human && ['pending', 'answering'].includes(m.status) && <p className="mt-1 text-[12px] text-brand-100">Waiting for the Verifier...</p>}
                {!human && m.cited_ids?.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">{m.cited_ids.map(c => <span key={c} className="id rounded bg-paper-2 px-1.5 py-px text-[11.5px] text-ink-2">{c}</span>)}</div>
                )}
              </div>
            </div>
          )
        })}
      </div>
      <form className="mt-4 flex gap-2" onSubmit={e => { e.preventDefault(); if (text.trim().length >= 3) ask.mutate(text.trim()) }}>
        <Input value={text} onChange={e => setText(e.target.value)} placeholder="Ask about this case" maxLength={1000} aria-label="Ask about this case" />
        <Button variant="primary" icon={<Send className="size-4" />} busy={ask.isPending} disabled={text.trim().length < 3}>Ask</Button>
      </form>
      <div className="mt-2"><ErrorNote error={ask.error} /></div>
    </div>
  )
}

export function MemoryTab({ detail }: { detail: Detail }) {
  const used = [...new Set(detail.claims.flatMap(c => c.lessons_applied ?? []))]
  return (
    <div className="space-y-4">
      {detail.lessons_made.length > 0 ? (
        <div>
          <SectionTitle>Lessons this case taught the bank</SectionTitle>
          <ul className="space-y-2">
            {detail.lessons_made.map(l => (
              <li key={l.id} className="rounded-[10px] border border-rule px-4 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Mono>{l.id}</Mono>
                  <Badge tone={l.stance === 'release' ? 'approved' : 'rejected'} dot={false}>{l.stance === 'release' ? 'Do not lien accounts like this' : 'Lien accounts like this'}</Badge>
                  <span className="num text-[12.5px] text-ink-3">confidence {l.confidence.toFixed(2)}</span>
                </div>
                <p className="mt-1 text-[13px] text-ink-2">{l.text}</p>
              </li>
            ))}
          </ul>
        </div>
      ) : <p className="text-[13px] text-ink-3">This case has taught no lesson. A lesson is made at the gate when a lien is rejected with a reason, or when a person overturns the Verifier.</p>}
      <p className="text-[13px] text-ink-3">Lessons the Adjudicator applied here: {used.length ? <span className="id text-ink-2">{used.join(', ')}</span> : 'none'}. The Bank memory page shows every lesson and how its confidence changed.</p>
    </div>
  )
}

export function TraceTab({ detail }: { detail: Detail }) {
  const inv = detail.investigation
  const spend = useQuery({ queryKey: ['spend', inv.id, 'trace'], queryFn: () => api.spend(inv.id) })
  const lf = useQuery({ queryKey: ['langfuse', inv.trace_id], queryFn: () => api.langfuse(inv.trace_id), enabled: !!inv.trace_id, staleTime: 60000 })
  const blocked = useQuery({ queryKey: ['blocked'], queryFn: api.blocked, staleTime: 30000 })
  const steps = [...detail.steps].sort((a, b) => a.started_ms - b.started_ms)
  const t0 = steps[0]?.started_ms ?? 0
  const t1 = Math.max(t0 + 1, ...steps.map(s => s.started_ms + Math.max(s.latency_ms, 200)))
  const agents = ['tracer', 'adjudicator', 'verifier', 'gate', 'chat'].filter(a => steps.some(s => s.agent === a))
  const byAgent = Object.entries(spend.data?.by_agent ?? {})
  return (
    <div className="space-y-6">
      <div>
        <SectionTitle aside={<span className="text-[12px] text-ink-3">{steps.length} steps, {duration(t1 - t0)} first to last</span>}>Agent timeline</SectionTitle>
        {steps.length === 0 ? <p className="text-[13px] text-ink-3">No steps recorded yet.</p> : (
          <div className="rounded-[12px] border border-rule bg-card p-3">
            {agents.map(a => (
              <div key={a} className="flex items-center gap-3 py-1.5">
                <span className="w-24 shrink-0 text-[12.5px] font-medium text-ink-2">{AGENT_INFO[a]?.name ?? a}</span>
                <div className="relative h-6 flex-1 rounded bg-paper/70">
                  {steps.filter(s => s.agent === a).map(s => (
                    <div key={s.id} title={`${s.node}: ${s.tool} (${duration(s.latency_ms)}, ${s.tokens} tokens, ${usd(s.usd, 4)})`}
                      className="absolute top-1 h-4 rounded-[3px] ring-1 ring-white"
                      style={{ left: `${((s.started_ms - t0) / (t1 - t0)) * 100}%`, width: `max(4px, ${(Math.max(s.latency_ms, 200) / (t1 - t0)) * 100}%)`,
                        background: AGENT_HEX[a] ?? '#8aa293', opacity: s.status === 'ok' ? 1 : 0.5 }} />
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div>
          <SectionTitle>Tokens and cost per agent</SectionTitle>
          <table className="w-full overflow-hidden rounded-[12px] border border-rule text-[13px]">
            <thead className="bg-paper-2 text-left text-[12.5px] text-ink-3"><tr><th className="px-3 py-2 font-medium">Agent</th><th className="px-3 py-2 text-right font-medium">Calls</th><th className="px-3 py-2 text-right font-medium">Tokens</th><th className="px-3 py-2 text-right font-medium">Ledger</th><th className="px-3 py-2 text-right font-medium">Langfuse</th></tr></thead>
            <tbody>
              {byAgent.map(([a, v]) => (
                <tr key={a} className="border-t border-rule/70">
                  <td className="px-3 py-2"><span className="mr-2 inline-block size-2 rounded-full align-middle" style={{ background: AGENT_HEX[a] }} />{AGENT_INFO[a]?.name ?? a}</td>
                  <td className="num px-3 py-2 text-right">{v.calls}</td>
                  <td className="num px-3 py-2 text-right">{(v.input_tokens + v.output_tokens).toLocaleString('en-IN')}</td>
                  <td className="num px-3 py-2 text-right">{usd(v.usd, 4)}</td>
                  <td className="num px-3 py-2 text-right text-ink-3">{lf.data?.agents?.[a] ? usd(lf.data.agents[a]['USD (Langfuse)'], 4) : '-'}</td>
                </tr>
              ))}
              {byAgent.length === 0 && <tr><td colSpan={5} className="px-3 py-4 text-center text-ink-3">No model calls for this run yet.</td></tr>}
            </tbody>
          </table>
          <p className="mt-2 text-[12px] text-ink-3">
            Spend ledger: {spend.data?.investigation?.calls ?? 0} calls, {usd(spend.data?.investigation?.usd ?? 0, 4)}.{' '}
            {lf.data?.error ? `Langfuse not read: ${lf.data.error}` : lf.data ? `Langfuse: ${lf.data.generations} generations, ${usd(lf.data.usd ?? 0, 4)}.` : ''}
          </p>
        </div>
        <div>
          <SectionTitle aside={blocked.data?.read_at ? <span className="text-[12px] text-ink-3">read {ago(blocked.data.read_at)}</span> : null}>Requests the sandboxes blocked</SectionTitle>
          {!blocked.data?.available ? <p className="text-[13px] text-ink-3">The sandbox policy logs are not readable where the UI runs.</p>
            : blocked.data.requests.length === 0 ? <p className="text-[13px] text-ink-3">No denied request in the last 6 hours.</p> : (
              <ul className="max-h-56 space-y-1 overflow-auto rounded-[12px] border border-rule p-2 scroll-thin">
                {blocked.data.requests.map((b, i) => <li key={i} className="id text-[11.5px] text-ink-2"><span className="font-medium text-danger">{b.agent}</span> {b.line}</li>)}
              </ul>
            )}
        </div>
      </div>
      <div>
        <SectionTitle>Handoffs between the agents</SectionTitle>
        <ul className="space-y-2">
          {detail.handoffs.map(h => (
            <li key={h.id}>
              <details className="group rounded-[10px] border border-rule bg-card">
                <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-[13px]">
                  <Badge tone="brand" dot={false}>{h.kind}</Badge>
                  <span className="text-ink-2">{AGENT_INFO[h.from_agent]?.name ?? h.from_agent} to {AGENT_INFO[h.to_agent]?.name ?? h.to_agent}</span>
                  <span className="ml-auto text-[12px] text-ink-3">{when(h.ts)}</span>
                </summary>
                <pre className="max-h-80 overflow-auto border-t border-rule bg-paper/60 p-3 text-[11.5px] leading-snug text-ink-2 scroll-thin">{JSON.stringify(h.payload, null, 1)}</pre>
              </details>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
