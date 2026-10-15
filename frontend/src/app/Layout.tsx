import { NavLink, Outlet } from 'react-router'

import { ru } from '../i18n/ru'

export function Layout() {
  return (
    <div className="flex min-h-screen flex-col">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:bg-white">
        Перейти к содержимому
      </a>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
          <span className="text-base font-semibold">{ru.appName}</span>
          <nav aria-label="Основная навигация" className="text-sm">
            <NavLink
              to="/"
              className={({ isActive }) => (isActive ? 'font-medium text-blue-800' : 'text-slate-700')}
            >
              {ru.nav.status}
            </NavLink>
          </nav>
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-6xl flex-1 px-4 py-6">
        <Outlet />
      </main>
      <footer className="border-t border-slate-200 bg-white">
        <p className="mx-auto max-w-6xl px-4 py-3 text-xs text-slate-600">{ru.disclaimer}</p>
      </footer>
    </div>
  )
}
