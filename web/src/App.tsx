import { lazy, type ReactNode, Suspense } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router'
import { Spinner, TipProvider } from '@/components/ui'
import { AppProvider } from '@/app/context'
import { Shell } from '@/app/Shell'
import { DashboardPage } from '@/features/dashboard/DashboardPage'

// Every page but the Dashboard loads only when opened (React Flow for the trail, Recharts for the charts), so the
// first page of the app loads faster.
const InvestigationsPage = lazy(() => import('@/features/investigations/InvestigationsPage').then(m => ({ default: m.InvestigationsPage })))
const ReviewsPage = lazy(() => import('@/features/reviews/ReviewsPage').then(m => ({ default: m.ReviewsPage })))
const MemoryPage = lazy(() => import('@/features/memory/MemoryPage').then(m => ({ default: m.MemoryPage })))
const GraphView = lazy(() => import('@/features/visualisation/GraphView').then(m => ({ default: m.GraphView })))
const CostView = lazy(() => import('@/features/visualisation/CostView').then(m => ({ default: m.CostView })))
const FinanceView = lazy(() => import('@/features/visualisation/FinanceView').then(m => ({ default: m.FinanceView })))

const later = (page: ReactNode) => <Suspense fallback={<Spinner label="Loading the page" />}>{page}</Suspense>

export function App() {
  return (
    <BrowserRouter>
      <AppProvider>
        <TipProvider>
          <Routes>
            <Route element={<Shell />}>
              <Route index element={<DashboardPage />} />
              <Route path="investigations" element={later(<InvestigationsPage />)} />
              <Route path="investigations/:inv" element={later(<InvestigationsPage />)} />
              <Route path="reviews" element={later(<ReviewsPage />)} />
              <Route path="memory" element={later(<MemoryPage />)} />
              <Route path="visualise" element={<Navigate to="/visualise/graph" replace />} />
              <Route path="visualise/graph" element={later(<GraphView />)} />
              <Route path="visualise/cost" element={later(<CostView />)} />
              <Route path="visualise/finance" element={later(<FinanceView />)} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </TipProvider>
      </AppProvider>
    </BrowserRouter>
  )
}
