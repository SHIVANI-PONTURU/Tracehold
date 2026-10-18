import * as DialogP from '@radix-ui/react-dialog'
import { useQuery } from '@tanstack/react-query'
import {
  ArrowLeft, BookOpenCheck, ChevronRight, ClipboardCheck, Coins, Compass, Database, Landmark, LayoutDashboard, Menu, Network,
  PanelLeftClose, PanelLeftOpen, Route as RouteIcon, ShieldCheck, Sparkles, Waypoints, X,
} from 'lucide-react'
import { type ReactNode, useEffect, useRef, useState } from 'react'
import { Link, matchPath, NavLink, Outlet, useLocation, useNavigate } from 'react-router'
import { HelpChat } from '@/features/help/HelpChat'
import { setTourNavigate, startTour } from '@/features/help/tour'
import { api } from '@/lib/api'
import { cn } from '@/lib/format'
import { AGENT_INFO, STAGES, STATUS_INFO, stageState } from '@/lib/status'
import { Badge, Button, Tip, useMedia } from '@/components/ui'
import { useApp } from './context'
import { CaseSearch, NeedsYouBell, SearchButton } from './HeaderTools'

type Count = 'gate' | 'reviews'
type NavItem = { to: string; label: string; icon: ReactNode; id: string; count?: Count; sub?: NavItem[] }

const NAV: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: <LayoutDashboard className="size-[18px]" />, id: 'nav-dashboard' },
  { to: '/investigations', label: 'Investigations', icon: <RouteIcon className="size-[18px]" />, id: 'nav-investigations', count: 'gate' },
  { to: '/reviews', label: 'Review queue', icon: <ClipboardCheck className="size-[18px]" />, id: 'nav-reviews', count: 'reviews' },
  { to: '/memory', label: 'Bank memory', icon: <BookOpenCheck className="size-[18px]" />, id: 'nav-memory' },
  {
    to: '/visualise', label: 'Visualisation', icon: <Network className="size-[18px]" />, id: 'nav-visualise', sub: [
      { to: '/visualise/graph', label: 'FalkorDB graph', icon: <Waypoints className="size-4" />, id: 'nav-vis-graph' },
      { to: '/visualise/cost', label: 'Model cost', icon: <Coins className="size-4" />, id: 'nav-vis-cost' },
      { to: '/visualise/finance', label: 'Finance and analysis', icon: <Landmark className="size-4" />, id: 'nav-vis-finance' },
    ],
  },
]

const TITLES: [RegExp, string, string][] = [
  [/^\/$/, 'Dashboard', 'The recovery desk at a glance'],
  [/^\/investigations/, 'Investigations', 'Trace stolen money through mule accounts and lien only the victim\'s money'],
  [/^\/reviews/, 'Review queue', 'Accounts the agents sent to a person instead of a lien'],
  [/^\/memory/, 'Bank memory', 'What this bank has learned from earlier decisions'],
  [/^\/visualise\/graph/, 'FalkorDB graph', 'Replay a run on the graph, and watch the queries FalkorDB runs now'],
  [/^\/visualise\/cost/, 'Model cost', 'What each model call, agent and complaint run costs'],
  [/^\/visualise\/finance/, 'Finance and analysis', 'Where the stolen money went, and how TraceHold compares'],
]

const COUNT_TEXT: Record<Count, (n: number) => string> = {
  gate: n => `${n} case${n === 1 ? '' : 's'} waiting at the gate`,
  reviews: n => `${n} account${n === 1 ? '' : 's'} waiting for review`,
}

const RAIL_KEY = 'tracehold.rail'

function LogoMark() {
  return (
    <svg viewBox="0 0 32 32" className="size-8 shrink-0" aria-hidden>
      <rect width="32" height="32" rx="8" fill="var(--color-brand-600)" />
      <path d="M8 11h16M8 16h16M8 21h10" stroke="var(--color-brand-100)" strokeWidth="2.2" strokeLinecap="round" />
      <circle cx="23" cy="21" r="3" fill="#fff" />
    </svg>
  )
}

function Logo() {
  return (
    <div className="flex items-center gap-2.5 px-2">
      <LogoMark />
      <div className="leading-tight">
        <div className="text-[16px] font-semibold tracking-tight text-ink">TraceHold</div>
        <div className="text-[11.5px] text-ink-3">Mule-account recovery</div>
      </div>
    </div>
  )
}

