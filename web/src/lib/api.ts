import type {
  Activity, Bank, Call, Complaint, Detail, EvalFile, Evaluation, Gate, GraphStats, Investigation, LangfuseSummary,
  Memory, Message, Review, Spend, Trail, UiConfig,
} from './types'

export class ApiError extends Error {
  status: number
  code: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

type Params = Record<string, string | number | undefined | null>

function query(params?: Params) {
  if (!params) return ''
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') q.set(k, String(v))
  const s = q.toString()
  return s ? `?${s}` : ''
}

async function request<T>(method: 'GET' | 'POST', path: string, params?: Params, body?: unknown): Promise<T> {
  const r = await fetch(path + query(params), {
    method,
    headers: body !== undefined ? { 'content-type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  const text = await r.text()
  let data: unknown = null
  try { data = text ? JSON.parse(text) : null } catch { data = null }
  if (!r.ok) {
    const e = (data as { error?: { code?: string; message?: string } } | null)?.error
    throw new ApiError(r.status, e?.code ?? 'http_error', e?.message ?? (text.slice(0, 200) || `HTTP ${r.status}`))
  }
  return data as T
}

const H = '/api/h'
const get = <T,>(path: string, params?: Params) => request<T>('GET', path, params)
const post = <T,>(path: string, body: unknown) => request<T>('POST', path, undefined, body)

export const api = {
  config: () => get<UiConfig>('/api/ui/config'),
  health: () => get<{ ok: boolean; tool_server: boolean; web_built: boolean }>('/api/ui/health'),
  banks: (ids: string[]) => get<Bank[]>(`${H}/banks`, { ids: ids.join(',') }),
  complaints: (bank: string) => get<Complaint[]>(`${H}/complaints`, { bank_id: bank }),
  investigations: (bank: string) => get<Investigation[]>(`${H}/investigations`, { bank_id: bank }),
  detail: (bank: string, inv: string) => get<Detail>(`${H}/investigations/${bank}/${inv}`),
  trail: (bank: string, inv: string) => get<Trail>(`${H}/trail/${bank}/${inv}`),
  gate: (bank: string, inv: string) => get<Gate>(`${H}/gate/${bank}/${inv}`),
  chat: (bank: string, inv: string) => get<Message[]>(`${H}/chat/${bank}/${inv}`),
  reviews: (bank: string, status: string) => get<Review[]>(`${H}/reviews`, { bank_id: bank, status }),
  memory: (bank: string) => get<Memory>(`${H}/memory`, { bank_id: bank }),
  spend: (inv?: string) => get<Spend>(`${H}/spend`, { investigation_id: inv }),
  calls: (inv?: string, limit = 5000) => get<Call[]>(`${H}/spend/calls`, { investigation_id: inv, limit }),
  activity: (since: number, source?: string, inv?: string, limit = 200) =>
    get<Activity>(`${H}/graph/activity`, { since, source, investigation_id: inv, limit }),
  graphStats: (banks: string[]) => get<GraphStats>(`${H}/graph/stats`, { banks: banks.join(',') }),
  evaluations: () => get<EvalFile[]>('/api/ui/evaluations'),
  evaluation: (file?: string) => get<Evaluation>('/api/ui/evaluation', { file }),
  langfuse: (traceId: string) => get<LangfuseSummary>(`/api/ui/langfuse/${traceId}`),
  blocked: () => get<{ available: boolean; requests: { agent: string; line: string }[]; read_at: number }>('/api/ui/blocked'),

  start: (bank: string, complaintId: string) =>
    post<{ investigation_id: string }>(`${H}/investigations`, { bank_id: bank, complaint_id: complaintId }),
  gateDecide: (bank: string, inv: string, decisions: { decision_id: string; approve: boolean; reason: string }[], mode: string) =>
    post<unknown>(`${H}/gate/decide`, { bank_id: bank, investigation_id: inv, decisions, gate_mode: mode }),
  undo: (bank: string, inv: string, actionId: string, reason: string) =>
    post<unknown>(`${H}/actions/undo`, { bank_id: bank, investigation_id: inv, action_id: actionId, reason }),
  resolveReview: (bank: string, actionId: string, outcome: 'cleared' | 'escalated', note: string) =>
    post<{ resolved: boolean; lessons: { id: string; before?: number; after?: number; confidence?: number }[] }>(
      `${H}/reviews/resolve`, { bank_id: bank, action_id: actionId, outcome, note }),
  ask: (bank: string, inv: string, text: string) =>
    post<{ message_id: string }>(`${H}/chat/ask`, { bank_id: bank, investigation_id: inv, text }),
  close: (bank: string, inv: string, reason: string) =>
    post<{ closed: boolean }>(`${H}/close`, { bank_id: bank, investigation_id: inv, reason }),
}

export function errorText(e: unknown) {
  if (e instanceof ApiError) return e.message
  if (e instanceof Error) return e.message
  return String(e)
}
