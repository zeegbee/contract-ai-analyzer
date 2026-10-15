import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router'

import type { Me } from '../api/auth'
import { ErrorNotice } from '../components/ErrorNotice'
import { ru } from '../i18n/ru'
import { CHANGE_PASSWORD_PATH, CurrentUserContext, useCurrentUser, useMe } from './session'

/** Только UX: реальные права проверяет backend. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const me = useMe()
  const location = useLocation()

  if (me.isPending) {
    return (
      <p className="p-6" aria-live="polite">
        {ru.common.loading}
      </p>
    )
  }
  if (me.isError) {
    return (
      <div className="p-6">
        <ErrorNotice error={me.error} onRetry={() => void me.refetch()} />
      </div>
    )
  }
  if (!me.data) {
    const next = encodeURIComponent(location.pathname + location.search)
    return <Navigate to={`/login?next=${next}`} replace />
  }
  if (me.data.must_change_password && location.pathname !== CHANGE_PASSWORD_PATH) {
    return <Navigate to={CHANGE_PASSWORD_PATH} replace />
  }
  return <CurrentUserContext value={me.data}>{children}</CurrentUserContext>
}

type Capability = keyof Omit<Me['capabilities'], 'assignable_roles'>

export function RequireCapability({ capability, children }: { capability: Capability; children: ReactNode }) {
  const me = useCurrentUser()
  if (!me.capabilities[capability]) {
    return (
      <p role="alert" className="rounded-md border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
        {ru.errors.forbidden}
      </p>
    )
  }
  return children
}
