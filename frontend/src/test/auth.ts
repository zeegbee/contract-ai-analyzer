import { http, HttpResponse } from 'msw'

import type { Me, Role } from '../api/auth'
import { server } from './server'

const ASSIGNABLE: Record<Role, Role[]> = {
  superadmin: ['admin', 'analyst', 'superadmin', 'viewer'],
  admin: ['admin', 'analyst', 'viewer'],
  analyst: [],
  viewer: [],
}

/** Тестовый пользователь — только для MSW-обработчиков в тестах. */
export function makeMe(role: Role = 'analyst', overrides: Partial<Me> = {}): Me {
  const isAdmin = role === 'superadmin' || role === 'admin'
  return {
    id: `id-${role}`,
    email: `${role}@example.org`,
    display_name: `Тест ${role}`,
    role,
    must_change_password: false,
    capabilities: {
      manage_users: isAdmin,
      view_audit_log: isAdmin,
      upload_documents: role !== 'viewer',
      manage_models: isAdmin,
      manage_providers: role === 'superadmin',
      manage_llm_settings: role === 'superadmin',
      assignable_roles: ASSIGNABLE[role],
    },
    ...overrides,
  }
}

export function serveMe(me: Me | null) {
  server.use(
    http.get('*/api/auth/me', () =>
      me
        ? HttpResponse.json(me)
        : HttpResponse.json(
            { error: { code: 'authentication_required', message: 'Требуется вход в систему', request_id: 'r', details: {} } },
            { status: 401 },
          ),
    ),
  )
}

export const readyHandler = http.get('*/api/health/ready', () =>
  HttpResponse.json({ status: 'ready', checks: { database: 'ok' }, version: '0.1.0' }),
)
