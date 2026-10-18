import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { isLive } from '@/lib/status'
import type { Detail, Investigation } from '@/lib/types'

export function useInvestigations(bank: string) {
  return useQuery({
    queryKey: ['investigations', bank], queryFn: () => api.investigations(bank), enabled: !!bank,
    refetchInterval: (q) => ((q.state.data as Investigation[] | undefined)?.some(i => isLive(i.status)) ? 3000 : 10000),
  })
}

export function useDetail(bank: string, inv: string | undefined) {
  return useQuery({
    queryKey: ['detail', bank, inv], queryFn: () => api.detail(bank, inv!), enabled: !!bank && !!inv,
    refetchInterval: (q) => {
      const s = (q.state.data as Detail | undefined)?.investigation.status
      return !s || isLive(s) ? 3000 : s === 'at_gate' ? 5000 : 20000
    },
  })
}

export function useTrail(bank: string, inv: string | undefined, status?: string) {
  return useQuery({
    queryKey: ['trail', bank, inv, status], queryFn: () => api.trail(bank, inv!), enabled: !!bank && !!inv,
    staleTime: 10000,
  })
}

export function useSpend(inv: string | undefined, live: boolean) {
  return useQuery({ queryKey: ['spend', inv], queryFn: () => api.spend(inv), enabled: !!inv, refetchInterval: live ? 4000 : false })
}