/** The figures beside the menu: cases waiting at the gate and open reviews at the chosen bank (shared query cache). */
function useCounts(): Record<Count, number> {
  const { bank } = useApp()
  const invs = useQuery({ queryKey: ['investigations', bank], queryFn: () => api.investigations(bank), enabled: !!bank, refetchInterval: 15000 })
  const reviews = useQuery({ queryKey: ['reviews', bank, 'open'], queryFn: () => api.reviews(bank, 'open'), enabled: !!bank, refetchInterval: 30000 })
  return { gate: (invs.data ?? []).filter(i => i.status === 'at_gate').length, reviews: reviews.data?.length ?? 0 }
}

function CountBadge({ kind, n, corner }: { kind: Count; n: number; corner?: boolean }) {
  if (!n) return null
  return (
    <span title={COUNT_TEXT[kind](n)} aria-label={COUNT_TEXT[kind](n)}
      className={cn('num rounded-full text-center font-semibold', corner
        ? 'absolute -right-1 -top-1 h-4 min-w-4 bg-brand-600 px-1 text-[10px] leading-4 text-white ring-2 ring-card'
        : 'ml-auto min-w-5 bg-brand-100 px-1.5 text-[11.5px] leading-5 text-brand-800')}>{n}</span>
  )
}

const BUILT_ON = [
  { icon: <Database className="size-3.5" />, text: 'FalkorDB 6.0.2 graphs' },
  { icon: <Sparkles className="size-3.5" />, text: 'Claude Opus 4.6 on Bedrock' },
  { icon: <ShieldCheck className="size-3.5" />, text: 'Agents in OpenShell sandboxes' },
]

/** The sidebar's content, shared by the fixed sidebar (wide screens, with the tour's ids) and the menu drawer.
 *  `rail` draws it as a narrow column of icons with tooltips; the bank and the "Built on" note need the full width. */
