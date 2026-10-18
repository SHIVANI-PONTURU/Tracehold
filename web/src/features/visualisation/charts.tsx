import { type ReactNode, useEffect, useRef, useState } from 'react'
import { short } from '@/lib/format'

/** Categorical series in the validated order (dataviz validator, light mode, white surface: every adjacent pair passes
 *  the colour-blind and normal-vision checks). Two of them sit below 3:1 contrast, so every chart also carries a
 *  legend and direct labels or a table. */
export const SERIES = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100']

/** Methods keep one colour each everywhere (TraceHold on the green slot, then the order that validated). */
export const METHOD_COLOR: Record<string, string> = { tracehold: '#1baf7a', full_freeze: '#eb6834', greedy: '#2a78d6', rules_only: '#eda100' }
export const METHOD_NAME: Record<string, string> = { tracehold: 'TraceHold', full_freeze: 'Full freeze', greedy: 'Greedy trace', rules_only: 'Rules only' }
export const METHODS = ['tracehold', 'full_freeze', 'greedy', 'rules_only'] as const
export const GROUP_NAME: Record<string, string> = { shallow: 'Shallow (1-3 hops)', medium: 'Medium (4-6)', deep: 'Deep (7+)', held_out: 'Held-out CYCLE', all_buckets: 'All depth buckets' }

export const AXIS = { tick: { fill: 'var(--color-ink-3)', fontSize: 12 }, stroke: 'var(--color-rule-2)' }

type TipItem = { name?: ReactNode; value?: unknown; color?: string; dataKey?: unknown; payload?: Record<string, unknown> }

/** A tooltip in the app's style: text in ink, a coloured swatch beside each value carries the identity. */
export function ChartTip({ active, payload, label, format, labelFormat, footer }: {
  active?: boolean; payload?: TipItem[]; label?: unknown; format?: (v: unknown, item: TipItem) => ReactNode
  labelFormat?: (l: unknown, payload?: TipItem[]) => ReactNode; footer?: (payload: TipItem[]) => ReactNode
}) {
  if (!active || !payload?.length) return null
  const items = payload.filter(p => p.value !== undefined && p.value !== null)
  return (
    <div className="min-w-44 rounded-[10px] border border-rule bg-card px-3 py-2 text-[12.5px] shadow-float">
      {label !== undefined && <div className="mb-1 font-medium text-ink">{labelFormat ? labelFormat(label, payload) : String(label)}</div>}
      <ul className="space-y-0.5">
        {items.map((p, i) => (
          <li key={i} className="flex items-center justify-between gap-4">
            <span className="flex items-center gap-1.5 text-ink-2"><span className="size-2.5 rounded-[3px]" style={{ background: p.color }} />{p.name}</span>
            <span className="num font-medium text-ink">{format ? format(p.value, p) : String(p.value)}</span>
          </li>
        ))}
      </ul>
      {footer && <div className="mt-1.5 border-t border-rule pt-1.5 text-ink-3">{footer(payload)}</div>}
    </div>
  )
}

export function LegendRow({ items }: { items: { key: string; label: ReactNode; color: string }[] }) {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[12.5px] text-ink-2">
      {items.map(i => <li key={i.key} className="inline-flex items-center gap-1.5"><span className="size-2.5 rounded-[3px]" style={{ background: i.color }} />{i.label}</li>)}
    </ul>
  )
}

/** Rupee ticks on a log axis: ₹10K, ₹1 L, ₹10 L, ₹1 Cr and so on. */
function rupeeTick(r: number) {
  if (r >= 1e7) return `₹${(r / 1e7).toLocaleString('en-IN')} Cr`
  if (r >= 1e5) return `₹${r / 1e5} L`
  if (r >= 1e3) return `₹${r / 1e3}K`
  return `₹${r}`
}

type DotRow = { key: string; label: string; values: Record<string, number | null> }
type DotSeries = { key: string; label: string; color: string; labelled?: boolean }

/** A dot plot on a log axis: one row per group, one dot per series, a thin rule across each row's spread.
 *  Bars would imply a zero baseline that a log axis does not have; dots compare positions honestly.
 *  Values are paise; a zero sits hollow at the axis start. The labelled series carries its value beside its dot. */
