import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { CheckCircle2, ClipboardCheck, Flag } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { useApp } from '@/app/context'
import { DataTable } from '@/components/DataTable'
import { Badge, Button, Empty, ErrorNote, Field, Mono, Panel, Segmented, Textarea } from '@/components/ui'
import { api } from '@/lib/api'
import { ago } from '@/lib/format'
import type { Review } from '@/lib/types'

type Status = 'open' | 'cleared' | 'escalated' | 'all'
const TONE = { open: 'review', cleared: 'approved', escalated: 'rejected' } as const

export function ReviewsPage() {
  const { bank } = useApp()
  const qc = useQueryClient()
  const [status, setStatus] = useState<Status>('open')
  const reviews = useQuery({ queryKey: ['reviews', bank, status], queryFn: () => api.reviews(bank, status), enabled: !!bank, refetchInterval: 15000 })
  const [sel, setSel] = useState<string | null>(null)
  const [outcome, setOutcome] = useState<'cleared' | 'escalated'>('cleared')
  const [note, setNote] = useState('')
  const rows = reviews.data ?? []
  const picked = rows.find(r => r.id === sel) ?? null
  const resolve = useMutation({
    mutationFn: () => api.resolveReview(bank, picked!.id, outcome, note.trim()),
    onSuccess: () => { setNote(''); qc.invalidateQueries({ queryKey: ['reviews', bank] }); qc.invalidateQueries({ queryKey: ['memory', bank] }) },
  })
  const cols = useMemo<ColumnDef<Review, any>[]>(() => [
    { header: 'Account', accessorKey: 'account_id', cell: c => <Mono>{c.getValue()}</Mono> },
    { header: 'Case', accessorKey: 'complaint_id', cell: c => <Link className="font-medium text-brand-700 hover:underline" to={`/investigations/${c.row.original.investigation_id}`} onClick={e => e.stopPropagation()}>{c.getValue()}</Link> },
    { header: 'What the Adjudicator asks', accessorKey: 'detail', meta: { wrap: true }, cell: c => <span className="line-clamp-2 leading-snug">{c.getValue()}</span> },
    { header: 'Opened', accessorKey: 'ts', cell: c => <span className="text-ink-3">{ago(c.getValue())}</span> },
    { header: 'Status', accessorKey: 'status', cell: c => <Badge tone={TONE[c.getValue() as keyof typeof TONE] ?? 'none'}>{c.getValue()}</Badge> },
  ], [])
  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
      <Panel title="Accounts waiting for a person" subtitle="The Adjudicator sends an account here when a lien would be wrong or unsure, for example a likely-genuine account that holds only a little of the victim's money."
        actions={<Segmented size="sm" value={status} onChange={v => { setStatus(v); setSel(null) }}
          options={[{ value: 'open', label: 'Open' }, { value: 'cleared', label: 'Cleared' }, { value: 'escalated', label: 'Escalated' }, { value: 'all', label: 'All' }]} />}>
        <ErrorNote error={reviews.error} />
        <DataTable data={rows} columns={cols} rowId={r => r.id} selectedId={sel} onRowClick={r => setSel(r.id)} search="Search reviews"
          empty={status === 'open' ? 'No open review at this bank.' : 'Nothing here.'} initialSort={[{ id: 'ts', desc: true }]} />
      </Panel>
      <aside className="xl:sticky xl:top-[88px] xl:self-start">
        <Panel raised title="Resolve a review" subtitle="Your outcome scores the lessons on this account: cleared counts like a rejected lien, escalated like an approved one.">
          {!picked ? <Empty title="Pick a review" icon={<ClipboardCheck className="size-6" />}>Select a row to read the Adjudicator's question and resolve it.</Empty> : (
            <div className="space-y-4">
              <div className="rounded-[10px] bg-paper/70 px-3.5 py-3">
                <div className="flex items-center justify-between gap-2"><Mono className="font-medium">{picked.account_id}</Mono><Badge tone={TONE[picked.status as keyof typeof TONE] ?? 'none'}>{picked.status}</Badge></div>
                <p className="mt-1.5 text-[13px] leading-relaxed text-ink-2">{picked.detail}</p>
                <p className="mt-1.5 text-[12px] text-ink-3"><span className="id">{picked.id}</span>, case <Link className="text-brand-700 hover:underline" to={`/investigations/${picked.investigation_id}`}>{picked.complaint_id}</Link></p>
              </div>
              {picked.status === 'open' ? (
                <>
                  <Segmented value={outcome} onChange={setOutcome} className="w-full"
                    options={[{ value: 'cleared', label: <span className="inline-flex items-center gap-1.5"><CheckCircle2 className="size-3.5" />Cleared, genuine</span> },
                      { value: 'escalated', label: <span className="inline-flex items-center gap-1.5"><Flag className="size-3.5" />Escalate</span> }]} />
                  <Field label="Note" hint="Say what you checked, for example the documents the holder sent."><Textarea value={note} onChange={e => setNote(e.target.value)} /></Field>
                  <ErrorNote error={resolve.error} />
                  <Button variant="primary" className="w-full" disabled={!note.trim()} busy={resolve.isPending} onClick={() => resolve.mutate()}>
                    {outcome === 'cleared' ? 'Clear this account' : 'Escalate this account'}
                  </Button>
                  {resolve.data && (
                    <p className="rounded-[10px] bg-approved-tint px-3 py-2 text-[12.5px] text-approved">
                      Resolved. {resolve.data.lessons?.length ? `${resolve.data.lessons.length} lesson${resolve.data.lessons.length > 1 ? 's' : ''} rescored; see Bank memory.` : 'No lesson applied to this account.'}
                    </p>
                  )}
                </>
              ) : <p className="text-[13px] text-ink-3">This review is already {picked.status}.</p>}
            </div>
          )}
        </Panel>
      </aside>
    </div>
  )
}
