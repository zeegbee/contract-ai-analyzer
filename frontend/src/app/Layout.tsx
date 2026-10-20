import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Suspense } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router'

import { logout } from '../api/auth'
import { ru } from '../i18n/ru'
import { useCurrentUser } from './session'

const linkClass = ({ isActive }: { isActive: boolean }) =>
  isActive ? 'font-medium text-blue-800' : 'text-slate-700 hover:text-slate-950'

export function Layout() {
  const me = useCurrentUser()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const logoutMutation = useMutation({
    mutationFn: logout,
    onSettled: () => {
      queryClient.clear()
      void navigate('/login', { replace: true })
    },
  })

  return (
    <div className="flex min-h-screen flex-col">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:bg-white">
        Перейти к содержимому
      </a>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-3">
          <span className="text-base font-semibold">{ru.appName}</span>
          {!me.must_change_password && (
            <nav aria-label={ru.nav.mainNavigation} className="flex flex-wrap gap-4 text-sm">
              <NavLink to="/" end className={linkClass}>
                {ru.nav.overview}
              </NavLink>
              <NavLink to="/documents" className={linkClass}>
                {ru.nav.documents}
              </NavLink>
              <NavLink to="/analyses" className={linkClass}>
                {ru.nav.analyses}
              </NavLink>
              {me.capabilities.manage_users && (
                <NavLink to="/admin/users" className={linkClass}>
                  {ru.nav.users}
                </NavLink>
              )}
              {me.capabilities.manage_models && (
                <NavLink to="/admin/models" className={linkClass}>
                  {ru.nav.models}
                </NavLink>
              )}
              {me.capabilities.manage_models && (
                <NavLink to="/admin/settings" className={linkClass}>
                  {ru.nav.settings}
                </NavLink>
              )}
              {me.capabilities.view_audit_log && (
                <NavLink to="/admin/audit" className={linkClass}>
                  {ru.nav.audit}
                </NavLink>
              )}
              <NavLink to="/account" className={linkClass}>
                {ru.nav.account}
              </NavLink>
            </nav>
          )}
          <div className="flex items-center gap-3 text-sm">
            <span className="text-slate-700">
              {me.display_name} · {ru.roles[me.role]}
            </span>
            <button
              type="button"
              onClick={() => logoutMutation.mutate()}
              disabled={logoutMutation.isPending}
              className="rounded-md border border-slate-300 px-2 py-1 hover:bg-slate-100"
            >
              {ru.nav.logout}
            </button>
          </div>
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-6xl flex-1 px-4 py-6">
        <Suspense fallback={<p aria-live="polite">{ru.common.loading}</p>}>
          <Outlet />
        </Suspense>
      </main>
      <footer className="border-t border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2 px-4 py-3 text-xs text-slate-600">
          <p>{ru.disclaimer}</p>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
            <NavLink to="/status" className="text-slate-700 underline hover:text-slate-950">
              {ru.nav.status}
            </NavLink>
          </div>
        </div>
      </footer>
    </div>
  )
}
