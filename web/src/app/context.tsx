import { useQuery } from '@tanstack/react-query'
import { createContext, type ReactNode, useContext, useEffect, useMemo, useState } from 'react'
import { api } from '@/lib/api'
import type { Bank, UiConfig } from '@/lib/types'

interface Ctx {
  config: UiConfig | undefined
  banks: Bank[]
  bank: string
  bankName: string
  setBank: (b: string) => void
}

const BankCtx = createContext<Ctx | null>(null)
const KEY = 'tracehold.bank'

function stored() {
  try { return localStorage.getItem(KEY) } catch { return null }
}

export function AppProvider({ children }: { children: ReactNode }) {
  const config = useQuery({ queryKey: ['config'], queryFn: api.config, staleTime: Infinity })
  const ids = config.data?.banks ?? []
  const banks = useQuery({ queryKey: ['banks', ids], queryFn: () => api.banks(ids), enabled: ids.length > 0, staleTime: Infinity })
  const [bank, setBankState] = useState<string>(() => stored() ?? '')
  useEffect(() => {
    if (!config.data) return
    if (!bank || !config.data.banks.includes(bank)) setBankState(config.data.default_bank)
  }, [config.data, bank])
  const setBank = (b: string) => {
    setBankState(b)
    try { localStorage.setItem(KEY, b) } catch { /* private window */ }
  }
  const list = banks.data ?? ids.map(i => ({ bank_id: i, name: `Bank ${i}` }))
  const value = useMemo<Ctx>(() => ({
    config: config.data, banks: list, bank, setBank,
    bankName: list.find(b => b.bank_id === bank)?.name ?? (bank ? `Bank ${bank}` : ''),
  }), [config.data, list, bank])
  return <BankCtx.Provider value={value}>{children}</BankCtx.Provider>
}

export function useApp() {
  const c = useContext(BankCtx)
  if (!c) throw new Error('useApp outside AppProvider')
  return c
}
