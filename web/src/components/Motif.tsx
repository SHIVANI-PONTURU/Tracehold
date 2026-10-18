import { cn } from '@/lib/format'

// Quiet line art for a hero card's corner, in the page's pale greens: no fill gradients, nothing a reader needs.
// The card that holds it needs `relative isolate overflow-hidden`; the motif sits behind the card's content.

const NODES: [number, number][] = [[24, 118], [70, 84], [112, 120], [150, 62], [196, 96], [236, 44], [250, 132], [104, 30], [196, 18]]
const EDGES: [number, number][] = [[0, 1], [1, 2], [1, 3], [3, 4], [4, 5], [4, 6], [3, 7], [5, 8], [2, 4], [7, 8]]
const TRAIL = new Set(['0-1', '1-3', '3-4', '4-5'])   // a path through the network, drawn a shade darker

export function NetworkMotif({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 280 160" className={cn('pointer-events-none absolute -z-10', className)} aria-hidden>
      {EDGES.map(([a, b]) => {
        const on = TRAIL.has(`${a}-${b}`)
        return <line key={`${a}-${b}`} x1={NODES[a][0]} y1={NODES[a][1]} x2={NODES[b][0]} y2={NODES[b][1]}
          stroke={on ? 'var(--color-brand-300)' : 'var(--color-brand-100)'} strokeWidth={on ? 2 : 1.5} strokeLinecap="round" />
      })}
      {NODES.map(([x, y], i) => {
        const on = [0, 1, 3, 4, 5].includes(i)
        return <circle key={i} cx={x} cy={y} r={on ? 5 : 4} fill={on ? 'var(--color-brand-100)' : 'var(--color-card)'}
          stroke={on ? 'var(--color-brand-300)' : 'var(--color-brand-200)'} strokeWidth={1.5} />
      })}
    </svg>
  )
}

export function RingsMotif({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 220 180" className={cn('pointer-events-none absolute -z-10', className)} aria-hidden>
      {[36, 64, 92, 120, 148].map((r, i) => (
        <circle key={r} cx={196} cy={-6} r={r} fill="none" stroke={i % 2 ? 'var(--color-brand-100)' : 'var(--color-paper-2)'} strokeWidth={1.5} />
      ))}
    </svg>
  )
}
