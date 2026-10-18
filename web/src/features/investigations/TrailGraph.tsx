import {
  Background, BaseEdge, Controls, type Edge, EdgeLabelRenderer, type EdgeProps, getBezierPath, Handle, type Node,
  type NodeProps, Position, ReactFlow, ReactFlowProvider, useReactFlow, useStore,
} from '@xyflow/react'
import { History, Maximize } from 'lucide-react'
import { memo, useCallback, useEffect, useMemo, useState } from 'react'
import { cn, short } from '@/lib/format'
import { LABEL_INFO, nodeState, type NodeState } from '@/lib/status'
import type { Trail, TrailNode } from '@/lib/types'

const COL = 330
const ROW = 98
const NODE_W = 214   // the card's width (w-[214px])
const NODE_H = 102   // the tallest card

const STRIP: Record<NodeState, string> = {
  victim: 'bg-victim', 'lien approved': 'bg-approved', 'lien rejected': 'bg-rejected', 'lien proposed': 'bg-proposed',
  review: 'bg-review', none: 'bg-none',
}
const PILL: Record<NodeState, string> = {
  victim: 'bg-victim-tint text-victim', 'lien approved': 'bg-approved-tint text-approved', 'lien rejected': 'bg-rejected-tint text-rejected',
  'lien proposed': 'bg-proposed-tint text-proposed', review: 'bg-review-tint text-review', none: 'bg-none-tint text-none',
}
const STATE_TEXT: Record<NodeState, string> = {
  victim: 'Victim', 'lien approved': 'Lien approved', 'lien rejected': 'Lien rejected', 'lien proposed': 'Lien proposed',
  review: 'Sent to review', none: 'No action',
}

type AccountData = { n: TrailNode; state: NodeState; selected: boolean; fresh: boolean; dim: boolean }

const AccountNode = memo(function AccountNode({ data }: NodeProps<Node<AccountData>>) {
  const { n, state, selected, fresh, dim } = data
  const victim = n.role === 'victim'
  return (
    <div className={cn('relative w-[214px] overflow-hidden rounded-[11px] border bg-card text-left transition-all duration-500',
      selected ? 'border-brand-600 shadow-raised ring-2 ring-brand-200' : 'border-rule shadow-card',
      fresh && 'animate-[node-in_0.6s_ease-out]', dim && 'opacity-35')}>
      <span className={cn('absolute inset-y-0 left-0 w-1', STRIP[state])} aria-hidden />
      <Handle type="target" position={Position.Left} className="!size-2 !border-0 !bg-rule-2" />
      <Handle type="source" position={Position.Right} className="!size-2 !border-0 !bg-rule-2" />
      <div className="py-2 pl-3.5 pr-2.5">
        <div className="flex items-center justify-between gap-2">
          <span className="id truncate text-[12px] font-medium text-ink">{n.id}</span>
          <span className="shrink-0 rounded bg-paper-2 px-1.5 text-[11px] font-medium text-ink-3">{victim ? 'Victim' : `L${n.layer}`}</span>
        </div>
        <div className="mt-1 flex items-baseline justify-between gap-2">
          <span className="num text-[14px] font-semibold text-ink">{victim ? short(n.disputed_paise) : short(n.tainted_paise)}</span>
          <span className="text-[11.5px] text-ink-3">{victim ? 'disputed' : 'victim\'s money held'}</span>
        </div>
        <div className="mt-1.5 flex items-center gap-1.5">
          <span className={cn('whitespace-nowrap rounded-full px-1.5 py-px text-[11px] font-medium', PILL[state])}>{STATE_TEXT[state]}</span>
          {n.label && <span className="truncate text-[11px] text-ink-3">{LABEL_INFO[n.label]?.label}</span>}
          {n.seen_before && <History className="ml-auto size-3.5 shrink-0 text-brand-600" aria-label="An earlier case at this bank touched this account" />}
        </div>
      </div>
    </div>
  )
})

// Where an edge's amount sits: beside the target when it is the target's only incoming edge (a fan-out), beside the
// source when it is the source's only outgoing edge (a fan-in), otherwise mid-curve. Midpoint labels of a fan-out bunch
// together and end up beside the wrong card.
type LabelAt = 'target' | 'source' | 'mid'
type MoneyData = { amount: number; transfers: number; flow: boolean; dim: boolean; at: LabelAt }