function SidebarBody({ ids, rail = false, onNavigate, onToggle }: { ids: boolean; rail?: boolean; onNavigate?: () => void; onToggle?: () => void }) {
  const { banks, bank, bankName, setBank } = useApp()
  const loc = useLocation()
  const counts = useCounts()
  const on = (item: NavItem, isActive: boolean) => isActive || (!!item.sub && loc.pathname.startsWith(item.to))
  if (rail) {
    return (
      <>
        <div className="grid place-items-center pb-4 pt-5"><Tip content="TraceHold" side="right"><span><LogoMark /></span></Tip></div>
        <div className="grid place-items-center" id={ids ? 'tour-bank' : undefined}>
          <Tip content={`${bankName} (${bank}). Expand the menu to switch bank.`} side="right">
            <button onClick={onToggle} aria-label={`Bank ${bankName}; expand the menu to switch`}
              className="num grid h-9 min-w-9 place-items-center rounded-full border border-rule-2 bg-paper px-2 text-[12px] font-semibold text-ink hover:border-brand-300 hover:bg-brand-50">{bank}</button>
          </Tip>
        </div>
        <nav className="mt-5 flex flex-1 flex-col items-center gap-1 overflow-y-auto scroll-thin" aria-label="Main">
          {NAV.map(item => (
            <div key={item.to} className="flex flex-col items-center gap-1">
              {/* the span takes the tooltip: Radix merges class names as strings, which breaks NavLink's class function */}
              <Tip content={item.label} side="right"><span className="inline-flex">
                <NavLink to={item.sub ? item.sub[0].to : item.to} end={item.to === '/'} id={ids ? item.id : undefined} aria-label={item.label}
                  className={({ isActive }) => cn('relative grid size-10 place-items-center rounded-[10px] transition-colors',
                    on(item, isActive) ? 'bg-brand-600 text-white shadow-card' : 'text-ink-2 hover:bg-brand-50 hover:text-ink')}>
                  {item.icon}
                  {item.count && <CountBadge kind={item.count} n={counts[item.count]} corner />}
                </NavLink>
              </span></Tip>
              {item.sub && (
                <div className="flex flex-col items-center gap-0.5 rounded-[10px] bg-paper/70 p-1">
                  {item.sub.map(s => (
                    <Tip key={s.to} content={s.label} side="right"><span className="inline-flex">
                      <NavLink to={s.to} id={ids ? s.id : undefined} aria-label={s.label}
                        className={({ isActive }) => cn('grid size-8 place-items-center rounded-[8px] transition-colors',
                          isActive ? 'bg-card text-brand-800 shadow-card' : 'text-ink-3 hover:bg-card hover:text-ink')}>
                        {s.icon}
                      </NavLink>
                    </span></Tip>
                  ))}
                </div>
              )}
            </div>
          ))}
        </nav>
        <div className="grid place-items-center border-t border-rule py-3">
          <Tip content="Expand the menu" side="right">
            <button onClick={onToggle} aria-label="Expand the menu" className="grid size-9 place-items-center rounded-[10px] text-ink-3 hover:bg-brand-50 hover:text-ink">
              <PanelLeftOpen className="size-[18px]" />
            </button>
          </Tip>
        </div>
      </>
    )
  }
  return (
    <>
      <div className="px-4 pb-4 pt-5"><Logo /></div>
      <div className="px-4" id={ids ? 'tour-bank' : undefined}>
        <label className="block">
          <span className="px-1 text-[12px] text-ink-3">Bank</span>
          <select value={bank} onChange={e => setBank(e.target.value)}
            className="mt-1 h-9 w-full rounded-[var(--radius-control)] border border-rule-2 bg-paper px-2.5 text-[13.5px] font-medium text-ink focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200">
            {banks.map(b => <option key={b.bank_id} value={b.bank_id}>{b.name} ({b.bank_id})</option>)}
          </select>
        </label>
      </div>
      <nav className="mt-5 flex-1 overflow-y-auto px-3 scroll-thin" aria-label="Main">
        {NAV.map(item => (
          <div key={item.to} className="mb-0.5">
            <NavLink to={item.sub ? item.sub[0].to : item.to} end={item.to === '/'} id={ids ? item.id : undefined} onClick={onNavigate}
              className={({ isActive }) => cn('relative flex h-9 items-center gap-2.5 rounded-[var(--radius-control)] px-2.5 text-[13.5px] font-medium transition-colors',
                on(item, isActive) ? 'bg-brand-50 text-brand-800' : 'text-ink-2 hover:bg-paper hover:text-ink')}>
              {({ isActive }) => (
                <>
                  {on(item, isActive) && <span className="absolute -left-3 bottom-1.5 top-1.5 w-[3px] rounded-r-full bg-brand-600" aria-hidden />}
                  <span className="text-current">{item.icon}</span>{item.label}
                  {item.count && <CountBadge kind={item.count} n={counts[item.count]} />}
                </>
              )}
            </NavLink>
            {item.sub && (
              <div className="ml-[22px] mt-0.5 space-y-0.5 border-l border-rule pl-2">
                {item.sub.map(s => (
                  <NavLink key={s.to} to={s.to} id={ids ? s.id : undefined} onClick={onNavigate}
                    className={({ isActive }) => cn('flex h-8 items-center gap-2 rounded-[var(--radius-control)] px-2 text-[13px] transition-colors',
                      isActive ? 'bg-brand-50/70 font-medium text-brand-800' : 'text-ink-3 hover:bg-paper hover:text-ink')}>
                    {s.icon}{s.label}
                  </NavLink>
                ))}
              </div>
            )}
          </div>
        ))}
      </nav>
      <div className="border-t border-rule px-5 py-4">
        <div className="flex items-center justify-between">
          <p className="text-[11.5px] font-medium text-ink-3">Built on</p>
          {onToggle && (
            <Tip content="Collapse the menu to icons" side="right">
              <button onClick={onToggle} aria-label="Collapse the menu to icons" className="-mr-1.5 grid size-7 place-items-center rounded-[8px] text-ink-3 hover:bg-brand-50 hover:text-ink">
                <PanelLeftClose className="size-4" />
              </button>
            </Tip>
          )}
        </div>
        <ul className="mt-2 space-y-1.5 text-[12.5px] leading-snug text-ink-2">
          {BUILT_ON.map(b => <li key={b.text} className="flex items-center gap-2"><span className="text-brand-600">{b.icon}</span>{b.text}</li>)}
        </ul>
      </div>
    </>
  )
}

function Sidebar({ rail, onToggle }: { rail: boolean; onToggle: () => void }) {
  return (
    <aside className={cn('sticky top-0 hidden h-screen shrink-0 flex-col border-r border-rule bg-card transition-[width] duration-200 lg:flex', rail ? 'w-[72px]' : 'w-[248px]')}>
      <SidebarBody ids rail={rail} onToggle={onToggle} />
    </aside>
  )
}

