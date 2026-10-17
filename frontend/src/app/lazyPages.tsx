import { lazy } from 'react'

// Редко используемые и тяжёлые разделы загружаются отдельными чанками (меньше начальный бандл).
export const AnalysesPage = lazy(() =>
  import('../pages/analyses/AnalysesPage').then((m) => ({ default: m.AnalysesPage })),
)
export const AnalysisPage = lazy(() =>
  import('../pages/analyses/AnalysisPage').then((m) => ({ default: m.AnalysisPage })),
)
export const NewAnalysisPage = lazy(() =>
  import('../pages/analyses/NewAnalysisPage').then((m) => ({ default: m.NewAnalysisPage })),
)
export const AccountPage = lazy(() => import('../pages/AccountPage').then((m) => ({ default: m.AccountPage })))
export const StatusPage = lazy(() => import('../pages/StatusPage').then((m) => ({ default: m.StatusPage })))
export const UsersPage = lazy(() => import('../pages/admin/UsersPage').then((m) => ({ default: m.UsersPage })))
export const UserDetailPage = lazy(() =>
  import('../pages/admin/UserDetailPage').then((m) => ({ default: m.UserDetailPage })),
)
export const LlmPage = lazy(() => import('../pages/admin/LlmPage').then((m) => ({ default: m.LlmPage })))
export const ModelPage = lazy(() => import('../pages/admin/ModelPage').then((m) => ({ default: m.ModelPage })))
export const SettingsPage = lazy(() =>
  import('../pages/admin/SettingsPage').then((m) => ({ default: m.SettingsPage })),
)
export const AuditPage = lazy(() => import('../pages/admin/AuditPage').then((m) => ({ default: m.AuditPage })))