const MoneyEdge = memo(function MoneyEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data }: EdgeProps<Edge<MoneyData>>) {
  const [path, lx, ly] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition })
  const flow = !!data?.flow
  const at = data?.at ?? 'mid'
  return (
    <>
      <BaseEdge id={id} path={path} style={{ stroke: flow ? 'var(--color-brand-500)' : 'var(--color-rule-2)', strokeWidth: flow ? 2 : 1.5, opacity: data?.dim ? 0.3 : 1 }}
        className={flow ? 'edge-flow' : undefined} />
      {flow && [0, 0.55, 1.1].map(d => (
        <circle key={d} r={4} fill="var(--color-brand-600)" stroke="#fff" strokeWidth={1.5}>
          <animateMotion dur="1.65s" begin={`${d}s`} repeatCount="indefinite" path={path} />
        </circle>
      ))}
      <EdgeLabelRenderer>
        <div style={{ transform: at === 'target' ? `translate(-100%, -50%) translate(${targetX - 8}px, ${targetY}px)`
            : at === 'source' ? `translate(0, -50%) translate(${sourceX + 8}px, ${sourceY}px)`
            : `translate(-50%, -50%) translate(${lx}px, ${ly}px)`, opacity: data?.dim ? 0.35 : 1 }}
          className="nodrag nopan pointer-events-none absolute rounded-md border border-rule bg-card px-1.5 py-px text-[11px] font-medium text-ink-2 shadow-card num">
          {short(data?.amount)}{(data?.transfers ?? 1) > 1 ? ` in ${data?.transfers}` : ''}
        </div>
      </EdgeLabelRenderer>
    </>
  )
})

const nodeTypes = { account: AccountNode }
const edgeTypes = { money: MoneyEdge }

export function layout(trail: Trail) {
  const byLayer = new Map<number, TrailNode[]>()
  for (const n of trail.nodes) byLayer.set(n.layer, [...(byLayer.get(n.layer) ?? []), n])
  const tallest = Math.max(1, ...[...byLayer.values()].map(v => v.length))
  const pos = new Map<string, { x: number; y: number }>()
  for (const [layer, ns] of byLayer) {
    ns.sort((a, b) => (b.tainted_paise ?? 0) - (a.tainted_paise ?? 0))
    const off = ((tallest - ns.length) * ROW) / 2
    ns.forEach((n, i) => pos.set(n.id, { x: layer * COL, y: off + i * ROW }))
  }
  return pos
}

const PAD = 28          // px kept clear around the trail
const MAX_ZOOM = 1.05
const READABLE = 0.75   // below this the cards' text is too small to read comfortably
const PAN_ZOOM = 0.85   // used when the whole trail does not fit at a readable size

type Box = { x: number; y: number; width: number; height: number; anchorY: number }

/** Fits the trail when it can be read whole. A trail too wide or tall for that keeps a readable zoom and shows
 *  where the story starts: the victim on a case, or the newest layer in a replay (`follow`); the user pans for
 *  the rest, and the fit button in the corner still shows all of it. The box comes from the layout, not from
 *  measured nodes, so the view is set as soon as the pane has a size. */
function Fit({ box, follow, onClip }: { box: Box; follow: boolean; onClip: (clipped: boolean) => void }) {
  const rf = useReactFlow()
  const w = useStore(s => s.width)
  const h = useStore(s => s.height)
  const { x: bx, y: by, width: bw, height: bh, anchorY } = box
  useEffect(() => {
    if (!w || !h || !bw || !bh) return
    const t = setTimeout(() => {
      const fit = Math.min((w - 2 * PAD) / bw, (h - 2 * PAD) / bh, MAX_ZOOM)
      const z = fit >= READABLE ? fit : PAN_ZOOM
      const x = fit >= READABLE ? (w - bw * z) / 2 - bx * z : follow ? w - PAD - (bx + bw) * z : PAD - bx * z
      // Too tall: centre on the victim (or the newest layer) rather than show the top of a wide fan-out.
      const y = bh * z <= h - 2 * PAD ? (h - bh * z) / 2 - by * z : h / 2 - anchorY * z
      onClip(fit < READABLE)
      rf.setViewport({ x, y, zoom: z }, { duration: 400 })
    }, 30)
    return () => clearTimeout(t)
  }, [bx, by, bw, bh, anchorY, w, h, follow, rf, onClip])
  return null
}

/** The money trail, left to right by layer. `visibleLayer` and `flowLayer` drive the replay: layers past
 *  visibleLayer are hidden and the transfers into flowLayer carry moving money. */
