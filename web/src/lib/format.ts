import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export const cn = (...v: ClassValue[]) => twMerge(clsx(v))

/** Indian digit grouping, to the paisa: ₹2,40,000.50 */
export function inr(paise: number | null | undefined, opts: { paise?: boolean } = {}) {
  if (paise === null || paise === undefined || Number.isNaN(paise)) return '-'
  const neg = paise < 0
  const abs = Math.abs(Math.round(paise))
  const rupees = Math.floor(abs / 100)
  const p = abs % 100
  let s = String(rupees)
  if (s.length > 3) {
    let head = s.slice(0, -3)
    const tail = s.slice(-3)
    const groups: string[] = []
    while (head.length > 2) { groups.unshift(head.slice(-2)); head = head.slice(0, -2) }
    s = [head, ...groups].filter(Boolean).join(',') + ',' + tail
  }
  const showPaise = opts.paise ?? true
  return `${neg ? '-' : ''}₹${s}${showPaise ? '.' + String(p).padStart(2, '0') : ''}`
}

/** Compact rupees: ₹5.47 L, ₹2.55 Cr, ₹950 */
export function short(paise: number | null | undefined) {
  const r = (paise ?? 0) / 100
  const a = Math.abs(r)
  if (a >= 1e7) return `₹${(r / 1e7).toLocaleString('en-IN', { maximumFractionDigits: a >= 1e9 ? 0 : 2 })} Cr`
  if (a >= 1e5) return `₹${(r / 1e5).toFixed(2)} L`
  return `₹${Math.round(r).toLocaleString('en-IN')}`
}

/** Rupees in lakh, for charts and the evaluation tables */
export const lakh = (paise: number) => paise / 1e7

export function usd(v: number | null | undefined, digits = 2) {
  if (v === null || v === undefined) return '-'
  if (v > 0 && v < 0.01 && digits <= 2) return `$${v.toFixed(4)}`
  return `$${v.toFixed(digits)}`
}

export const pct = (v: number | null | undefined, digits = 0) =>
  v === null || v === undefined ? '-' : `${(v * 100).toFixed(digits)}%`

export const num = (v: number | null | undefined) => (v === null || v === undefined ? '-' : v.toLocaleString('en-IN'))

export function when(ts: number | null | undefined) {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  return d.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

export function ago(ts: number | null | undefined, now = Date.now() / 1000) {
  if (!ts) return '-'
  const s = Math.max(0, Math.round(now - ts))
  if (s < 60) return `${s} s ago`
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} d ago`
}

export function duration(ms: number) {
  if (ms < 1000) return `${Math.round(ms)} ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`
  return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`
}

export const shortInv = (id: string) => id.replace(/^INV-(\d+)-(\w{6}).*$/, 'INV-$1-$2')

export const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`
