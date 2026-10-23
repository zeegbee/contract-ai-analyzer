import type { RouteObject } from 'react-router'

import { ChangePasswordPage } from '../pages/ChangePasswordPage'
import { DocumentPage } from '../pages/documents/DocumentPage'
import { DocumentsPage } from '../pages/documents/DocumentsPage'
import { LoginPage } from '../pages/LoginPage'
import { NotFoundPage } from '../pages/NotFoundPage'
import { OverviewPage } from '../pages/OverviewPage'
import { RequireAuth, RequireCapability } from './guards'
import { Layout } from './Layout'
import {
  AccountPage,
  AnalysesPage,
  AnalysisPage,
  AuditPage,
  LlmPage,
  ModelPage,
  NewAnalysisPage,
  RiskRulesPage,
  SettingsPage,
  StatusPage,
  UserDetailPage,
  UsersPage,
} from './lazyPages'

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
      { index: true, element: <OverviewPage /> },
      { path: 'status', element: <StatusPage /> },
      { path: 'change-password', element: <ChangePasswordPage /> },
      { path: 'account', element: <AccountPage /> },
      { path: 'documents', element: <DocumentsPage /> },
      { path: 'documents/:documentId', element: <DocumentPage /> },
      { path: 'analyses', element: <AnalysesPage /> },
      {
        path: 'analyses/new',
        element: (
          <RequireCapability capability="upload_documents">
            <NewAnalysisPage />
          </RequireCapability>
        ),
      },
      { path: 'analyses/:analysisId', element: <AnalysisPage /> },
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
        path: 'admin/risk-rules',
        element: (
          <RequireCapability capability="manage_risk_rules">
            <RiskRulesPage />
          </RequireCapability>
        ),
      },
      {
        path: 'admin/settings',
        element: (
          <RequireCapability capability="manage_models">
            <SettingsPage />
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
