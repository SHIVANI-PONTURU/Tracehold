import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { AlertTriangle, Check, CircleDashed, History, Loader2, Scale, Waypoints, X } from 'lucide-react'
import { type ReactNode, useMemo, useState } from 'react'
import { Link } from 'react-router'
import { useApp } from '@/app/context'
import { DataTable } from '@/components/DataTable'
import { RingsMotif } from '@/components/Motif'
import { Badge, Button, ErrorNote, Field, Mono, Modal, Panel, Spinner, TabPanel, Tabs, Textarea, Tip, useMedia } from '@/components/ui'
import { api } from '@/lib/api'
import { cn, inr, short, when } from '@/lib/format'
import { ACTION_INFO, AGENT_INFO, BRANCH_END, FINAL, isLive, LABEL_INFO, report, STAGES, stageState, STATUS_INFO } from '@/lib/status'
import type { Detail, ProposalAccount, TraceAccount, Verdict } from '@/lib/types'
import { ActionsTab, ChatTab, EvidenceTab, MemoryTab, TraceTab } from './CaseTabs'
import { GatePanel } from './GatePanel'
import { useDetail, useSpend, useTrail } from './hooks'
import { TrailGraph, TrailLegend } from './TrailGraph'

export function Pipeline({ detail, bank }: { detail: Detail; bank: string }) {
  const s = detail.investigation.status
  return (
    <ol className="grid grid-cols-2 gap-2 md:grid-cols-4" id="tour-pipeline">
      {STAGES.map((st, i) => {
        const state = stageState(s, st)
        return (
          <li key={st.key} className={cn('relative rounded-[12px] border px-3.5 py-3 transition-colors',
            state === 'working' ? 'border-brand-300 bg-brand-50' : state === 'failed' ? 'border-danger/40 bg-danger-tint' : 'border-rule bg-card')}>
            <div className="flex items-center gap-2">
              <span className={cn('grid size-6 place-items-center rounded-full text-white',
                state === 'done' ? 'bg-brand-600' : state === 'working' ? 'bg-brand-500' : state === 'failed' ? 'bg-danger' : 'bg-rule-2')}>
                {state === 'done' ? <Check className="size-3.5" /> : state === 'working' ? <Loader2 className="size-3.5 animate-spin" /> : state === 'failed' ? <X className="size-3.5" /> : <CircleDashed className="size-3.5" />}
              </span>
              <Tip content={AGENT_INFO[st.key]?.does}><span className="cursor-help text-[13.5px] font-semibold text-ink">{st.name}</span></Tip>
              {st.key === 'tracer' && (
                <Tip content="Replay this run on the FalkorDB graph">
                  <Link to={`/visualise/graph?bank=${bank}&inv=${detail.investigation.id}`} id="tour-tracer-shortcut"
                    className="ml-auto grid size-7 place-items-center rounded-full border border-rule bg-card text-brand-700 shadow-card hover:bg-brand-50"
                    aria-label="Replay this run on the FalkorDB graph">
                    <Waypoints className="size-3.5" />
                  </Link>
                </Tip>
              )}
            </div>
            <p className="mt-1.5 text-[12px] leading-snug text-ink-3">
              <span className={cn('font-medium', state === 'working' ? 'text-brand-700' : state === 'failed' ? 'text-danger' : 'text-ink-2')}>
                {state === 'done' ? 'Done' : state === 'working' ? 'Working now' : state === 'failed' ? 'Failed' : 'Waiting'}
              </span>
              <span className="block">{AGENT_INFO[st.key]?.role}</span>
            </p>
            {i < STAGES.length - 1 && <span className="absolute -right-2 top-1/2 hidden h-px w-2 bg-rule-2 md:block" aria-hidden />}
          </li>
        )
      })}
    </ol>
  )
}

/** The case's head, laid out like a bank statement: the complaint and the disputed amount large, how much is liened,
 *  and the facts of the complaint below as label-over-value pairs. */