export function DotPlot({ rows, series, ariaLabel }: { rows: DotRow[]; series: DotSeries[]; ariaLabel: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const [w, setW] = useState(640)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => setW(Math.max(320, e.contentRect.width)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const [hover, setHover] = useState<{ row: DotRow; s: string; x: number; y: number } | null>(null)
  const LEFT = 132, RIGHT = 20, TOP = 14, ROW = 56, AXIS = 26
  const rupees = rows.flatMap(r => Object.values(r.values)).filter((v): v is number => v != null && v > 0).map(v => v / 100)
  const lo = rupees.length ? 10 ** Math.floor(Math.log10(Math.min(...rupees))) : 1e4
  const hi = rupees.length ? 10 ** Math.ceil(Math.log10(Math.max(...rupees))) : 1e8
  const span = Math.max(1, Math.log10(hi) - Math.log10(lo))
  const x = (paise: number) => LEFT + ((Math.log10(Math.max(paise / 100, lo)) - Math.log10(lo)) / span) * (w - LEFT - RIGHT)
  const ticks: number[] = []
  for (let t = lo; t <= hi * 1.0001; t *= 10) ticks.push(t)
  const plotH = rows.length * ROW
  return (
    <div ref={ref} className="relative">
      <svg width={w} height={TOP + plotH + AXIS} role="img" aria-label={ariaLabel} className="block overflow-visible">
        {ticks.map((t, i) => {
          const tx = LEFT + ((Math.log10(t) - Math.log10(lo)) / span) * (w - LEFT - RIGHT)
          // A label such as "₹1,00,000 Cr" needs about 64 px; when the gridlines are closer, label every other one.
          const every = (w - LEFT - RIGHT) / Math.max(1, ticks.length - 1) < 64 ? 2 : 1
          return (
            <g key={t}>
              <line x1={tx} x2={tx} y1={TOP - 6} y2={TOP + plotH} stroke="var(--color-rule)" />
              {i % every === 0 && <text x={tx} y={TOP + plotH + 18} textAnchor="middle" fontSize={11.5} fill="var(--color-ink-3)">{rupeeTick(t)}</text>}
            </g>
          )
        })}
        {rows.map((r, i) => {
          const cy = TOP + i * ROW + ROW / 2
          const present = series.filter(s => r.values[s.key] != null)
          const xs = present.map(s => x(r.values[s.key]!))
          // The labelled series keeps the row's centre line; a dot that would sit on top of one already placed moves
          // down a step, so every method stays visible when their figures coincide. The labelled one is drawn last.
          const placed: number[] = []
          const dots = [...present.filter(s => s.labelled), ...present.filter(s => !s.labelled)].map(s => {
            const cx = x(r.values[s.key]!)
            const dy = placed.filter(px => Math.abs(px - cx) < 11).length * 8
            placed.push(cx)
            return { s, v: r.values[s.key]!, cx, dy }
          }).reverse()
          return (
            <g key={r.key}>
              <text x={0} y={cy} dominantBaseline="middle" fontSize={12.5} fill="var(--color-ink-2)">{r.label}</text>
              {xs.length > 1 && <line x1={Math.min(...xs)} x2={Math.max(...xs)} y1={cy} y2={cy} stroke="var(--color-rule-2)" strokeWidth={2} strokeLinecap="round" />}
              {dots.map(({ s, v, cx, dy }) => {
                return (
                  <g key={s.key}>
                    <circle cx={cx} cy={cy + dy} r={6.5} fill={v > 0 ? s.color : '#fff'} stroke={v > 0 ? '#fff' : s.color} strokeWidth={2} />
                    {s.labelled && <text x={cx} y={cy - 13} textAnchor="middle" fontSize={11.5} fontWeight={600} fill="var(--color-ink)">{v > 0 ? short(v) : 'none'}</text>}
                    <circle cx={cx} cy={cy + dy} r={dy ? 6 : 13} fill="transparent" style={{ cursor: 'default' }}
                      onMouseEnter={() => setHover({ row: r, s: s.key, x: cx, y: cy + dy })} onMouseLeave={() => setHover(h => (h?.s === s.key && h.row.key === r.key ? null : h))} />
                  </g>
                )
              })}
            </g>
          )
        })}
      </svg>
      {hover && (
        <div className="pointer-events-none absolute z-10 min-w-48 rounded-[10px] border border-rule bg-card px-3 py-2 text-[12.5px] shadow-float"
          style={{ left: Math.min(hover.x + 14, w - 210), top: hover.y + 12 }}>
          <div className="mb-1 font-medium text-ink">{hover.row.label}</div>
          <ul className="space-y-0.5">
            {series.map(s => (
              <li key={s.key} className={`flex items-center justify-between gap-4 ${s.key === hover.s ? 'font-semibold' : ''}`}>
                <span className="flex items-center gap-1.5 text-ink-2"><span className="size-2.5 rounded-[3px]" style={{ background: s.color }} />{s.label}</span>
                <span className="num text-ink">{hover.row.values[s.key] == null ? '-' : hover.row.values[s.key] ? short(hover.row.values[s.key]) : 'none'}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
