import { z } from 'zod'

import { ApiError, apiRequest } from './client'

export const roleSchema = z.enum(['superadmin', 'admin', 'analyst', 'viewer'])
export type Role = z.infer<typeof roleSchema>

export const meSchema = z.object({
  id: z.string(),
  email: z.string(),
  display_name: z.string(),
  role: roleSchema,
  must_change_password: z.boolean(),
  capabilities: z.object({
    manage_users: z.boolean(),
    view_audit_log: z.boolean(),
    upload_documents: z.boolean(),
    manage_models: z.boolean(),
    manage_providers: z.boolean(),
    manage_llm_settings: z.boolean(),
    assignable_roles: z.array(roleSchema),
  }),
})
export type Me = z.infer<typeof meSchema>

export const sessionSchema = z.object({
  id: z.string(),
  created_at: z.string(),
  last_seen_at: z.string(),
  expires_at: z.string(),
  ip: z.string().nullable(),
  user_agent: z.string().nullable(),
  current: z.boolean(),
})
export type SessionInfo = z.infer<typeof sessionSchema>

export const ME_QUERY_KEY = ['auth', 'me'] as const

/** Текущий пользователь или null, если сессии нет (401). */
export async function fetchMe(signal?: AbortSignal): Promise<Me | null> {
  try {
    return meSchema.parse(await apiRequest<unknown>('/auth/me', { signal }))
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null
    throw error
  }
}

export async function login(email: string, password: string): Promise<Me> {
  return meSchema.parse(await apiRequest<unknown>('/auth/login', { method: 'POST', body: { email, password } }))
}

export async function logout(): Promise<void> {
  await apiRequest<void>('/auth/logout', { method: 'POST' })
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<Me> {
  return meSchema.parse(
    await apiRequest<unknown>('/auth/change-password', {
      method: 'POST',
      body: { current_password: currentPassword, new_password: newPassword },
    }),
  )
}

export async function fetchMySessions(signal?: AbortSignal): Promise<SessionInfo[]> {
  return z.array(sessionSchema).parse(await apiRequest<unknown>('/auth/sessions', { signal }))
}

export async function revokeMySession(id: string): Promise<void> {
  await apiRequest<void>(`/auth/sessions/${encodeURIComponent(id)}/revoke`, { method: 'POST' })
}

export async function revokeOtherSessions(): Promise<number> {
  const raw = await apiRequest<unknown>('/auth/sessions/revoke-others', { method: 'POST' })
  return z.object({ revoked: z.number() }).parse(raw).revoked
}