/** Below 1024 px the sidebar is hidden; the header's menu button opens the same content as a drawer. */
function MenuDrawer({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  return (
    <DialogP.Root open={open} onOpenChange={onOpenChange}>
      <DialogP.Portal>
        <DialogP.Overlay className="fixed inset-0 z-40 bg-ink/25" />
        <DialogP.Content aria-describedby={undefined}
          className="fixed inset-y-0 left-0 z-50 flex w-[272px] max-w-[85vw] flex-col bg-card shadow-float outline-none">
          <DialogP.Title className="sr-only">Menu</DialogP.Title>
          <SidebarBody ids={false} onNavigate={() => onOpenChange(false)} />
          <DialogP.Close className="absolute right-3 top-5 rounded-md p-1.5 text-ink-3 hover:bg-paper-2 hover:text-ink" aria-label="Close menu">
            <X className="size-4" />
          </DialogP.Close>
        </DialogP.Content>
      </DialogP.Portal>
    </DialogP.Root>
  )
}

type AgentState = 'working' | 'online' | 'offline' | 'down'
const DOT: Record<AgentState, string> = {
  working: 'bg-brand-500 animate-pulse-soft', online: 'bg-brand-500', offline: 'bg-review', down: 'bg-danger',
}
const AGENTS = ['tracer', 'adjudicator', 'verifier'] as const

/** One dot per agent: online when that agent's worker polled the tool server in the last 20 s, pulsing while it works on
 *  a case at this bank. A server that does not yet tag polls with the agent falls back to any agent's poll. */
function AgentsLive() {
  const { bank } = useApp()
  const q = useQuery({ queryKey: ['agents-live'], queryFn: () => api.activity(0, 'agent', undefined, 200), refetchInterval: 8000 })
  const h = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 20000 })
  const invs = useQuery({ queryKey: ['investigations', bank], queryFn: () => api.investigations(bank), enabled: !!bank, refetchInterval: 15000 })
  const now = q.dataUpdatedAt
  const entries = q.data?.entries ?? []
  const tagged = entries.some(e => e.agent !== undefined)
  const last = (a?: string) => Math.max(0, ...entries.filter(e => !a || e.agent === a).map(e => e.ts_ms))
  const ok = !h.isError && !q.isError && h.data?.tool_server !== false
  const rows = AGENTS.map(a => {
    const stage = STAGES.find(s => s.key === a)!
    const busy = (invs.data ?? []).filter(i => stageState(i.status, stage) === 'working').map(i => i.complaint_id)
    const t = last(tagged ? a : undefined)
    const seen = !!q.data && t > 0 && now - t < 20000
    const state: AgentState = !ok ? 'down' : !seen ? 'offline' : busy.length ? 'working' : 'online'
    const text = state === 'down' ? 'the tool server is not answering' : state === 'offline' ? 'no poll in the last 20 s; its sandbox may be stopped'
      : state === 'working' ? `working on ${busy.join(', ')}` : 'online, waiting for work'
    return { a, state, text }
  })
  const online = rows.filter(r => r.state === 'online' || r.state === 'working').length
  const label = !ok ? 'Tool server down' : online === 3 ? 'Agents online' : `${online} of 3 agents online`
  const summary = rows.map(r => `${AGENT_INFO[r.a].name}: ${r.text}`).join('. ')
  return (
    <Tip content={<ul className="space-y-0.5">{rows.map(r => <li key={r.a}><b className="font-semibold">{AGENT_INFO[r.a].name}</b>: {r.text}</li>)}</ul>}>
      <span role="status" aria-label={`${label}. ${summary}`}
        className={cn('inline-flex h-9 shrink-0 items-center gap-2 rounded-full border px-3 text-[12.5px] font-medium',
          online === 3 ? 'border-brand-100 bg-brand-50 text-brand-800' : 'border-review/20 bg-review-tint text-review')}>
        <span className="flex items-center gap-1" aria-hidden>{rows.map(r => <span key={r.a} className={cn('size-2 rounded-full', DOT[r.state])} />)}</span>
        <span className="hidden 2xl:inline">{label}</span>
      </span>
    </Tip>
  )
}

