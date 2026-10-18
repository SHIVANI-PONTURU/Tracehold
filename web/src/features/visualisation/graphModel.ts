import type { Step, Trail } from '@/lib/types'

/** What each tool asks FalkorDB (the real queries from mcp_server/network.py and tools.py, shortened). */
export const TOOL_QUERY: Record<string, { graph: 'network' | 'memory' | 'scratch' | 'model'; does: string; cypher: string }> = {
  trace_funds: { graph: 'network', does: 'Next-layer accounts: transfers out of each account after the money arrived',
    cypher: 'UNWIND $starts AS s MATCH (a:Account {id: s.id}) WITH s, a\nMATCH (a)-[t:TRANSFERRED]->(b:Account)\nWITH s, a, t, b WHERE t.ts >= s.after AND t.self_loop = false\nWITH a.id AS src, t, b ORDER BY t.amount DESC\nRETURN src, count(t), sum(t.amount), collect([t.id, b.id, t.amount, t.ts])[0..$top]' },
  compute_taint: { graph: 'network', does: 'Every transfer in and out of the traced accounts, for the pro-rata taint engine',
    cypher: 'UNWIND $ids AS id MATCH (x:Account {id: id}) WITH x\nMATCH (x)-[t:TRANSFERRED]-(y:Account)\nRETURN t.id, startNode(t).id, endNode(t).id, t.amount, t.ts, t.format' },
  account_signals: { graph: 'network', does: 'The signal record: age, payers, devices and phones shared with known mules',
    cypher: 'UNWIND $ids AS id MATCH (a:Account {id: id}) WITH a\nMATCH (c:Customer)-[:OWNS]->(a)-[:HELD_AT]->(b:Bank)\nOPTIONAL MATCH (a)-[:USED_DEVICE]->(d:Device)<-[:USED_DEVICE]-(m:Account)\nRETURN a, c, b, collect(DISTINCT d.id), count(DISTINCT m)' },
  shared_attribute_cluster: { graph: 'network', does: 'Accounts sharing a device, a phone or an IP with this one',
    cypher: 'MATCH (a:Account {id: $id})-[:USED_DEVICE]->(d:Device)<-[:USED_DEVICE]-(b:Account)\nRETURN d.id, collect(b.id)\n// and the same through (:Customer)-[:HAS_PHONE]->(:Phone) and [:LOGGED_IN_FROM]->(:IP)' },
  mule_community: { graph: 'scratch', does: 'Communities and components over shared identifiers, on a scratch graph',
    cypher: "CALL algo.labelPropagation({nodeLabels: ['Account'], relationshipTypes: ['SHARES']})\nYIELD node, communityId\nCALL algo.WCC({nodeLabels: ['Account'], relationshipTypes: ['SHARES', 'TRANSFERRED']})" },
  key_accounts: { graph: 'scratch', does: 'Which accounts hold a community together (betweenness)',
    cypher: "CALL algo.betweenness({nodeLabels: ['Account'], relationshipTypes: ['SHARES', 'TRANSFERRED'], samplingSeed: 42})\nYIELD node, score RETURN node.id, score ORDER BY score DESC" },
  recall_context: { graph: 'memory', does: 'This bank\'s earlier cases, decisions and lessons touching these accounts, devices and phones',
    cypher: 'UNWIND $ids AS eid MATCH (e:EntityRef {id: eid}) WITH e\nMATCH (i:Investigation)-[:INVOLVES]->(e)\nOPTIONAL MATCH (l:Lesson)-[:APPLIES_TO]->(e) WHERE l.active\nRETURN i, collect(DISTINCT l)' },
  propose_lien: { graph: 'memory', does: 'Writes a lien decision; the amount comes from code, never the model',
    cypher: 'MATCH (i:Investigation {id: $inv})\nMERGE (d:Decision {id: $decision_id})\nSET d.account_id = $account, d.lien_amount = $amount, d.label = $label\nMERGE (i)-[:DECIDED]->(d)' },
  open_review: { graph: 'memory', does: 'Opens a review for a person', cypher: 'MATCH (i:Investigation {id: $inv})\nMERGE (a:Action {id: $id}) SET a.kind = \'review\', a.status = \'open\'\nMERGE (i)-[:TOOK]->(a)' },
  request_info: { graph: 'memory', does: 'Asks the account holder for documents', cypher: 'MERGE (a:Action {id: $id}) SET a.kind = \'info_request\'' },
  flag_entity: { graph: 'memory', does: 'Flags a shared device or phone', cypher: 'MERGE (a:Action {id: $id}) SET a.kind = \'flag\'' },
  model: { graph: 'model', does: 'Claude Opus 4.6 on Amazon Bedrock decides; no graph query', cypher: '' },
}

export function stepTools(s: Step) {
  return (s.tool || '').split(',').map(t => t.trim().replace(/x\d+$/, '')).filter(t => t && t !== '-')
}

export type Phase = 'trace' | 'adjudicate' | 'verify' | 'gate' | 'done'

export function phaseOf(steps: Step[], k: number): Phase {
  const s = steps[Math.min(k, steps.length - 1)]
  if (!s) return 'trace'
  if (k >= steps.length) return 'done'
  if (s.agent === 'tracer') return 'trace'
  if (s.agent === 'adjudicator') return 'adjudicate'
  if (s.agent === 'verifier') return s.node === 'gate' || s.node === 'execute' ? 'gate' : 'verify'
  return 'gate'
}

/** How far the trail has grown after `k` steps: one layer per Tracer judgement, all of it once the Tracer is done. */
export function layerAt(steps: Step[], k: number, maxLayer: number) {
  const done = steps.slice(0, k)
  if (done.some(s => s.agent !== 'tracer') || k >= steps.length) return maxLayer
  const judged = done.filter(s => s.agent === 'tracer' && s.node === 'judge').length
  return Math.min(maxLayer, Math.max(0, judged))
}

/** The trail as it stood in a phase: labels appear with the Adjudicator, lien outcomes after the gate. */
export function maskTrail(trail: Trail, phase: Phase): Trail {
  if (phase === 'done' || phase === 'gate') return trail
  return {
    edges: trail.edges,
    nodes: trail.nodes.map(n => n.role === 'victim' ? n : {
      ...n, approved: null,
      label: phase === 'trace' ? null : n.label, action: phase === 'trace' ? null : n.action,
    }),
  }
}
