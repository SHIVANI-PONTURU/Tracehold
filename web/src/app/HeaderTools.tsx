import * as PopoverP from '@radix-ui/react-popover'
import { useQueries, useQuery } from '@tanstack/react-query'
import { ArrowRight, Bell, Search } from 'lucide-react'
import { type KeyboardEvent, useEffect, useId, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { Badge } from '@/components/ui'
import { api } from '@/lib/api'
import { ago, cn } from '@/lib/format'
import { STATUS_INFO } from '@/lib/status'
import type { Investigation } from '@/lib/types'
import { useApp } from './context'

type Hit = Investigation & { bank: string; bankName: string }

/** Every case at every bank the UI serves (the same per-bank queries the pages use, so the cache is shared). */
function useAllCases(): Hit[] {
  const { banks } = useApp()
  const qs = useQueries({ queries: banks.map(b => ({ queryKey: ['investigations', b.bank_id], queryFn: () => api.investigations(b.bank_id), staleTime: 15000 })) })
  return banks.flatMap((b, i) => (qs[i]?.data ?? []).map(inv => ({ ...inv, bank: b.bank_id, bankName: b.name })))
}

function match(all: Hit[], q: string) {
  const s = q.trim().toUpperCase()
  if (!s) return []
  const digits = s.replace(/\D/g, '')
  return all.filter(c => c.complaint_id.toUpperCase().includes(s) || c.id.toUpperCase().includes(s)
    || (digits.length >= 2 && c.complaint_id.replace(/\D/g, '').includes(digits)))
    .sort((a, b) => b.started_at - a.started_at).slice(0, 8)
}

/** Find a complaint or case at any bank and jump to it. "/" or Ctrl+K focuses it. */
export function CaseSearch({ autoFocus, onDone, className }: { autoFocus?: boolean; onDone?: () => void; className?: string }) {
  const { bank, setBank } = useApp()
  const nav = useNavigate()
  const all = useAllCases()
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const [at, setAt] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const list = useId()
  const hits = match(all, q)
  useEffect(() => {
    if (autoFocus) return
    const onKey = (e: globalThis.KeyboardEvent) => {
      const typing = e.target instanceof HTMLElement && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)
      if ((e.key === '/' && !typing) || (e.key.toLowerCase() === 'k' && (e.ctrlKey || e.metaKey))) { e.preventDefault(); input.current?.focus() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [autoFocus])
  const go = (h: Hit) => {
    if (h.bank !== bank) setBank(h.bank)
    nav(`/investigations/${h.id}`)
    setQ(''); setOpen(false); input.current?.blur(); onDone?.()
  }
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setAt(i => Math.min(hits.length - 1, i + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setAt(i => Math.max(0, i - 1)) }
    else if (e.key === 'Enter' && hits[at]) { e.preventDefault(); go(hits[at]) }
    else if (e.key === 'Escape') { setQ(''); setOpen(false); input.current?.blur(); onDone?.() }
  }
  const show = open && q.trim().length > 0
  return (
    <div className={cn('relative', className)}>
      <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-ink-4" aria-hidden />
      <input ref={input} value={q} autoFocus={autoFocus} placeholder="Find a complaint or case"
        role="combobox" aria-expanded={show} aria-controls={list} aria-autocomplete="list" aria-label="Find a complaint or case"
        aria-activedescendant={show && hits[at] ? `${list}-${at}` : undefined}
        onChange={e => { setQ(e.target.value); setAt(0); setOpen(true) }} onFocus={() => setOpen(true)} onBlur={() => window.setTimeout(() => setOpen(false), 150)} onKeyDown={onKey}
        className="h-9 w-full rounded-full border border-rule-2 bg-card pl-8 pr-9 text-[13px] text-ink shadow-card placeholder:text-ink-4 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200" />
      {!autoFocus && <kbd className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 rounded border border-rule bg-paper px-1.5 font-mono text-[11px] text-ink-3">/</kbd>}
      {show && (
        <ul id={list} role="listbox" className="absolute left-0 right-0 top-[calc(100%+6px)] z-50 max-h-[360px] overflow-y-auto rounded-[12px] border border-rule bg-card p-1.5 shadow-float scroll-thin">
          {hits.length === 0 && <li className="px-3 py-2.5 text-[12.5px] text-ink-3">No case matches. Complaint IDs look like CMP-0011.</li>}
          {hits.map((h, i) => (
            <li key={`${h.bank}-${h.id}`} id={`${list}-${i}`} role="option" aria-selected={i === at}
              onMouseDown={e => { e.preventDefault(); go(h) }} onMouseEnter={() => setAt(i)}
              className={cn('flex cursor-pointer items-center gap-3 rounded-[8px] px-2.5 py-2', i === at ? 'bg-brand-50' : '')}>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2"><span className="text-[13px] font-semibold text-ink">{h.complaint_id}</span><Badge tone={STATUS_INFO[h.status].tone} className="text-[11px]">{STATUS_INFO[h.status].label}</Badge></div>
                <div className="truncate text-[11.5px] text-ink-3"><span className="id text-[11px]">{h.id}</span>, {h.bankName}, {ago(h.started_at)}</div>
              </div>
              <ArrowRight className="size-3.5 shrink-0 text-ink-4" aria-hidden />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** Below 1280 px the search sits behind an icon button. */
export function SearchButton() {
  const [open, setOpen] = useState(false)
  return (
    <PopoverP.Root open={open} onOpenChange={setOpen}>
      <PopoverP.Trigger aria-label="Find a complaint or case"
        className="grid size-9 shrink-0 place-items-center rounded-full border border-rule-2 bg-card text-ink-2 shadow-card hover:border-brand-300 hover:bg-brand-50 hover:text-ink">
        <Search className="size-4" />
      </PopoverP.Trigger>
      <PopoverP.Portal>
        <PopoverP.Content align="end" sideOffset={8} className="z-50 w-[min(340px,calc(100vw-24px))] rounded-[14px] border border-rule bg-card p-2 shadow-float">
          <CaseSearch autoFocus onDone={() => setOpen(false)} />
        </PopoverP.Content>
      </PopoverP.Portal>
    </PopoverP.Root>
  )
}

/** What is waiting for a person at this bank: cases at the gate and open reviews. */
export function NeedsYouBell() {
  const { bank, bankName } = useApp()
  const [open, setOpen] = useState(false)
  const invs = useQuery({ queryKey: ['investigations', bank], queryFn: () => api.investigations(bank), enabled: !!bank, refetchInterval: 15000 })
  const reviews = useQuery({ queryKey: ['reviews', bank, 'open'], queryFn: () => api.reviews(bank, 'open'), enabled: !!bank, refetchInterval: 30000 })
  const gate = (invs.data ?? []).filter(i => i.status === 'at_gate')
  const rev = reviews.data ?? []
  const n = gate.length + rev.length
  const close = () => setOpen(false)
  return (
    <PopoverP.Root open={open} onOpenChange={setOpen}>
      <PopoverP.Trigger aria-label={n ? `${n} item${n === 1 ? '' : 's'} need you` : 'Nothing needs you'}
        className="relative grid size-9 shrink-0 place-items-center rounded-full border border-rule-2 bg-card text-ink-2 shadow-card hover:border-brand-300 hover:bg-brand-50 hover:text-ink">
        <Bell className="size-4" />
        {n > 0 && <span className="num absolute -right-1 -top-1 grid h-[18px] min-w-[18px] place-items-center rounded-full bg-brand-600 px-1 text-[10.5px] font-semibold leading-none text-white ring-2 ring-paper">{n > 99 ? '99+' : n}</span>}
      </PopoverP.Trigger>
      <PopoverP.Portal>
        <PopoverP.Content align="end" sideOffset={8} className="z-50 w-[min(340px,calc(100vw-24px))] rounded-[14px] border border-rule bg-card shadow-float">
          <div className="border-b border-rule px-4 py-3">
            <p className="text-[14px] font-semibold text-ink">Needs you</p>
            <p className="text-[12px] text-ink-3">At {bankName}: {gate.length} at the gate, {rev.length} review{rev.length === 1 ? '' : 's'}</p>
          </div>
          <ul className="max-h-[340px] space-y-1 overflow-y-auto p-2 scroll-thin">
            {gate.map(i => (
              <li key={i.id}>
                <Link to={`/investigations/${i.id}`} onClick={close} className="flex items-center gap-3 rounded-[10px] px-2.5 py-2 hover:bg-review-tint/60">
                  <span className="size-2 shrink-0 rounded-full bg-review" aria-hidden />
                  <span className="min-w-0 flex-1"><span className="block text-[13px] font-semibold text-ink">{i.complaint_id} is at the gate</span><span className="block truncate text-[11.5px] text-ink-3">Approve or override the Verifier, {ago(i.updated_at)}</span></span>
                  <ArrowRight className="size-3.5 shrink-0 text-ink-4" aria-hidden />
                </Link>
              </li>
            ))}
            {rev.slice(0, 6).map(r => (
              <li key={r.id}>
                <Link to="/reviews" onClick={close} className="flex items-center gap-3 rounded-[10px] px-2.5 py-2 hover:bg-paper">
                  <span className="size-2 shrink-0 rounded-full bg-proposed" aria-hidden />
                  <span className="min-w-0 flex-1"><span className="block text-[13px] font-medium text-ink">Review <span className="id">{r.account_id}</span></span><span className="block truncate text-[11.5px] text-ink-3">{r.detail}</span></span>
                  <ArrowRight className="size-3.5 shrink-0 text-ink-4" aria-hidden />
                </Link>
              </li>
            ))}
            {rev.length > 6 && <li><Link to="/reviews" onClick={close} className="block px-2.5 py-1.5 text-[12.5px] font-medium text-brand-700 hover:underline">All {rev.length} open reviews</Link></li>}
            {n === 0 && <li className="px-2.5 py-3 text-[13px] text-ink-3">Nothing is waiting for you at this bank.</li>}
          </ul>
        </PopoverP.Content>
      </PopoverP.Portal>
    </PopoverP.Root>
  )
}