function StatementCard({ detail, bank, calls, onClose }: { detail: Detail; bank: string; calls: number; onClose: () => void }) {
  const { bankName } = useApp()
  const i = detail.investigation
  const complaints = useQuery({ queryKey: ['complaints', bank], queryFn: () => api.complaints(bank), enabled: !!bank, staleTime: 300000 })
  const c = complaints.data?.find(x => x.complaint_id === i.complaint_id)
  const r = report(detail)
  const disputed = r.trace?.disputed_paise ?? c?.disputed_paise ?? 0
  const liened = detail.liens.reduce((s, l) => s + (l.amount ?? 0), 0)
  const proposed = (r.proposal?.accounts ?? []).reduce((s, a) => s + (a.lien_amount_paise ?? 0), 0)
  const shown = detail.liens.length ? liened : proposed
  const share = disputed ? Math.min(1, shown / disputed) : 0
  const facts: [string, ReactNode][] = [
    ['Victim account', c ? <Mono key="victim" className="text-[13px]">{c.victim_account_id}</Mono> : '-'],
    ['Bank', bankName],
    ['Reported', c ? when(c.reported_at) : '-'],
    ['Fraudulent transfers', c ? String(c.fraud_transfers) : '-'],
    ['Case', <Mono key="case" className="text-[12.5px]">{i.id}</Mono>],
    ['Started', `${when(i.started_at)}${calls ? `, ${calls} model calls` : ''}`],
  ]
  return (
    <section className="relative isolate overflow-hidden rounded-[16px] border border-rule/70 bg-card p-5 shadow-raised">
      <RingsMotif className="right-0 top-0 hidden h-[180px] w-[220px] sm:block" />
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[12px] text-ink-3">Complaint</p>
          <div className="flex flex-wrap items-center gap-2.5">
            <h2 className="text-[24px] font-semibold tracking-tight text-ink">{i.complaint_id}</h2>
            <Badge tone={STATUS_INFO[i.status].tone} pulse={isLive(i.status)}>{STATUS_INFO[i.status].label}</Badge>
          </div>
        </div>
        <div className="flex gap-2">
          <Link to={`/visualise/graph?bank=${bank}&inv=${i.id}`}><Button size="sm" icon={<Waypoints className="size-4" />}>Replay on the graph</Button></Link>
          <Button size="sm" variant="ghost" disabled={FINAL.includes(i.status)} onClick={onClose}>Close case</Button>
        </div>
      </div>
      <div className="mt-4 grid gap-5 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div>
          <p className="text-[12.5px] text-ink-3">Disputed by the victim</p>
          <p className="text-[34px] font-semibold leading-tight tracking-tight text-ink">{disputed ? inr(disputed) : '-'}</p>
        </div>
        <div className="self-end">
          <div className="flex items-baseline justify-between gap-3 text-[12.5px]">
            <span className="text-ink-3">{detail.liens.length ? 'Liened for the victim' : 'Liens proposed'}</span>
            <span className="num font-semibold text-brand-700">{shown ? inr(shown) : 'none yet'}</span>
          </div>
          <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-paper-2" role="meter" aria-valuenow={Math.round(share * 100)} aria-valuemin={0} aria-valuemax={100} aria-label="Share of the disputed money liened">
            <div className="h-full rounded-full bg-brand-600" style={{ width: `${share * 100}%` }} />
          </div>
          <p className="num mt-1 text-[12px] text-ink-3">{Math.round(share * 100)}% of the disputed money</p>
        </div>
      </div>
      <dl className="mt-5 grid grid-cols-2 gap-x-6 gap-y-3 border-t border-rule pt-4 md:grid-cols-3 xl:grid-cols-6">
        {facts.map(([k, v]) => (
          <div key={k} className="min-w-0">
            <dt className="text-[11.5px] text-ink-3">{k}</dt>
            <dd className="mt-0.5 truncate text-[13px] font-medium text-ink">{v}</dd>
          </div>
        ))}
      </dl>
      {!!i.reclaims && <p className="mt-3 text-[12px] text-ink-3">Taken back after a worker restart {i.reclaims} time{i.reclaims > 1 ? 's' : ''}; no step ran twice.</p>}
    </section>
  )
}

function CaseLedger({ detail }: { detail: Detail }) {
  const r = report(detail)
  if (!r.trace) return null
  const liened = detail.liens.reduce((s, l) => s + (l.amount ?? 0), 0)
  const proposed = (r.proposal?.accounts ?? []).reduce((s, a) => s + (a.lien_amount_paise ?? 0), 0)
  const diff = r.trace.disputed_paise - (r.held + r.trace.unrecoverable_paise + r.trace.not_followed_paise)
  return (
    <div id="tour-ledger">
      <div className="grid gap-px overflow-hidden rounded-[14px] border border-rule/70 bg-rule shadow-card sm:grid-cols-5">
        {[
          { l: 'Disputed by the victim', v: inr(r.trace.disputed_paise), strong: true },
          { l: 'Still held on traced accounts', v: inr(r.held) },
          { l: 'Cashed out or left the bank', v: inr(r.trace.unrecoverable_paise) },
          { l: 'On branches not followed', v: inr(r.trace.not_followed_paise) },
          { l: detail.liens.length ? 'Liens placed' : 'Liens proposed', v: inr(detail.liens.length ? liened : proposed), brand: true },
        ].map(x => (
          <div key={x.l} className="bg-card px-4 py-3">
            <div className="text-[12px] text-ink-3">{x.l}</div>
            <div className={cn('num mt-0.5 text-[16px] font-semibold', x.brand ? 'text-brand-700' : 'text-ink')}>{x.v}</div>
          </div>
        ))}
      </div>
      <p className={cn('mt-2 flex items-center gap-1.5 text-[12.5px]', diff === 0 ? 'text-brand-700' : 'text-rejected')}>
        <Scale className="size-3.5" />
        {diff === 0 ? 'The books balance to the paisa: disputed = held + cashed out + not followed.'
          : `The books are off by ${inr(diff)}: check the trace.`}
      </p>
    </div>
  )
}