export function TrailGraph({ trail, selected, onSelect, visibleLayer, flowLayer, height = 420, fresh, focusIds }: {
  trail: Trail; selected?: string | null; onSelect?: (id: string) => void; visibleLayer?: number; flowLayer?: number | null
  height?: number; fresh?: Set<string>; focusIds?: Set<string>
}) {
  const pos = useMemo(() => layout(trail), [trail])
  const layerOf = useMemo(() => new Map(trail.nodes.map(n => [n.id, n.layer])), [trail])
  const maxLayer = visibleLayer ?? Infinity
  const nodes: Node<AccountData>[] = useMemo(() => trail.nodes.filter(n => n.layer <= maxLayer).map(n => ({
    id: n.id, type: 'account', position: pos.get(n.id) ?? { x: 0, y: 0 }, draggable: false, connectable: false,
    data: { n, state: nodeState(n), selected: selected === n.id, fresh: !!fresh?.has(n.id), dim: !!focusIds && !focusIds.has(n.id) },
  })), [trail, pos, selected, maxLayer, fresh, focusIds])
  const edges: Edge<MoneyData>[] = useMemo(() => {
    const shown = trail.edges.filter(e => (layerOf.get(e.dst) ?? 0) <= maxLayer && (layerOf.get(e.src) ?? 0) <= maxLayer)
    const into = new Map<string, number>(), outOf = new Map<string, number>()
    for (const e of shown) { into.set(e.dst, (into.get(e.dst) ?? 0) + 1); outOf.set(e.src, (outOf.get(e.src) ?? 0) + 1) }
    return shown.map(e => ({
      id: `${e.src}->${e.dst}`, source: e.src, target: e.dst, type: 'money',
      data: { amount: e.amount_paise, transfers: e.transfers, flow: flowLayer != null && layerOf.get(e.dst) === flowLayer,
        dim: !!focusIds && !(focusIds.has(e.src) && focusIds.has(e.dst)),
        at: into.get(e.dst) === 1 ? 'target' : outOf.get(e.src) === 1 ? 'source' : 'mid' },
    }))
  }, [trail, layerOf, maxLayer, flowLayer, focusIds])
  const box = useMemo(() => {
    if (!nodes.length) return { x: 0, y: 0, width: 0, height: 0, anchorY: 0 }
    const xs = nodes.map(n => n.position.x), ys = nodes.map(n => n.position.y)
    const x = Math.min(...xs), y = Math.min(...ys)
    // The layer the view starts from: the victim's, or in a replay the newest one shown.
    const layers = nodes.map(n => n.data.n.layer)
    const at = visibleLayer != null ? Math.max(...layers) : Math.min(...layers)
    const ay = nodes.filter(n => n.data.n.layer === at).map(n => n.position.y + NODE_H / 2)
    return { x, y, width: Math.max(...xs) + NODE_W - x, height: Math.max(...ys) + NODE_H - y, anchorY: ay.reduce((a, b) => a + b, 0) / ay.length }
  }, [nodes, visibleLayer])
  const [clipped, setClipped] = useState(false)
  const onClip = useCallback((c: boolean) => setClipped(c), [])
  return (
    <div style={{ height }} className="relative overflow-hidden rounded-[12px] border border-rule bg-paper/60">
      <ReactFlowProvider>
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} minZoom={0.2} maxZoom={1.6}
          nodesDraggable={false} nodesConnectable={false} elementsSelectable={!!onSelect} proOptions={{ hideAttribution: true }}
          onNodeClick={onSelect ? (_, n) => onSelect(n.id) : undefined}
          onMoveStart={e => { if (e) setClipped(false) }}>
          <Background color="var(--color-rule)" gap={22} size={1.2} />
          <Controls showInteractive={false} position="bottom-right" fitViewOptions={{ padding: 0.08, duration: 400 }} />
          <Fit box={box} follow={visibleLayer != null} onClip={onClip} />
        </ReactFlow>
      </ReactFlowProvider>
      {clipped && (
        <p className="pointer-events-none absolute bottom-3 left-3 rounded-md border border-rule bg-card/95 px-2 py-1 text-[12px] text-ink-3 shadow-card">
          Drag to see the rest of the trail, or <Maximize className="inline size-3 align-[-1px]" aria-label="fit" /> to see it all.
        </p>
      )}
    </div>
  )
}

export function TrailLegend() {
  const items: NodeState[] = ['victim', 'lien approved', 'lien proposed', 'lien rejected', 'review', 'none']
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[12px] text-ink-3">
      {items.map(s => <span key={s} className="inline-flex items-center gap-1.5"><span className={cn('h-3 w-1 rounded-sm', STRIP[s])} />{STATE_TEXT[s]}</span>)}
      <span className="inline-flex items-center gap-1.5"><History className="size-3.5 text-brand-600" />Seen in an earlier case at this bank</span>
    </div>
  )
}
