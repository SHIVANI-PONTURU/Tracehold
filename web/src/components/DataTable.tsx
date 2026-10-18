import {
  type ColumnDef, flexRender, getCoreRowModel, getFilteredRowModel, getPaginationRowModel, getSortedRowModel,
  type RowData, type SortingState, useReactTable,
} from '@tanstack/react-table'
import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, Search } from 'lucide-react'
import { type ReactNode, useState } from 'react'
import { cn } from '@/lib/format'

declare module '@tanstack/react-table' {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData extends RowData, TValue> {
    align?: 'left' | 'right' | 'center'
    width?: string
    wrap?: boolean
  }
}

export function DataTable<T>({ data, columns, search, pageSize = 12, onRowClick, rowId, selectedId, empty = 'Nothing here yet.',
  initialSort, toolbar, dense, className, maxHeight }: {
  data: T[]; columns: ColumnDef<T, any>[]; search?: string; pageSize?: number; onRowClick?: (row: T) => void
  rowId?: (row: T) => string; selectedId?: string | null; empty?: ReactNode; initialSort?: SortingState; toolbar?: ReactNode
  dense?: boolean; className?: string; maxHeight?: number
}) {
  const [sorting, setSorting] = useState<SortingState>(initialSort ?? [])
  const [filter, setFilter] = useState('')
  const table = useReactTable({
    data, columns, state: { sorting, globalFilter: filter }, onSortingChange: setSorting, onGlobalFilterChange: setFilter,
    getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel(), getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(), initialState: { pagination: { pageSize } },
    getRowId: rowId ? (r) => rowId(r) : undefined, autoResetPageIndex: false,
  })
  const rows = table.getRowModel().rows
  const pages = table.getPageCount()
  const filtered = table.getFilteredRowModel().rows.length
  return (
    <div className={cn('min-w-0', className)}>
      {(search || toolbar) && (
        <div className="mb-3 flex flex-wrap items-center gap-2">
          {search && (
            <label className="relative w-full max-w-xs">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-ink-4" aria-hidden />
              <input value={filter} onChange={e => { setFilter(e.target.value); table.setPageIndex(0) }} placeholder={search}
                aria-label={search}
                className="h-8 w-full rounded-[var(--radius-control)] border border-rule-2 bg-card pl-8 pr-3 text-[13px] text-ink placeholder:text-ink-4 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200" />
            </label>
          )}
          <div className="ml-auto flex items-center gap-2">{toolbar}</div>
        </div>
      )}
      <div className="overflow-auto rounded-[12px] border border-rule bg-card scroll-thin" style={maxHeight ? { maxHeight } : undefined}>
        <table className="w-full border-collapse text-left">
          <thead className="sticky top-0 z-10 bg-paper-2">
            {table.getHeaderGroups().map(hg => (
              <tr key={hg.id}>
                {hg.headers.map(h => {
                  const meta = h.column.columnDef.meta
                  const sort = h.column.getIsSorted()
                  const can = h.column.getCanSort()
                  return (
                    <th key={h.id} scope="col" style={meta?.width ? { width: meta.width } : undefined}
                      className={cn('whitespace-nowrap border-b border-rule px-3 text-[12.5px] font-medium text-ink-3', dense ? 'h-8' : 'h-9',
                        meta?.align === 'right' && 'text-right', meta?.align === 'center' && 'text-center')}
                      aria-sort={sort === 'asc' ? 'ascending' : sort === 'desc' ? 'descending' : undefined}>
                      {h.isPlaceholder ? null : can ? (
                        <button onClick={h.column.getToggleSortingHandler()}
                          className={cn('inline-flex items-center gap-1 hover:text-ink', meta?.align === 'right' && 'flex-row-reverse')}>
                          {flexRender(h.column.columnDef.header, h.getContext())}
                          {sort === 'asc' ? <ArrowUp className="size-3" /> : sort === 'desc' ? <ArrowDown className="size-3" /> : <span className="size-3" />}
                        </button>
                      ) : flexRender(h.column.columnDef.header, h.getContext())}
                    </th>
                  )
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={columns.length} className="px-3 py-8 text-center text-[13px] text-ink-3">{filter ? 'No row matches the search.' : empty}</td></tr>
            )}
            {rows.map(r => {
              const id = rowId ? rowId(r.original) : r.id
              const selected = selectedId != null && id === selectedId
              return (
                <tr key={r.id} onClick={onRowClick ? () => onRowClick(r.original) : undefined}
                  className={cn('border-b border-rule/70 last:border-b-0 transition-colors', onRowClick && 'cursor-pointer',
                    selected ? 'bg-brand-50' : 'hover:bg-paper/70')}
                  aria-selected={onRowClick ? selected : undefined}>
                  {r.getVisibleCells().map(c => {
                    const meta = c.column.columnDef.meta
                    return (
                      <td key={c.id} className={cn('px-3 align-top text-[13px] text-ink-2', dense ? 'py-1.5' : 'py-2.5',
                        meta?.align === 'right' && 'num text-right', meta?.align === 'center' && 'text-center',
                        meta?.wrap ? 'min-w-[16rem]' : 'whitespace-nowrap', selected && c.column.getIndex() === 0 && 'shadow-[inset_3px_0_0_var(--color-brand-600)]')}>
                        {flexRender(c.column.columnDef.cell, c.getContext())}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {pages > 1 && (
        <div className="mt-2 flex items-center justify-between text-[12.5px] text-ink-3">
          <span>{filtered} rows, page {table.getState().pagination.pageIndex + 1} of {pages}</span>
          <div className="flex gap-1">
            <button className="rounded-md p-1 hover:bg-paper-2 disabled:opacity-40" onClick={() => table.previousPage()} disabled={!table.getCanPreviousPage()} aria-label="Previous page"><ChevronLeft className="size-4" /></button>
            <button className="rounded-md p-1 hover:bg-paper-2 disabled:opacity-40" onClick={() => table.nextPage()} disabled={!table.getCanNextPage()} aria-label="Next page"><ChevronRight className="size-4" /></button>
          </div>
        </div>
      )}
    </div>
  )
}