/** On a case page the header names the case and leads back to the queue. */
function CaseCrumb({ inv }: { inv: string }) {
  const { bank } = useApp()
  const d = useQuery({ queryKey: ['detail', bank, inv], queryFn: () => api.detail(bank, inv), enabled: !!bank, staleTime: 5000 })
  const status = d.data?.investigation.status
  return (
    <div className="flex min-w-0 flex-1 items-center gap-3">
      <Tip content="Back to all investigations">
        <Link to="/investigations" aria-label="Back to all investigations"
          className="grid size-8 shrink-0 place-items-center rounded-full border border-rule-2 bg-card text-ink-2 shadow-card transition-colors hover:border-brand-300 hover:bg-brand-50 hover:text-ink">
          <ArrowLeft className="size-4" />
        </Link>
      </Tip>
      <div className="min-w-0">
        <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1 text-[12.5px] text-ink-3">
          <Link to="/investigations" className="shrink-0 underline-offset-4 hover:text-ink hover:underline">Investigations</Link>
          <ChevronRight className="size-3.5 shrink-0" aria-hidden />
          <span className="id truncate text-[12px]" aria-current="page">{inv}</span>
        </nav>
        <h1 className="flex min-w-0 items-center gap-2 text-[18px] font-semibold leading-6 text-ink">
          <span className="truncate">{d.data?.investigation.complaint_id ?? 'Case'}</span>
          {status && <Badge tone={STATUS_INFO[status].tone} className="hidden sm:inline-flex">{STATUS_INFO[status].label}</Badge>}
        </h1>
      </div>
    </div>
  )
}

function Header({ onMenu }: { onMenu: () => void }) {
  const loc = useLocation()
  const inv = matchPath('/investigations/:inv', loc.pathname)?.params.inv
  const t = TITLES.find(([re]) => re.test(loc.pathname)) ?? TITLES[0]
  const roomy = useMedia('(min-width: 1280px)')
  return (
    <header className="sticky top-0 z-30 border-b border-rule bg-paper/95 backdrop-blur-[2px]">
      <div className="flex h-16 items-center gap-2.5 px-4 sm:gap-3 sm:px-6 lg:px-8">
        <button onClick={onMenu} aria-label="Open menu"
          className="grid size-9 shrink-0 place-items-center rounded-[var(--radius-control)] text-ink-2 hover:bg-brand-50 hover:text-ink lg:hidden">
          <Menu className="size-5" />
        </button>
        {inv ? <CaseCrumb inv={inv} /> : (
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-[18px] font-semibold leading-6 text-ink">{t[1]}</h1>
            <p className="truncate text-[13px] text-ink-3">{t[2]}</p>
          </div>
        )}
        {roomy ? <CaseSearch className="w-[260px] shrink-0" /> : <SearchButton />}
        <AgentsLive />
        <NeedsYouBell />
        <Button variant="secondary" size="sm" icon={<Compass className="size-4" />} onClick={() => startTour()} id="tour-new-here" aria-label="New here? Take the tour" className="h-9 rounded-full">
          <span className="hidden sm:inline">New here?</span>
        </Button>
      </div>
    </header>
  )
}

export function Shell() {
  const loc = useLocation()
  const nav = useNavigate()
  const wide = useMedia('(min-width: 1024px)')
  const [menu, setMenu] = useState(false)
  const [rail, setRail] = useState(() => { try { return localStorage.getItem(RAIL_KEY) === '1' } catch { return false } })
  const toggleRail = () => setRail(r => {
    try { localStorage.setItem(RAIL_KEY, r ? '0' : '1') } catch { /* private window */ }
    return !r
  })
  useEffect(() => { setTourNavigate(to => nav(to)) }, [nav])
  const main = useRef<HTMLElement>(null)
  useEffect(() => { main.current?.scrollTo({ top: 0 }) }, [loc.pathname])
  return (
    <div className="flex min-h-screen">
      <Sidebar rail={rail} onToggle={toggleRail} />
      <MenuDrawer open={menu && !wide} onOpenChange={setMenu} />
      <div className="flex min-w-0 flex-1 flex-col">
        <Header onMenu={() => setMenu(true)} />
        <main ref={main} className="flex-1 px-4 py-6 sm:px-6 lg:px-8">
          <Outlet />
        </main>
      </div>
      <HelpChat />
    </div>
  )
}
