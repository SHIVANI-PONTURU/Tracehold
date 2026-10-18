import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, LifeBuoy, Send, Sparkles, X } from 'lucide-react'
import { type ReactNode, useEffect, useRef, useState } from 'react'
import { Link, matchPath, useLocation } from 'react-router'
import { useApp } from '@/app/context'
import { api, errorText } from '@/lib/api'
import { cn, inr } from '@/lib/format'
import { ACTION_INFO, LABEL_INFO, report } from '@/lib/status'
import type { Detail } from '@/lib/types'
import { GUIDE, SUGGESTIONS } from './guide'
import { makeMatcher } from './match'

type Msg = { id: number; who: 'me' | 'bot'; text: ReactNode; link?: { to: string; label: string }; ask?: string }

const ACCOUNT_RE = /\b\d{1,6}-[0-9A-F]{9}\b/i
const GREETING_RE = /^\s*(hi|hello|hey|thanks|thank you|thx|cheers|good (morning|afternoon|evening))\b[\s!.]*$/i
const matcher = makeMatcher(GUIDE)

function caseAnswer(q: string, d: Detail): ReactNode | null {
  const r = report(d)
  const m = q.match(ACCOUNT_RE)
  if (m) {
    const id = m[0].toUpperCase()
    const p = r.props.get(id)
    const t = r.trace?.accounts.find(a => a.account_id === id)
    if (!p && !t) return <>Account <span className="id">{id}</span> is not on this case's trail.</>
    const v = p?.lien_proposal_id ? r.verdicts.get(p.lien_proposal_id) : undefined
    const dec = p?.lien_proposal_id ? r.decisions.get(p.lien_proposal_id) : undefined
    return (
      <div className="space-y-1.5">
        <p><span className="id font-medium">{id}</span>{t ? ` holds ${inr(t.tainted_paise)} of the victim's money (layer ${t.layer}).` : '.'}</p>
        {p && <p>The Adjudicator labelled it <b className="font-semibold">{LABEL_INFO[p.label]?.label.toLowerCase()}</b> and proposed: {ACTION_INFO[p.proposed_action].toLowerCase()}{p.lien_amount_paise != null ? ` of ${inr(p.lien_amount_paise)}` : ''}.</p>}
        {p && <p className="text-ink-2">Why: {p.rationale}</p>}
        {v && <p>The Verifier said <b className="font-semibold">{v.verdict}</b>: {v.reason}</p>}
        {dec?.approved != null && <p>At the gate the lien was {dec.approved ? 'approved' : 'rejected'} by {dec.decided_by === 'human' ? 'a person' : 'the Verifier\'s standing verdict'}.</p>}
      </div>
    )
  }
  if (/\b(this case|the case|summar\w*|what happened|outcomes?)\b/i.test(q)) {
    const liens = (r.proposal?.accounts ?? []).filter(a => a.proposed_action === 'lien')
    const reviews = (r.proposal?.accounts ?? []).filter(a => a.proposed_action.startsWith('review'))
    return (
      <div className="space-y-1.5">
        <p><b className="font-semibold">{d.investigation.complaint_id}</b>: {r.trace ? `${inr(r.trace.disputed_paise)} disputed; ${inr(r.held)} still held on ${r.trace.accounts.length} traced accounts, ${inr(r.trace.unrecoverable_paise)} cashed out.` : 'the Tracer is still following the money.'}</p>
        {r.proposal && <p>{liens.length} lien{liens.length === 1 ? '' : 's'} proposed ({inr(liens.reduce((s, a) => s + (a.lien_amount_paise ?? 0), 0))}), {reviews.length} account{reviews.length === 1 ? '' : 's'} sent to review.</p>}
        {d.liens.length > 0 && <p>{d.liens.length} lien request{d.liens.length > 1 ? 's' : ''} sent to the bank.</p>}
        {d.investigation.status === 'at_gate' && <p>It is waiting at the gate for your decision.</p>}
      </div>
    )
  }
  return null
}