type Row = TraceAccount & { p?: ProposalAccount; v?: Verdict; approved?: boolean | null; decidedBy?: string | null; lienStatus?: string; seen?: boolean }

function AccountsTable({ detail, seen, selected, onSelect }: { detail: Detail; seen: Set<string>; selected: string | null; onSelect: (id: string) => void }) {
  const r = report(detail)
  const rows: Row[] = (r.trace?.accounts ?? []).map(a => {
    const p = r.props.get(a.account_id)
    const did = p?.lien_proposal_id ?? ''
    const d = r.decisions.get(did)
    return { ...a, p, v: r.verdicts.get(did), approved: d?.approved, decidedBy: d?.decided_by, lienStatus: r.liens.get(did)?.status, seen: seen.has(a.account_id) }
  })
  const cols = useMemo<ColumnDef<Row, any>[]>(() => [
    { header: 'Account', accessorKey: 'account_id', cell: c => (
      <span className="inline-flex items-center gap-1.5"><Mono>{c.getValue()}</Mono>{c.row.original.seen && <History className="size-3.5 text-brand-600" aria-label="Seen in an earlier case" />}</span>) },
    { header: 'Layer', accessorKey: 'layer', meta: { align: 'right' } },
    { header: 'Victim\'s money', accessorKey: 'tainted_paise', meta: { align: 'right' }, cell: c => <span className="font-medium text-ink">{inr(c.getValue())}</span> },
    { header: 'Balance', accessorKey: 'balance_paise', meta: { align: 'right' }, cell: c => short(c.getValue()) },
    { header: 'Share', id: 'share', accessorFn: a => (a.balance_paise ? a.tainted_paise / a.balance_paise : 0), meta: { align: 'right' },
      cell: c => `${(c.getValue() * 100).toFixed(c.getValue() < 0.01 ? 2 : 1)}%` },
    { header: 'Label', id: 'label', accessorFn: a => a.p?.label ?? '', cell: c => c.row.original.p ? <Badge tone={LABEL_INFO[c.row.original.p.label]?.tone}>{LABEL_INFO[c.row.original.p.label]?.label}</Badge> : <span className="text-ink-4">-</span> },
    { header: 'Action', id: 'action', accessorFn: a => a.p?.proposed_action ?? '', cell: c => c.row.original.p ? ACTION_INFO[c.row.original.p.proposed_action] : '-' },
    { header: 'Lien', id: 'lien', accessorFn: a => a.p?.lien_amount_paise ?? 0, meta: { align: 'right' }, cell: c => c.row.original.p?.lien_amount_paise != null ? inr(c.row.original.p.lien_amount_paise) : <span className="text-ink-4">-</span> },
    { header: 'Verifier', id: 'verifier', accessorFn: a => a.v?.verdict ?? '', cell: c => c.row.original.v ? <Badge tone={c.row.original.v.verdict === 'approve' ? 'approved' : 'rejected'} dot={false}>{c.row.original.v.verdict}</Badge> : <span className="text-ink-4">-</span> },
    { header: 'Gate', id: 'gate', accessorFn: a => (a.approved == null ? '' : a.approved ? 'approved' : 'rejected'),
      cell: c => c.row.original.approved == null ? <span className="text-ink-4">-</span> : <span className={c.row.original.approved ? 'text-approved' : 'text-rejected'}>{c.row.original.approved ? 'Approved' : 'Rejected'}{c.row.original.decidedBy === 'human' ? ' by a person' : ''}</span> },
    { header: 'Branch end', id: 'end', accessorFn: a => a.branch_end ?? '', cell: c => <span className="text-ink-3">{c.getValue() ? BRANCH_END[c.getValue()] ?? c.getValue() : 'Moved on'}</span> },
  ], [])
  return <DataTable data={rows} columns={cols} rowId={r => r.account_id} selectedId={selected} onRowClick={r => onSelect(r.account_id)}
    initialSort={[{ id: 'tainted_paise', desc: true }]} search="Search accounts" pageSize={10} />
}

