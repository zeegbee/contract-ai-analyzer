import type { RouteObject } from 'react-router'

import { AccountPage } from '../pages/AccountPage'
import { AuditPage } from '../pages/admin/AuditPage'
import { LlmPage } from '../pages/admin/LlmPage'
import { ModelPage } from '../pages/admin/ModelPage'
import { UserDetailPage } from '../pages/admin/UserDetailPage'
import { UsersPage } from '../pages/admin/UsersPage'
import { ChangePasswordPage } from '../pages/ChangePasswordPage'
import { DocumentPage } from '../pages/documents/DocumentPage'
import { DocumentsPage } from '../pages/documents/DocumentsPage'
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
      { path: 'documents', element: <DocumentsPage /> },
      { path: 'documents/:documentId', element: <DocumentPage /> },
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
        path: 'admin/models',
        element: (
          <RequireCapability capability="manage_models">
            <LlmPage />
          </RequireCapability>
        ),
      },
      {
        path: 'admin/models/:modelId',
        element: (
          <RequireCapability capability="manage_models">
            <ModelPage />
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
