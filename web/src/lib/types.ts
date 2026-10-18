// The shapes the UI server returns (the handoff API on the tool server, plus /api/ui/*).

export type Status =
  | 'new' | 'tracing' | 'traced' | 'adjudicating' | 'proposed' | 'checks_failed' | 'verifying'
  | 'at_gate' | 'executed' | 'closed' | 'failed'

export type Label = 'mule' | 'likely_genuine' | 'uncertain'
export type ProposedAction = 'lien' | 'review' | 'review_and_request_info' | 'none'

export interface Bank { bank_id: string; name: string }

export interface Complaint {
  complaint_id: string
  victim_account_id: string
  disputed_paise: number
  reported_at: number
  fraud_transfers: number
}

export interface Investigation {
  id: string
  complaint_id: string
  status: Status
  trace_id: string
  started_at: number
  updated_at: number
  claimed_by?: string | null
  claimed_at?: number | null
  lease_until?: number | null
  gate_mode?: string | null
  gate_decisions?: string | null
  closed_reason?: string | null
  failure?: string | null
  failed_by?: string | null
  reclaims?: number | null
}

export interface TraceAccount {
  account_id: string
  layer: number
  tainted_paise: number
  balance_paise: number
  branch_end: string | null
  transfer_ids: string[]
}

export interface TracePayload {
  complaint_id: string
  investigation_id: string
  disputed_paise: number
  unrecoverable_paise: number
  not_followed_paise: number
  accounts: TraceAccount[]
}

export interface Check { check: number; name: string; passed: boolean; account_ids: string[] }

export interface ProposalAccount {
  account_id: string
  label: Label
  proposed_action: ProposedAction
  lien_proposal_id: string | null
  lien_amount_paise: number | null
  rationale: string
  lessons_applied: string[]
  evidence: { signals: string[]; transfer_ids: string[] }
}

export interface ProposalPayload {
  accounts: ProposalAccount[]
  attempt: number
  checks?: { results: Check[] }
  checks_failed_twice?: boolean
}

export interface Verdict {
  decision_id: string
  verdict: 'approve' | 'reject'
  reason: string
  decided_by: string
  lessons_cited: string[]
}

export interface VerdictPayload { verdicts: Verdict[]; checks_failed: boolean }

export interface Handoff {
  id: string
  kind: 'trace' | 'proposal' | 'verdict' | string
  from_agent: string
  to_agent: string
  n: number
  ts: number
  payload: unknown
}

export interface Decision {
  id: string
  account_id: string
  label: Label
  lien_amount: number | null
  proposed_action: ProposedAction
  reason: string
  ts: number
  approved?: boolean | null
  decided_by?: string | null
  gate_mode?: string | null
  gate_reason?: string | null
  complaint_id?: string
  investigation_id?: string
}

export interface Action {
  id: string
  kind: 'review' | 'info_request' | 'flag' | string
  entity_kind: string
  entity_id: string
  status: string
  detail: string
  ts: number
  external_id?: string | null
  undo_reason?: string | null
  resolution_note?: string | null
  resolved_at?: number | null
}

export interface Lien { id: string; decision_id: string; account_id: string; amount: number; status: string; ts?: number }

export interface Step {
  id: string
  n: number
  agent: string
  node: string
  tool: string
  input_summary: string
  output_summary: string
  latency_ms: number
  tokens: number
  usd: number
  started_ms: number
  status: string
  ts: number
}

export interface Claim {
  id: string
  account_id: string
  amount: number | null
  attempt: number
  decision_id: string | null
  label: Label
  lessons_applied: string[]
  proposed_action: ProposedAction
  signals: string[]
  text: string
  transfer_ids: string[]
  ts: number
}

export interface Message {
  id: string
  n: number
  role: 'human' | 'verifier' | string
  status: string
  text: string
  cited_ids: string[]
  reply_to: string
  author: string
  note: string
  ts: number
}

export interface LessonEvent { ts: number; event: string; confidence?: number; decision_id?: string; action_id?: string }

export interface Lesson {
  id: string
  stance: 'release' | 'hold'
  label: Label | null
  text: string
  confidence: number
  active: boolean
  source: string
  times_confirmed: number
  times_contradicted: number
  superseded_by: string | null
  complaint_id: string
  account_id: string
  investigation_id: string
  decision_id: string
  created_ts: number
  updated_ts: number
  history: LessonEvent[]
}

export interface Detail {
  investigation: Investigation
  handoffs: Handoff[]
  decisions: Decision[]
  actions: Action[]
  liens: Lien[]
  steps: Step[]
  claims: Claim[]
  messages: Message[]
  lessons_made: Lesson[]
}

