import type { Detail, Investigation, Label, ProposalPayload, ProposedAction, Status, TracePayload, TrailNode, VerdictPayload } from './types'

export const AGENTS = ['tracer', 'adjudicator', 'verifier'] as const
export type Agent = (typeof AGENTS)[number]

export const AGENT_INFO: Record<string, { name: string; role: string; does: string }> = {
  tracer: { name: 'Tracer', role: 'Follows the money',
    does: 'Starts at the victim\'s disputed transfer and follows the stolen money forward through FalkorDB, hop by hop, splitting it pro rata, until it is held, cashed out or too small to follow.' },
  adjudicator: { name: 'Adjudicator', role: 'Judges each account',
    does: 'Reads each traced account\'s signals (pass-through, age, shared devices, mule communities, the bank\'s memory) and labels it mule, likely genuine or uncertain, then proposes a lien or a review.' },
  verifier: { name: 'Verifier', role: 'Checks every lien',
    does: 'Re-checks each proposed lien against the evidence and the bank\'s lessons, approves or rejects it with a reason, and holds the case at the HITL gate for a person.' },
  chat: { name: 'Case chat', role: 'Answers questions', does: 'The Verifier answering an analyst\'s question from the case record, citing evidence IDs.' },
  gate: { name: 'HITL gate', role: 'A person decides', does: 'A human approves or overrides the Verifier before any lien request reaches the bank.' },
}

/** Chart colours per agent: the validated categorical order (dataviz check, light, white surface). */
export const AGENT_COLOR: Record<string, string> = {
  tracer: 'var(--color-s-blue)', adjudicator: 'var(--color-s-orange)', verifier: 'var(--color-s-aqua)', chat: 'var(--color-s-yellow)',
}
export const AGENT_HEX: Record<string, string> = { tracer: '#2a78d6', adjudicator: '#eb6834', verifier: '#1baf7a', chat: '#eda100' }

export const STAGES: { key: string; name: string; active: Status[]; done: Status[] }[] = [
  { key: 'tracer', name: 'Tracer', active: ['new', 'tracing'], done: ['traced', 'adjudicating', 'checks_failed', 'proposed', 'verifying', 'at_gate', 'executed', 'closed'] },
  { key: 'adjudicator', name: 'Adjudicator', active: ['traced', 'adjudicating', 'checks_failed'], done: ['proposed', 'verifying', 'at_gate', 'executed', 'closed'] },
  { key: 'verifier', name: 'Verifier', active: ['proposed', 'verifying'], done: ['at_gate', 'executed', 'closed'] },
  { key: 'gate', name: 'HITL gate', active: ['at_gate'], done: ['executed', 'closed'] },
]

export type StageState = 'done' | 'working' | 'waiting' | 'failed'
export function stageState(status: Status, s: (typeof STAGES)[number]): StageState {
  if (status === 'failed') return 'failed'
  if (s.done.includes(status)) return 'done'
  return s.active.includes(status) ? 'working' : 'waiting'
}

export const FINAL: Status[] = ['executed', 'closed', 'failed']
export const isLive = (s: Status) => !FINAL.includes(s) && s !== 'at_gate'

export const STATUS_INFO: Record<Status, { label: string; tone: Tone }> = {
  new: { label: 'Queued', tone: 'none' },
  tracing: { label: 'Tracing', tone: 'proposed' },
  traced: { label: 'Traced', tone: 'proposed' },
  adjudicating: { label: 'Adjudicating', tone: 'proposed' },
  checks_failed: { label: 'Checks failed', tone: 'rejected' },
  proposed: { label: 'Proposed', tone: 'proposed' },
  verifying: { label: 'Verifying', tone: 'proposed' },
  at_gate: { label: 'At the gate', tone: 'review' },
  executed: { label: 'Executed', tone: 'approved' },
  closed: { label: 'Closed', tone: 'none' },
  failed: { label: 'Failed', tone: 'danger' },
}

export type Tone = 'approved' | 'proposed' | 'rejected' | 'review' | 'victim' | 'none' | 'danger' | 'brand'

export const LABEL_INFO: Record<Label, { label: string; tone: Tone }> = {
  mule: { label: 'Mule', tone: 'victim' },
  likely_genuine: { label: 'Likely genuine', tone: 'approved' },
  uncertain: { label: 'Uncertain', tone: 'review' },
}

export const ACTION_INFO: Record<ProposedAction, string> = {
  lien: 'Lien', review: 'Review', review_and_request_info: 'Review and ask for documents', none: 'No action',
}

export const BRANCH_END: Record<string, string> = {
  cashed_out: 'Cashed out', absorbed_genuine: 'Held by a genuine account', no_later_outflow: 'Money stayed here',
  below_minimum: 'Too small to follow', depth_limit: 'Depth limit', cycle: 'Came back round', untracked: 'Left the bank\'s view',
}

export type NodeState = 'victim' | 'lien approved' | 'lien rejected' | 'lien proposed' | 'review' | 'none'
export function nodeState(n: TrailNode): NodeState {
  if (n.role === 'victim') return 'victim'
  if (n.action === 'lien') return n.approved === true ? 'lien approved' : n.approved === false ? 'lien rejected' : 'lien proposed'
  if (n.action === 'review' || n.action === 'review_and_request_info') return 'review'
  return 'none'
}
export const NODE_TONE: Record<NodeState, Tone> = {
  victim: 'victim', 'lien approved': 'approved', 'lien rejected': 'rejected', 'lien proposed': 'proposed', review: 'review', none: 'none',
}

/** The latest trace, proposal and verdict of a run, with lookups the pages need. */
export function report(d: Detail | undefined) {
  const latest: Record<string, unknown> = {}
  for (const h of d?.handoffs ?? []) latest[h.kind] = h.payload
  const trace = latest.trace as TracePayload | undefined
  const proposal = latest.proposal as ProposalPayload | undefined
  const verdict = latest.verdict as VerdictPayload | undefined
  const props = new Map((proposal?.accounts ?? []).map(a => [a.account_id, a]))
  const verdicts = new Map((verdict?.verdicts ?? []).map(v => [v.decision_id, v]))
  const decisions = new Map((d?.decisions ?? []).map(x => [x.id, x]))
  const liens = new Map((d?.liens ?? []).map(l => [l.decision_id, l]))
  const held = (trace?.accounts ?? []).reduce((s, a) => s + a.tainted_paise, 0)
  return { trace, proposal, verdict, props, verdicts, decisions, liens, held }
}

/** One line on how a finished run ended. */
export function outcome(d: Detail) {
  const decided = d.decisions.filter(x => x.approved !== null && x.approved !== undefined)
  const total = d.liens.reduce((s, l) => s + (l.amount ?? 0), 0)
  if (d.liens.length) return { liens: d.liens.length, total, decided: decided.length }
  return { liens: 0, total: 0, decided: decided.length }
}

export const caseTitle = (i: Investigation) => `${i.complaint_id}`