export function HelpChat() {
  const { bank } = useApp()
  const loc = useLocation()
  const qc = useQueryClient()
  const inv = matchPath('/investigations/:inv', loc.pathname)?.params.inv
  const detail = useQuery({ queryKey: ['detail', bank, inv], queryFn: () => api.detail(bank, inv!), enabled: !!inv && !!bank, staleTime: 5000 })
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [msgs, setMsgs] = useState<Msg[]>([{ id: 0, who: 'bot', text: 'Hello. Ask me about this app, about mule accounts, or about a decision in the case you have open.' }])
  const [waiting, setWaiting] = useState<string | null>(null)
  const seq = useRef(1)
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => { end.current?.scrollIntoView({ block: 'end' }) }, [msgs, open])
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])
  const push = (m: Omit<Msg, 'id'>) => setMsgs(x => [...x, { ...m, id: seq.current++ }])

  // A question handed to the Verifier: show its answer here when it comes back.
  const chat = useQuery({ queryKey: ['chat', bank, inv], queryFn: () => api.chat(bank, inv!), enabled: !!waiting && !!inv, refetchInterval: waiting ? 2500 : false })
  useEffect(() => {
    if (!waiting || !chat.data) return
    const ans = chat.data.find(m => m.reply_to === waiting && m.role !== 'human')
    if (ans) {
      push({ who: 'bot', text: <div><p className="mb-1 text-[11.5px] font-medium text-brand-700">The Verifier answered{ans.status === 'rejected' ? ' (rejected by the ID check)' : ''}:</p><p>{ans.text}</p></div> })
      setWaiting(null)
    }
  }, [chat.data, waiting])

  function answer(q: string) {
    push({ who: 'me', text: q })
    const d = detail.data
    const fromCase = d ? caseAnswer(q, d) : null
    if (fromCase) { push({ who: 'bot', text: fromCase }); return }
    if (GREETING_RE.test(q)) {
      push({ who: 'bot', text: /^\s*(thank|thx|cheers)/i.test(q) ? 'You are welcome. Ask again any time.' : 'Hello. Ask me about the app, mule accounts or the agents, or open a case and ask about an account in it.' })
      return
    }
    const e = matcher.best(q)
    // With a case open the guide's general answer may not be what was meant, so the Verifier stays one click away.
    if (e) { push({ who: 'bot', text: e.a, link: e.link, ask: inv ? q : undefined }); return }
    if (!inv && ACCOUNT_RE.test(q)) {
      push({ who: 'bot', text: 'I answer account questions from the open case. Open the case that traced this account, then ask again.', link: { to: '/investigations', label: 'Open Investigations' } })
      return
    }
    if (inv) push({ who: 'bot', text: 'I do not have that in my guide. I can pass the question to the Verifier, which answers from this case\'s record and cites evidence IDs (one model call, about 2 US cents).', ask: q })
    else push({ who: 'bot', text: 'I do not have that in my guide yet. Try one of the questions below, or open a case and ask about an account in it.' })
  }

  async function askVerifier(q: string) {
    if (!inv) return
    try {
      const r = await api.ask(bank, inv, q)
      setWaiting(r.message_id)
      qc.invalidateQueries({ queryKey: ['chat', bank, inv] })
      push({ who: 'bot', text: 'Sent to the Verifier. Its answer appears here and in the case\'s "Ask about this case" tab, usually within 15 seconds.' })
    } catch (e) { push({ who: 'bot', text: `The question was not sent: ${errorText(e)}` }) }
  }

  return (
    <>
      <button id="help-chat-button" onClick={() => setOpen(o => !o)} aria-expanded={open} aria-label={open ? 'Close help' : 'Open help'}
        className={cn('fixed bottom-5 right-5 z-40 grid size-14 place-items-center rounded-full text-white shadow-float transition-transform hover:scale-105',
          open ? 'bg-ink' : 'bg-brand-600')}>
        {open ? <X className="size-6" /> : <LifeBuoy className="size-6" />}
      </button>
      {open && (
        <section role="dialog" aria-label="TraceHold help"
          className="fixed bottom-24 right-5 z-40 flex h-[min(600px,calc(100vh-140px))] w-[min(400px,calc(100vw-40px))] flex-col overflow-hidden rounded-[16px] border border-rule bg-card shadow-float">
          <header className="flex items-center gap-3 border-b border-rule bg-brand-50 px-4 py-3">
            <span className="grid size-9 place-items-center rounded-full bg-brand-600 text-white"><Bot className="size-5" /></span>
            <div className="min-w-0">
              <h2 className="text-[14.5px] font-semibold text-ink">TraceHold help</h2>
              <p className="truncate text-[12px] text-ink-3">{inv ? `Answers about the app and the open case, ${detail.data?.investigation.complaint_id ?? inv}` : 'Answers about the app and mule accounts'}</p>
            </div>
          </header>
          <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4 scroll-thin">
            {msgs.map(m => (
              <div key={m.id} className={cn('flex', m.who === 'me' && 'justify-end')}>
                <div className={cn('max-w-[88%] rounded-[12px] px-3 py-2 text-[13px] leading-relaxed', m.who === 'me' ? 'bg-brand-600 text-white' : 'bg-paper text-ink-2')}>
                  {m.text}
                  {m.link && <Link to={m.link.to} onClick={() => setOpen(false)} className="mt-1.5 block text-[12.5px] font-medium text-brand-700 hover:underline">{m.link.label}</Link>}
                  {m.ask && (
                    <button onClick={() => askVerifier(m.ask!)} disabled={!!waiting}
                      className="mt-2 inline-flex items-center gap-1.5 rounded-[8px] border border-brand-300 bg-card px-2.5 py-1 text-[12.5px] font-medium text-brand-800 hover:bg-brand-50 disabled:opacity-50">
                      <Sparkles className="size-3.5" />Ask the Verifier about this case
                    </button>
                  )}
                </div>
              </div>
            ))}
            {waiting && <p className="text-[12px] text-ink-3">Waiting for the Verifier...</p>}
            <div ref={end} />
          </div>
          <div className="border-t border-rule px-3 pb-3 pt-2">
            <div className="mb-2 flex gap-1.5 overflow-x-auto pb-1 scroll-thin">
              {(inv ? ['What happened in this case?', ...SUGGESTIONS.slice(0, 3)] : SUGGESTIONS).map(s => (
                <button key={s} onClick={() => answer(s)} className="shrink-0 rounded-full border border-rule bg-card px-2.5 py-1 text-[12px] text-ink-2 hover:border-brand-300 hover:bg-brand-50">{s}</button>
              ))}
            </div>
            <form className="flex gap-2" onSubmit={e => { e.preventDefault(); const q = text.trim(); if (q) { answer(q); setText('') } }}>
              <input value={text} onChange={e => setText(e.target.value)} placeholder={inv ? 'Ask, or paste an account ID' : 'Ask a question'} aria-label="Your question"
                className="h-9 flex-1 rounded-[var(--radius-control)] border border-rule-2 px-3 text-[13px] placeholder:text-ink-4 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200" />
              <button className="grid size-9 place-items-center rounded-[var(--radius-control)] bg-brand-600 text-white hover:bg-brand-700 disabled:bg-brand-300" disabled={!text.trim()} aria-label="Send"><Send className="size-4" /></button>
            </form>
          </div>
        </section>
      )}
    </>
  )
}