export interface TrailNode {
  id: string
  layer: number
  role: 'victim' | 'traced'
  disputed_paise?: number
  tainted_paise?: number
  balance_paise?: number
  branch_end?: string | null
  label?: Label | null
  action?: ProposedAction | null
  approved?: boolean | null
  seen_before?: boolean
}

export interface TrailEdge { src: string; dst: string; transfers: number; amount_paise: number; transfer_ids: string[] }
export interface Trail { nodes: TrailNode[]; edges: TrailEdge[] }

export interface Gate { status: Status; gate_mode: string | null; decisions: { decision_id: string; approve: boolean; reason: string }[] | null }

export interface Review {
  id: string
  kind: string
  status: 'open' | 'cleared' | 'escalated' | string
  detail: string
  account_id: string
  investigation_id: string
  complaint_id: string
  ts: number
}

export interface Memory {
  counts: Record<string, number>
  lessons: Lesson[]
  decisions: Decision[]
  investigations: { id: string; complaint_id: string; status: Status; started_at: number }[]
}

export interface SpendTotals { calls: number; usd: number; input_tokens: number; output_tokens: number }
export interface Spend { total: SpendTotals; investigation?: SpendTotals; by_agent?: Record<string, SpendTotals> }

export interface Call {
  id: number
  ts: number
  agent: string
  investigation_id: string
  model: string
  input_tokens: number
  cached_input_tokens: number
  output_tokens: number
  reasoning_tokens: number
  usd: number
}

export interface QueryEntry {
  seq: number
  ts_ms: number
  graph: string
  kind: 'read' | 'read-write'
  query: string
  params: Record<string, string>
  ms: number
  engine_ms: number | null
  rows: number | null
  ok: boolean
  source: 'tool' | 'agent' | 'ui' | 'other'
  name: string
  bank: string
  investigation_id: string
  /** The agent whose poll ran the query (tracer, adjudicator, verifier); empty for tools and the UI. Older servers omit it. */
  agent?: string
}

export interface Activity {
  entries: QueryEntry[]
  last_seq: number
  totals: Record<string, { queries: number; ms: number; errors: number }>
  started_ms: number
}

export interface GraphStats {
  network: Record<string, number>
  memory: Record<string, Record<string, number>>
  graphs: string[]
  at: number
}

export interface UiConfig {
  banks: string[]
  default_bank: string
  gate_mode: 'placeholder' | 'confirm'
  budget: { per_run_usd: number; total_usd: number }
  default_model: string
  langfuse_host: string
  langfuse_ready: boolean
}

export interface EvalFile { file: string; written: string; sample: string; rubric: string; tracehold_runs: number; complaints: number | null }

export interface EvalRow {
  scope: 'sample' | 'all_test'
  method: 'tracehold' | 'full_freeze' | 'greedy' | 'rules_only'
  group: 'shallow' | 'medium' | 'deep' | 'held_out' | 'all_buckets'
  complaints: number
  frozen: number
  innocent: number
  innocent_accounts: number
  beyond_taint: number
  stolen_held: number
  liens: number
  liened_mules: number
  reviews: number
  trail_mules: number
  caught: number
  mules_liened: number
  disputed: number
  precision: number | null
  recall: number | null
  lien_recall: number | null
  reviews_per_case: number
  median_usd?: number
  median_seconds?: number
}

export interface EvalCase {
  complaint_id: string
  group: EvalRow['group']
  pattern: string
  depth: number
  disputed: number
  in_sample: boolean
  method: EvalRow['method']
  status?: string
  usd?: number | null
  seconds?: number | null
  frozen: number
  innocent: number
  innocent_accounts: number
  beyond_taint: number
  stolen_held: number
  liens: number
  liened_mules: number
  reviews: number
  trail_mules: number
  caught: number
  mules_liened: number
}

export interface Evaluation {
  meta: { written: string; held_out_pattern: string; sample_name?: string; rubric?: string; tracehold_runs: number; tracehold_failed: string[] }
  headline: { innocent_less_than_full_freeze?: number; innocent_less_than_greedy?: number | null; tracehold_recall?: number; tracehold_precision?: number; complaints?: number }
  rules_thresholds: Record<string, number>
  rules_dev_f1: number
  by_group: EvalRow[]
  overlap: Record<string, number | boolean>
  overlap_greedy?: Record<string, number | boolean>
  labels_vs_ibm: Record<string, Record<string, { mule?: number; genuine?: number }>>
  load_test?: { complaints: number; bank_id: string; wall_seconds: number; duplicates: { total: number }; leaks: { total: number };
    liens_over_balance: unknown[]; latency_ms: Record<string, { n: number; p50: number; p95: number; max: number }> }
  cases: EvalCase[]
}

export interface LangfuseSummary {
  error?: string
  observations?: number
  generations?: number
  usd?: number
  agents?: Record<string, { generations: number; 'USD (Langfuse)': number }>
}
