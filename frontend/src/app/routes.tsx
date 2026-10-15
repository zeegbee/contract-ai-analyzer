import type { RouteObject } from 'react-router'

import { AccountPage } from '../pages/AccountPage'
import { AuditPage } from '../pages/admin/AuditPage'
import { UserDetailPage } from '../pages/admin/UserDetailPage'
import { UsersPage } from '../pages/admin/UsersPage'
import { ChangePasswordPage } from '../pages/ChangePasswordPage'
import { LoginPage } from '../pages/LoginPage'
import { NotFoundPage } from '../pages/NotFoundPage'
import { StatusPage } from '../pages/StatusPage'
import { RequireAuth, RequireCapability } from './guards'
import { Layout } from './Layout'

export const routes: RouteObject[] = [
  { path: '/login', element: <LoginPage /> },
  {
    path: '/',
    element: (
      <RequireAuth>
        <Layout />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <StatusPage /> },
      { path: 'change-password', element: <ChangePasswordPage /> },
      { path: 'account', element: <AccountPage /> },
      {
        path: 'admin/users',
        element: (
          <RequireCapability capability="manage_users">
            <UsersPage />
          </RequireCapability>
        ),
      },
      {
        path: 'admin/users/:userId',
        element: (
          <RequireCapability capability="manage_users">
            <UserDetailPage />
          </RequireCapability>
        ),
      },
      {
        path: 'admin/audit',
        element: (
          <RequireCapability capability="view_audit_log">
            <AuditPage />
          </RequireCapability>
        ),
      },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]
