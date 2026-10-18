import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Play } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { useApp } from '@/app/context'
import { Button, Empty, ErrorNote, Modal, Spinner } from '@/components/ui'
import { api } from '@/lib/api'
import { cn, inr } from '@/lib/format'

// Its own module so the Dashboard can open it without loading the Investigations page (and React Flow) up front.

export function StartDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const { bank, bankName } = useApp()
  const nav = useNavigate()
  const qc = useQueryClient()
  const complaints = useQuery({ queryKey: ['complaints', bank], queryFn: () => api.complaints(bank), enabled: open && !!bank })
  const [pick, setPick] = useState<string>('')
  const start = useMutation({
    mutationFn: (c: string) => api.start(bank, c),
    onSuccess: r => { onOpenChange(false); qc.invalidateQueries({ queryKey: ['investigations', bank] }); nav(`/investigations/${r.investigation_id}`) },
  })
  const list = complaints.data ?? []
  return (
    <Modal open={open} onOpenChange={onOpenChange} width="max-w-xl" title="Start an investigation"
      description={`Complaints reported to ${bankName}. The three agents take it from here; you decide at the gate. A run costs about 15 to 30 US cents.`}>
      {complaints.isLoading ? <Spinner /> : list.length === 0 ? <Empty title="No complaint is reported to this bank" /> : (
        <ul className="max-h-[50vh] space-y-1.5 overflow-auto pr-1 scroll-thin" role="radiogroup" aria-label="Complaint">
          {list.map(c => (
            <li key={c.complaint_id}>
              <button role="radio" aria-checked={pick === c.complaint_id} onClick={() => setPick(c.complaint_id)}
                aria-label={`${c.complaint_id}, ${inr(c.disputed_paise, { paise: false })} disputed`}
                className={cn('flex w-full items-center gap-3 rounded-[10px] border px-3.5 py-2.5 text-left transition-colors',
                  pick === c.complaint_id ? 'border-brand-500 bg-brand-50 shadow-card' : 'border-rule hover:border-brand-300 hover:bg-paper')}>
                <span className={cn('size-4 shrink-0 rounded-full border-2', pick === c.complaint_id ? 'border-brand-600 bg-brand-600 shadow-[inset_0_0_0_2px_#fff]' : 'border-rule-2')} />
                <span className="min-w-0 flex-1">
                  <span className="block text-[14px] font-semibold text-ink">{c.complaint_id}</span>
                  <span className="block text-[12.5px] text-ink-3">Victim <span className="id text-ink-2">{c.victim_account_id}</span>, reported {new Date(c.reported_at * 1000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })}</span>
                </span>
                <span className="num text-right text-[14px] font-semibold text-ink">{inr(c.disputed_paise, { paise: false })}<span className="block text-[11.5px] font-normal text-ink-3">disputed</span></span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <ErrorNote error={start.error} />
      <div className="mt-5 flex justify-end gap-2">
        <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
        <Button variant="primary" icon={<Play className="size-4" />} disabled={!pick} busy={start.isPending} onClick={() => start.mutate(pick)}>Start investigation</Button>
      </div>
    </Modal>
  )
}