export function CaseDetail({ inv }: { inv: string }) {
  const { bank } = useApp()
  const q = useDetail(bank, inv)
  const d = q.data
  const status = d?.investigation.status
  const trail = useTrail(bank, inv, status)
  const spend = useSpend(inv, !!status && isLive(status))
  const [tab, setTab] = useState('evidence')
  const [selected, setSelected] = useState<string | null>(null)
  const [closing, setClosing] = useState(false)
  const wide = useMedia('(min-width: 1536px)')
  if (q.isLoading) return <Spinner label="Loading the case" />
  if (q.error || !d) return <ErrorNote error={q.error ?? 'The case could not be read.'} />
  const i = d.investigation
  const seen = new Set((trail.data?.nodes ?? []).filter(n => n.seen_before).map(n => n.id))
  const select = (id: string) => { setSelected(id); setTab('evidence') }
  const checks = report(d).proposal?.checks?.results ?? []
  const failed = checks.filter(c => !c.passed)
  return (
    <div className="grid grid-cols-1 gap-6 2xl:grid-cols-[minmax(0,1fr)_380px]">
      <div className="min-w-0 space-y-5">
        <StatementCard detail={d} bank={bank} calls={spend.data?.investigation?.calls ?? 0} onClose={() => setClosing(true)} />
        {i.status === 'failed' && (
          <div className="flex items-start gap-2 rounded-[12px] border border-danger/30 bg-danger-tint px-4 py-3 text-[13px] text-danger">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" /><div><b className="font-semibold">Failed in the {i.failed_by ?? 'run'}.</b> {i.failure}</div>
          </div>
        )}
        <Pipeline detail={d} bank={bank} />
        <CaseLedger detail={d} />
        {!wide && <Panel raised><GatePanel detail={d} onSelectAccount={select} /></Panel>}
        <Panel title="The money trail" subtitle="Each card is an account the stolen money reached; arrows are the transfers that carried it. Click an account to read the evidence."
          bodyClass="pt-3" id="tour-trail">
          <div className="mb-3"><TrailLegend /></div>
          {trail.data?.nodes.length ? <TrailGraph trail={trail.data} selected={selected} onSelect={select} height={430} />
            : <p className="py-10 text-center text-[13px] text-ink-3">{isLive(i.status) ? 'The Tracer is following the money; the trail appears when it hands over.' : 'This run has no trail.'}</p>}
        </Panel>
        <Panel title="Traced accounts" subtitle={failed.length ? `Deterministic checks failed: ${failed.map(c => c.name).join(', ')}` : checks.length ? 'All 7 deterministic checks passed on the proposal.' : undefined} id="tour-accounts">
          <AccountsTable detail={d} seen={seen} selected={selected} onSelect={select} />
        </Panel>
        <Panel>
          <Tabs value={tab} onValueChange={setTab} tabs={[
            { value: 'evidence', label: 'Evidence' }, { value: 'actions', label: `Automatic actions (${d.actions.length})` },
            { value: 'chat', label: 'Ask about this case', id: 'tour-case-chat' }, { value: 'memory', label: 'Memory' }, { value: 'trace', label: 'Agent trace' },
          ]}>
            <TabPanel value="evidence"><EvidenceTab detail={d} account={selected} onSelect={setSelected} /></TabPanel>
            <TabPanel value="actions"><ActionsTab detail={d} /></TabPanel>
            <TabPanel value="chat"><ChatTab detail={d} /></TabPanel>
            <TabPanel value="memory"><MemoryTab detail={d} /></TabPanel>
            <TabPanel value="trace"><TraceTab detail={d} /></TabPanel>
          </Tabs>
        </Panel>
      </div>
      {wide && (
        <aside className="sticky top-[88px] self-start">
          <Panel raised><GatePanel detail={d} onSelectAccount={select} /></Panel>
        </aside>
      )}
      <CloseDialog open={closing} onOpenChange={setClosing} inv={i.id} />
    </div>
  )
}

function CloseDialog({ open, onOpenChange, inv }: { open: boolean; onOpenChange: (v: boolean) => void; inv: string }) {
  const { bank } = useApp()
  const qc = useQueryClient()
  const [reason, setReason] = useState('')
  const close = useMutation({
    mutationFn: () => api.close(bank, inv, reason.trim()),
    onSuccess: () => { onOpenChange(false); setReason(''); qc.invalidateQueries({ queryKey: ['detail', bank, inv] }); qc.invalidateQueries({ queryKey: ['investigations', bank] }) },
  })
  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Close this case" description="The agents stop working on it and no lien is placed. The case and its evidence stay in the bank's memory.">
      <Field label="Reason for closing"><Textarea value={reason} onChange={e => setReason(e.target.value)} placeholder="For example: duplicate of an earlier complaint" /></Field>
      <ErrorNote error={close.error} />
      <div className="mt-5 flex justify-end gap-2">
        <Button variant="ghost" onClick={() => onOpenChange(false)}>Keep it open</Button>
        <Button variant="danger" disabled={!reason.trim()} busy={close.isPending} onClick={() => close.mutate()}>Close case</Button>
      </div>
    </Modal>
  )
}
