import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Suspense } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router'

import { logout } from '../api/auth'
import { ru } from '../i18n/ru'
import { useCurrentUser } from './session'

// Пункты меню — «вкладки»: активный раздел выделен фоном и подчёркиванием.
const linkClass = ({ isActive }: { isActive: boolean }) =>
  `block rounded-t-md border-b-2 px-3 py-2 whitespace-nowrap ${
    isActive
      ? 'border-blue-700 bg-blue-50 font-medium text-blue-800'
      : 'border-transparent text-slate-700 hover:border-slate-300 hover:bg-slate-100 hover:text-slate-950'
  }`

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
  const menu = [
    { to: '/', label: ru.nav.overview, show: true },
    { to: '/documents', label: ru.nav.documents, show: true },
    { to: '/analyses', label: ru.nav.analyses, show: true },
    { to: '/admin/users', label: ru.nav.users, show: me.capabilities.manage_users },
    { to: '/admin/models', label: ru.nav.models, show: me.capabilities.manage_models },
    { to: '/admin/risk-rules', label: ru.nav.riskRules, show: me.capabilities.manage_risk_rules },
    { to: '/admin/settings', label: ru.nav.settings, show: me.capabilities.manage_models },
    { to: '/admin/audit', label: ru.nav.audit, show: me.capabilities.view_audit_log },
    { to: '/account', label: ru.nav.account, show: true },
  ].filter((item) => item.show)

  return (
    <div className="flex min-h-screen flex-col">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:bg-white">
        Перейти к содержимому
      </a>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-3">
          <span className="text-base font-semibold">{ru.appName}</span>
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
        {!me.must_change_password && (
          <nav aria-label={ru.nav.mainNavigation} className="border-t border-slate-200 bg-slate-50">
            <ul className="mx-auto flex max-w-6xl flex-wrap gap-1 px-4 pt-1 text-sm">
              {menu.map((item) => (
                <li key={item.to}>
                  <NavLink to={item.to} end={item.to === '/'} className={linkClass}>
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>
        )}
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
