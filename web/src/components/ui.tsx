import * as DialogP from '@radix-ui/react-dialog'
import * as TabsP from '@radix-ui/react-tabs'
import * as TooltipP from '@radix-ui/react-tooltip'
import { Loader2, X } from 'lucide-react'
import type { ButtonHTMLAttributes, CSSProperties, InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from 'react'
import { forwardRef, useEffect, useState } from 'react'
import { cn } from '@/lib/format'
import type { Tone } from '@/lib/status'

type BtnVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'quiet'
const BTN: Record<BtnVariant, string> = {
  primary: 'bg-brand-600 text-white shadow-card hover:bg-brand-700 active:shadow-press disabled:bg-brand-300',
  secondary: 'bg-card text-ink border border-rule-2 shadow-card hover:bg-brand-50 hover:border-brand-300 active:shadow-press disabled:text-ink-4',
  ghost: 'text-ink-2 hover:bg-brand-50 hover:text-ink disabled:text-ink-4',
  quiet: 'text-brand-700 hover:text-brand-800 hover:underline underline-offset-4 disabled:text-ink-4',
  danger: 'bg-card text-danger border border-rule-2 shadow-card hover:bg-danger-tint hover:border-danger/40 disabled:text-ink-4',
}

export const Button = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: BtnVariant; size?: 'sm' | 'md'; busy?: boolean; icon?: ReactNode
}>(function Button({ variant = 'secondary', size = 'md', busy, icon, className, children, disabled, ...rest }, ref) {
  return (
    <button ref={ref} disabled={disabled || busy}
      className={cn('inline-flex items-center justify-center gap-2 rounded-[var(--radius-control)] font-medium transition-colors',
        'disabled:cursor-not-allowed disabled:shadow-none',
        size === 'sm' ? 'h-8 px-3 text-[13px]' : 'h-9 px-3.5 text-[13.5px]', BTN[variant], className)} {...rest}>
      {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : icon}
      {children}
    </button>
  )
})

const TONE: Record<Tone, string> = {
  approved: 'bg-approved-tint text-approved', proposed: 'bg-proposed-tint text-proposed',
  rejected: 'bg-rejected-tint text-rejected', review: 'bg-review-tint text-review', victim: 'bg-victim-tint text-victim',
  none: 'bg-none-tint text-none', danger: 'bg-danger-tint text-danger', brand: 'bg-brand-100 text-brand-800',
}
const DOT: Record<Tone, string> = {
  approved: 'bg-approved', proposed: 'bg-proposed', rejected: 'bg-rejected', review: 'bg-review', victim: 'bg-victim',
  none: 'bg-none', danger: 'bg-danger', brand: 'bg-brand-600',
}

export function Badge({ tone = 'none', children, dot = true, pulse, className }:
  { tone?: Tone; children: ReactNode; dot?: boolean; pulse?: boolean; className?: string }) {
  return (
    <span className={cn('inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-[12px] font-medium', TONE[tone], className)}>
      {dot && <span className={cn('size-1.5 rounded-full', DOT[tone], pulse && 'animate-pulse-soft')} aria-hidden />}
      {children}
    </span>
  )
}

