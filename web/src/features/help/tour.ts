import { type Driver, driver, type DriveStep } from 'driver.js'

// The "New here?" walkthrough: it moves across the pages, highlighting each part and saying what it is for.

type Step = DriveStep & { route?: string }

let navigate: ((to: string) => void) | null = null
export function setTourNavigate(fn: (to: string) => void) { navigate = fn }

const STEPS: Step[] = [
  { popover: { title: 'Welcome to TraceHold', description: 'When a bank customer reports a fraudulent transfer, TraceHold\'s three AI agents follow the stolen money through the accounts it passed through, often mule accounts, and propose liens that freeze only the victim\'s share. A person approves every lien. This tour takes about two minutes.' } },
  { route: '/', element: '#tour-bank', popover: { title: 'Pick a bank', description: 'Each bank is its own fraud desk: it sees only complaints from its customers and has its own memory graph. The agents never see another bank\'s memory.', side: 'right' } },
  { route: '/', element: '#tour-books', popover: { title: 'Where the disputed money is', description: 'Across every case at this bank, each disputed rupee is liened, still held, cashed out, or on a branch too small to follow. The books balance to the paisa.' } },
  { route: '/', element: '#tour-agents', popover: { title: 'Three agents and you', description: 'The Tracer follows the money, the Adjudicator judges each account and proposes liens or reviews, and the Verifier checks every lien. Then you decide at the HITL gate.' } },
  { route: '/', element: '#tour-needs-you', popover: { title: 'What needs you', description: 'Cases waiting at the gate, and accounts the agents sent to a person instead of a lien.', side: 'left' } },
  { route: '/investigations', element: '#tour-start', popover: { title: 'Start an investigation', description: 'Pick a complaint reported to this bank. The agents take one to three minutes and about 15 to 30 US cents of model time.', side: 'right' } },
  { route: '/investigations', element: '#tour-queue', popover: { title: 'The case queue', description: 'Every case at this bank. The four marks under each show how far it has got: Tracer, Adjudicator, Verifier, gate.', side: 'right' } },
  { route: '/investigations', element: '#tour-pipeline', popover: { title: 'Where this case is', description: 'The stage each agent is at. The small graph button beside the Tracer replays this run on the FalkorDB graph.' } },
  { route: '/investigations', element: '#tour-ledger', popover: { title: 'The case\'s books', description: 'The disputed amount always equals what is still held on traced accounts, what was cashed out and what was not followed. A lien is never more than what is held.' } },
  { route: '/investigations', element: '#tour-gate', popover: { title: 'The HITL gate', description: 'Approve the Verifier\'s verdicts with one button, or override a lien with a reason. Your reason becomes a lesson this bank\'s agents read in later cases.', side: 'left' } },
  { route: '/investigations', element: '#tour-trail', popover: { title: 'The money trail', description: 'Read from FalkorDB: each card is an account the money reached, each arrow the transfers that carried it. The colour strip shows what happened to the account.' } },
  { route: '/investigations', element: '#tour-accounts', popover: { title: 'Traced accounts', description: 'How much of the victim\'s money each account holds, its share of the balance, the label, the proposed action and the Verifier\'s verdict. Click a row to read the evidence.' } },
  { route: '/investigations', element: '#tour-case-chat', popover: { title: 'Ask about this case', description: 'The Verifier answers your question from the case record and cites evidence IDs. An answer that cites an ID that does not exist is rejected.' } },
  { route: '/reviews', element: '#nav-reviews', popover: { title: 'Review queue', description: 'Accounts the agents sent to a person. Clearing or escalating one also scores the bank\'s lessons on that account.', side: 'right' } },
  { route: '/memory', element: '#nav-memory', popover: { title: 'Bank memory', description: 'The lessons this bank has learned at the gate, and how their confidence rose or fell with later decisions.', side: 'right' } },
  { route: '/visualise/graph', element: '#tour-replay', popover: { title: 'Replay a run on FalkorDB', description: 'Pick a complaint\'s run and press play: the trail grows layer by layer as the agents built it, and the steps list shows each tool and the Cypher it runs on FalkorDB.' } },
  { route: '/visualise/graph', element: '#tour-live', popover: { title: 'FalkorDB right now', description: 'Every query the tool server sends to FalkorDB, live: the agents\' tools, their polling, and this page\'s own reads.' } },
  { route: '/visualise/cost', element: '#tour-cost-runs', popover: { title: 'Model cost', description: 'What every complaint run cost, split by agent. Click a run to see each model call and its tokens.' } },
  { route: '/visualise/finance', element: '#tour-finance-flow', popover: { title: 'Finance and analysis', description: 'Where each complaint\'s money went, and below, how TraceHold compares with a full freeze, a greedy trace and fixed rules.' } },
  { element: '#help-chat-button', popover: { title: 'Help, any time', description: 'Ask about the app, mule accounts or a decision in the case you have open.', side: 'left' } },
  { element: '#tour-new-here', popover: { title: 'That is the tour', description: 'Start it again from here whenever you like.', side: 'bottom' } },
]

function onRoute(route?: string) {
  if (!route) return true
  const p = window.location.pathname
  return route === '/' ? p === '/' : p === route || p.startsWith(route + '/')
}

function waitFor(sel: string | undefined, ms = 5000) {
  return new Promise<void>(resolve => {
    if (!sel) return resolve()
    const t0 = Date.now()
    const tick = () => (document.querySelector(sel) || Date.now() - t0 > ms ? resolve() : window.setTimeout(tick, 120))
    tick()
  })
}

let active: Driver | null = null

export function startTour() {
  active?.destroy()
  const d = driver({
    popoverClass: 'th-tour', showProgress: true, progressText: '{{current}} of {{total}}', nextBtnText: 'Next', prevBtnText: 'Back',
    doneBtnText: 'Finish', allowClose: true, overlayColor: '#13291c', overlayOpacity: 0.32, stagePadding: 6, stageRadius: 12,
    smoothScroll: true, steps: STEPS.map(({ element, popover }) => ({ element, popover })),
    onNextClick: () => go(1), onPrevClick: () => go(-1),
    onDestroyed: () => { active = null },
  })
  active = d
  async function go(dir: 1 | -1) {
    const i = d.getActiveIndex() ?? 0
    const next = STEPS[i + dir]
    if (!next) { d.destroy(); return }
    if (!onRoute(next.route) && next.route) {
      navigate?.(next.route)
      await waitFor(typeof next.element === 'string' ? next.element : undefined)
      await new Promise(r => setTimeout(r, 250))
    }
    if (dir > 0) d.moveNext()
    else d.movePrevious()
  }
  d.drive()
}
