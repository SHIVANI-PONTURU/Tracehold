import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Gavel, RotateCcw, ShieldCheck } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useApp } from '@/app/context'
import { Badge, Button, ErrorNote, Input, Segmented } from '@/components/ui'
import { api } from '@/lib/api'
import { cn, inr } from '@/lib/format'
import { report } from '@/lib/status'
import type { Detail } from '@/lib/types'

type Choice = { override: boolean; approve: boolean; reason: string }

export function GatePanel({ detail, onSelectAccount }: { detail: Detail; onSelectAccount?: (id: string) => void }) {
  const { bank, config } = useApp()
  const qc = useQueryClient()
  const inv = detail.investigation
  const r = useMemo(() => report(detail), [detail])
  const atGate = inv.status === 'at_gate'
  const gate = useQuery({ queryKey: ['gate', bank, inv.id], queryFn: () => api.gate(bank, inv.id), enabled: atGate, refetchInterval: 4000 })
  const [mode, setMode] = useState<'placeholder' | 'confirm'>(config?.gate_mode ?? 'placeholder')
  const [choices, setChoices] = useState<Record<string, Choice>>({})
  const verdicts = [...r.verdicts.values()]
  const decide = useMutation({
    mutationFn: (decisions: { decision_id: string; approve: boolean; reason: string }[]) => api.gateDecide(bank, inv.id, decisions, mode),
    onSuccess: () => {
      setChoices({})
      qc.invalidateQueries({ queryKey: ['gate', bank, inv.id] })
      qc.invalidateQueries({ queryKey: ['detail', bank, inv.id] })
      qc.invalidateQueries({ queryKey: ['investigations', bank] })
    },
  })
  const set = (id: string, patch: Partial<Choice>, v: { verdict: string }) =>
    setChoices(c => {
      const base: Choice = c[id] ?? { override: false, approve: v.verdict === 'approve', reason: '' }
      return { ...c, [id]: { ...base, ...patch } }
    })

  if (!atGate) {
    const decided = detail.decisions.filter(d => d.approved !== null && d.approved !== undefined)
    return (
      <div id="tour-gate">
        <GateHeader />
        {decided.length ? (
          <ul className="mt-3 space-y-2">
            {decided.map(d => (
              <li key={d.id} className="rounded-[10px] border border-rule bg-paper/50 px-3 py-2.5">
                <div className="flex items-center justify-between gap-2">
                  <button className="id truncate text-ink hover:underline" onClick={() => onSelectAccount?.(d.account_id)}>{d.account_id}</button>
                  <Badge tone={d.approved ? 'approved' : 'rejected'}>{d.approved ? 'Approved' : 'Rejected'}</Badge>
                </div>
                <div className="num mt-1 text-[13px] font-medium text-ink">{inr(d.lien_amount)}</div>
                <p className="mt-1 text-[12.5px] leading-snug text-ink-3">
                  {d.decided_by === 'human' ? 'Decided by a person' : 'The Verifier\'s verdict stood'}{d.gate_reason ? `: ${d.gate_reason}` : ''}
                </p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-3 text-[13px] text-ink-3">
            {['executed', 'closed', 'failed'].includes(inv.status)
              ? 'This case finished without a lien decision at the gate.'
              : 'The gate opens when the Verifier has given its verdict on every proposed lien. Nothing reaches the bank before a person has seen it.'}
          </p>
        )}
        {detail.liens.length > 0 && (
          <div className="mt-4 rounded-[10px] bg-approved-tint px-3 py-2.5 text-[13px] text-approved">
            <CheckCircle2 className="mr-1.5 inline size-4 align-[-3px]" />
            {detail.liens.length} lien request{detail.liens.length > 1 ? 's' : ''} sent to the bank, {inr(detail.liens.reduce((s, l) => s + l.amount, 0))} in all.
          </div>
        )}
      </div>
    )
  }

  if (gate.data?.decisions != null) {
    return (
      <div id="tour-gate">
        <GateHeader />
        <div className="mt-3 rounded-[10px] bg-proposed-tint px-3 py-3 text-[13px] text-proposed">
          Decision received. The Verifier is placing the approved liens; this case updates in a few seconds.
        </div>
      </div>
    )
  }

  const overrides = verdicts.filter(v => choices[v.decision_id]?.override)
  const send = (decisions: { decision_id: string; approve: boolean; reason: string }[]) => decide.mutate(decisions)
  return (
    <div id="tour-gate">
      <GateHeader />
      <div className="mt-3 flex items-center justify-between gap-3">
        <Segmented size="sm" value={mode} onChange={setMode} options={[{ value: 'placeholder', label: 'Verifier stands' }, { value: 'confirm', label: 'I decide each' }]} />
      </div>
      <p className="mt-2 text-[12.5px] leading-snug text-ink-3">
        {mode === 'placeholder' ? 'The Verifier\'s verdict stands unless you override it. A reason you give becomes a lesson for this bank.'
          : 'You approve or reject every lien yourself. A reason that rejects or overturns a lien becomes a lesson.'}
      </p>
      {verdicts.length === 0 && (
        <div className="mt-3 rounded-[10px] border border-rule bg-paper/50 px-3 py-2.5 text-[13px] text-ink-2">
          No lien was proposed, so there is nothing to approve. Approving closes the case; reviews and document requests stay open.
        </div>
      )}
      <ul className="mt-3 space-y-2.5">
        {verdicts.map(v => {
          const d = r.decisions.get(v.decision_id)
          const c = choices[v.decision_id]
          const approveNow = mode === 'confirm' ? (c?.approve ?? v.verdict === 'approve') : c?.override ? v.verdict !== 'approve' : v.verdict === 'approve'
          return (
            <li key={v.decision_id} className={cn('rounded-[11px] border px-3 py-3 transition-colors', c?.override ? 'border-review/50 bg-review-tint/40' : 'border-rule bg-card')}>
              <div className="flex items-center justify-between gap-2">
                <button className="id truncate text-ink hover:underline" onClick={() => onSelectAccount?.(d?.account_id ?? '')}>{d?.account_id}</button>
                <Badge tone={v.verdict === 'approve' ? 'approved' : 'rejected'}>Verifier: {v.verdict === 'approve' ? 'approve' : 'reject'}</Badge>
              </div>
              <div className="num mt-1 text-[15px] font-semibold text-ink">{inr(d?.lien_amount)}</div>
              <p className="mt-1 text-[12.5px] leading-snug text-ink-2">{v.reason}</p>
              {v.lessons_cited?.length > 0 && <p className="mt-1 text-[12px] text-ink-3">Lessons cited: <span className="id">{v.lessons_cited.join(', ')}</span></p>}
              <div className="mt-2.5 flex flex-wrap items-center gap-2">
                {mode === 'placeholder' ? (
                  <label className="inline-flex cursor-pointer items-center gap-2 text-[12.5px] font-medium text-ink-2">
                    <input type="checkbox" checked={!!c?.override} onChange={e => set(v.decision_id, { override: e.target.checked }, v)}
                      className="size-4 rounded border-rule-2 accent-[var(--color-brand-600)]" />
                    Override ({v.verdict === 'approve' ? 'reject' : 'approve'} instead)
                  </label>
                ) : (
                  <Segmented size="sm" value={approveNow ? 'approve' : 'reject'} onChange={x => set(v.decision_id, { approve: x === 'approve' }, v)}
                    options={[{ value: 'approve', label: 'Approve' }, { value: 'reject', label: 'Reject' }]} />
                )}
                <span className={cn('ml-auto text-[12px] font-medium', approveNow ? 'text-approved' : 'text-rejected')}>{approveNow ? 'Will be placed' : 'Will not be placed'}</span>
              </div>
              {(mode === 'confirm' || c?.override) && (
                <Input className="mt-2 h-8 text-[13px]" placeholder={mode === 'confirm' ? 'Reason (a lesson when it rejects or overturns)' : 'Why you override (becomes a lesson)'}
                  value={c?.reason ?? ''} onChange={e => set(v.decision_id, { reason: e.target.value }, v)} />
              )}
            </li>
          )
        })}
      </ul>
      <ErrorNote error={decide.error} />
      <div className="mt-4 flex flex-col gap-2" id="tour-gate-buttons">
        {mode === 'placeholder' ? (
          <>
            <Button variant="primary" icon={<ShieldCheck className="size-4" />} busy={decide.isPending && overrides.length === 0}
              disabled={overrides.length > 0} onClick={() => send([])}>
              {verdicts.length ? 'Approve the Verifier\'s verdicts' : 'Approve and close'}
            </Button>
            <Button icon={<RotateCcw className="size-4" />} disabled={overrides.length === 0} busy={decide.isPending && overrides.length > 0}
              onClick={() => send(overrides.map(v => ({ decision_id: v.decision_id, approve: v.verdict !== 'approve',
                reason: choices[v.decision_id]?.reason.trim() || 'Overridden at the gate.' })))}>
              Send {overrides.length || ''} override{overrides.length === 1 ? '' : 's'}
            </Button>
          </>
        ) : (
          <Button variant="primary" icon={<Gavel className="size-4" />} busy={decide.isPending}
            onClick={() => send(verdicts.map(v => {
              const c = choices[v.decision_id]
              const approve = c?.approve ?? v.verdict === 'approve'
              return { decision_id: v.decision_id, approve,
                reason: c?.reason.trim() || (approve === (v.verdict === 'approve') ? 'Confirmed at the gate.' : '') }
            }))}>
            Submit my decisions
          </Button>
        )}
      </div>
    </div>
  )
}

function GateHeader() {
  return (
    <div className="flex items-center gap-2">
      <span className="grid size-8 place-items-center rounded-full bg-brand-50 text-brand-700"><Gavel className="size-4" /></span>
      <div>
        <h3 className="text-[15px] font-semibold text-ink">HITL gate</h3>
        <p className="text-[12px] text-ink-3">A person decides before any lien reaches the bank</p>
      </div>
    </div>
  )
}