export function Panel({ title, subtitle, actions, children, className, bodyClass, id, raised }: {
  title?: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; bodyClass?: string
  id?: string; raised?: boolean
}) {
  return (
    <section id={id} className={cn('rounded-[var(--radius-panel)] bg-card', raised ? 'shadow-raised' : 'shadow-card', 'border border-rule/70', className)}>
      {(title || actions) && (
        <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 px-5 pt-4">
          <div className="min-w-0">
            {title && <h2 className="text-[15px] font-semibold leading-6 text-ink">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-[13px] text-ink-3">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cn('px-5 pb-5', title || actions ? 'pt-3' : 'pt-5', bodyClass)}>{children}</div>
    </section>
  )
}

/** A ledger line: labelled figures separated by ruled columns, like a bank statement's summary. */
export function Ledger({ items, className }: {
  items: { label: string; value: ReactNode; note?: ReactNode; tone?: 'ink' | 'brand' | 'muted' }[]; className?: string
}) {
  return (
    // one figure per row on narrow screens, one ruled row of figures from 768 px
    <dl className={cn('grid grid-cols-1 divide-y divide-rule rounded-[var(--radius-panel)] border border-rule/70 bg-card shadow-card md:grid-cols-[repeat(var(--n),minmax(0,1fr))] md:divide-x md:divide-y-0', className)}
      style={{ '--n': items.length } as CSSProperties}>
      {items.map(it => (
        <div key={it.label} className="min-w-0 px-4 py-3.5">
          <dt className="truncate text-[12.5px] text-ink-3">{it.label}</dt>
          <dd className={cn('num mt-1 truncate text-[20px] font-semibold leading-7',
            it.tone === 'brand' ? 'text-brand-700' : it.tone === 'muted' ? 'text-ink-3' : 'text-ink')}>{it.value}</dd>
          {it.note && <div className="mt-0.5 truncate text-[12px] text-ink-3">{it.note}</div>}
        </div>
      ))}
    </dl>
  )
}

export function Empty({ title, children, action, icon }: { title: string; children?: ReactNode; action?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-[var(--radius-panel)] border border-dashed border-rule-2 bg-paper/60 px-6 py-10 text-center">
      {icon && <div className="mb-3 text-brand-500">{icon}</div>}
      <p className="text-[14px] font-medium text-ink">{title}</p>
      {children && <div className="mt-1 max-w-md text-[13px] text-ink-3">{children}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return <div className="flex items-center gap-2 py-6 text-[13px] text-ink-3"><Loader2 className="size-4 animate-spin" />{label}</div>
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null
  const msg = error instanceof Error ? error.message : String(error)
  return <div role="alert" className="rounded-[var(--radius-control)] bg-danger-tint px-3 py-2 text-[13px] text-danger">{msg}</div>
}

export function Tip({ content, children, side = 'top' }: { content: ReactNode; children: ReactNode; side?: 'top' | 'bottom' | 'left' | 'right' }) {
  return (
    <TooltipP.Root delayDuration={250}>
      <TooltipP.Trigger asChild>{children}</TooltipP.Trigger>
      <TooltipP.Portal>
        <TooltipP.Content side={side} sideOffset={6}
          className="z-50 max-w-xs rounded-lg bg-ink px-2.5 py-1.5 text-[12.5px] leading-snug text-white shadow-float">
          {content}
        </TooltipP.Content>
      </TooltipP.Portal>
    </TooltipP.Root>
  )
}
export const TipProvider = TooltipP.Provider

export function Tabs({ value, onValueChange, tabs, children, className, listClass }: {
  value: string; onValueChange: (v: string) => void; tabs: { value: string; label: ReactNode; id?: string }[]
  children: ReactNode; className?: string; listClass?: string
}) {
  return (
    <TabsP.Root value={value} onValueChange={onValueChange} className={className}>
      <TabsP.List className={cn('flex gap-1 border-b border-rule', listClass)}>
        {tabs.map(t => (
          <TabsP.Trigger key={t.value} value={t.value} id={t.id}
            className="relative -mb-px inline-flex h-10 items-center gap-2 border-b-2 border-transparent px-3 text-[13.5px] font-medium text-ink-3 transition-colors hover:text-ink data-[state=active]:border-brand-600 data-[state=active]:text-ink">
            {t.label}
          </TabsP.Trigger>
        ))}
      </TabsP.List>
      {children}
    </TabsP.Root>
  )
}
export const TabPanel = ({ value, children, className }: { value: string; children: ReactNode; className?: string }) =>
  <TabsP.Content value={value} className={cn('pt-4 outline-none', className)}>{children}</TabsP.Content>

export function Segmented<T extends string>({ value, onChange, options, size = 'md', className }: {
  value: T; onChange: (v: T) => void; options: { value: T; label: ReactNode }[]; size?: 'sm' | 'md'; className?: string
}) {
  return (
    <div role="radiogroup" className={cn('inline-flex rounded-[var(--radius-control)] bg-paper-2 p-0.5', className)}>
      {options.map(o => (
        <button key={o.value} role="radio" aria-checked={value === o.value} onClick={() => onChange(o.value)}
          className={cn('rounded-[6px] font-medium transition-colors', size === 'sm' ? 'h-7 px-2.5 text-[12.5px]' : 'h-8 px-3 text-[13px]',
            value === o.value ? 'bg-card text-ink shadow-card' : 'text-ink-3 hover:text-ink')}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

export function Modal({ open, onOpenChange, title, description, children, width = 'max-w-lg' }: {
  open: boolean; onOpenChange: (v: boolean) => void; title: string; description?: ReactNode; children: ReactNode; width?: string
}) {
  return (
    <DialogP.Root open={open} onOpenChange={onOpenChange}>
      <DialogP.Portal>
        <DialogP.Overlay className="fixed inset-0 z-40 bg-ink/25" />
        <DialogP.Content className={cn('fixed left-1/2 top-[8vh] z-50 max-h-[84vh] w-[calc(100vw-32px)] -translate-x-1/2 overflow-y-auto rounded-[var(--radius-panel)] bg-card p-6 shadow-float scroll-thin', width)}>
          <div className="flex items-start justify-between gap-4">
            <div>
              <DialogP.Title className="text-[17px] font-semibold text-ink">{title}</DialogP.Title>
              {description && <DialogP.Description className="mt-1 text-[13px] text-ink-3">{description}</DialogP.Description>}
            </div>
            <DialogP.Close className="rounded-md p-1 text-ink-3 hover:bg-paper-2 hover:text-ink" aria-label="Close"><X className="size-4" /></DialogP.Close>
          </div>
          <div className="mt-5">{children}</div>
        </DialogP.Content>
      </DialogP.Portal>
    </DialogP.Root>
  )
}

const FIELD = 'w-full rounded-[var(--radius-control)] border border-rule-2 bg-card px-3 text-[13.5px] text-ink placeholder:text-ink-4 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200 disabled:bg-paper disabled:text-ink-4'

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...p }, ref) {
  return <input ref={ref} className={cn(FIELD, 'h-9', className)} {...p} />
})
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea({ className, ...p }, ref) {
  return <textarea ref={ref} className={cn(FIELD, 'min-h-20 py-2 leading-snug', className)} {...p} />
})
export function Select({ className, children, ...p }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={cn(FIELD, 'h-9 pr-8', className)} {...p}>{children}</select>
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block">
      <span className="text-[13px] font-medium text-ink">{label}</span>
      <div className="mt-1.5">{children}</div>
      {hint && <span className="mt-1 block text-[12px] text-ink-3">{hint}</span>}
    </label>
  )
}

/** A progress meter (spend against its cap, a lesson's confidence). */
export function Meter({ value, max = 1, tone = 'brand', className, label }: { value: number; max?: number; tone?: 'brand' | 'review' | 'danger'; className?: string; label?: string }) {
  const w = Math.max(0, Math.min(1, max ? value / max : 0))
  return (
    <div className={cn('h-1.5 w-full overflow-hidden rounded-full bg-paper-2', className)} role="meter" aria-valuenow={value} aria-valuemin={0} aria-valuemax={max} aria-label={label}>
      <div className={cn('h-full rounded-full', tone === 'brand' ? 'bg-brand-500' : tone === 'review' ? 'bg-review' : 'bg-danger')} style={{ width: `${w * 100}%` }} />
    </div>
  )
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn('id text-ink', className)}>{children}</span>
}

export function useMedia(query: string) {
  const [m, setM] = useState(() => typeof window !== 'undefined' && window.matchMedia(query).matches)
  useEffect(() => {
    const mq = window.matchMedia(query)
    const on = () => setM(mq.matches)
    mq.addEventListener('change', on)
    on()
    return () => mq.removeEventListener('change', on)
  }, [query])
  return m
}

export function SectionTitle({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return <div className="mb-3 flex items-end justify-between gap-4"><h3 className="text-[14px] font-semibold text-ink">{children}</h3>{aside}</div>
}
