import { z } from 'zod'

import { roleSchema, type Role } from './auth'
import { apiRequest } from './client'

export const USERS_KEY = ['users'] as const

export const userSchema = z.object({
  id: z.string(),
  email: z.string(),
  display_name: z.string(),
  role: roleSchema,
  is_active: z.boolean(),
  must_change_password: z.boolean(),
  failed_login_count: z.number(),
  locked_until: z.string().nullable(),
  last_login_at: z.string().nullable(),
  password_changed_at: z.string().nullable(),
  created_at: z.string(),
})
export type User = z.infer<typeof userSchema>

export const userDetailSchema = userSchema.extend({ active_sessions: z.number() })
export type UserDetail = z.infer<typeof userDetailSchema>

const userListSchema = z.object({ items: z.array(userSchema), next_cursor: z.string().nullable() })
export type UserList = z.infer<typeof userListSchema>

const temporaryPasswordSchema = z.object({ user: userSchema, temporary_password: z.string() })
export type TemporaryPassword = z.infer<typeof temporaryPasswordSchema>

const modelPermissionSchema = z.object({
  model_config_id: z.string(),
  display_name: z.string(),
  external_model_id: z.string(),
  enabled: z.boolean(),
  allowed_by_role: z.boolean(),
  override: z.boolean().nullable(),
  effective: z.boolean(),
})
export type ModelPermission = z.infer<typeof modelPermissionSchema>
const modelPermissionsSchema = z.object({ items: z.array(modelPermissionSchema) })

export interface UserFilters {
  q?: string
  role?: Role | ''
  isActive?: 'true' | 'false' | ''
  cursor?: string | null
}

export async function fetchUsers(filters: UserFilters, signal?: AbortSignal): Promise<UserList> {
  const params = new URLSearchParams({ limit: '50' })
  if (filters.q) params.set('q', filters.q)
  if (filters.role) params.set('role', filters.role)
  if (filters.isActive) params.set('is_active', filters.isActive)
  if (filters.cursor) params.set('cursor', filters.cursor)
  return userListSchema.parse(await apiRequest<unknown>(`/users?${params.toString()}`, { signal }))
}

export async function fetchUser(id: string, signal?: AbortSignal): Promise<UserDetail> {
  return userDetailSchema.parse(await apiRequest<unknown>(`/users/${encodeURIComponent(id)}`, { signal }))
}

export async function createUser(body: { email: string; display_name: string; role: Role }): Promise<TemporaryPassword> {
  return temporaryPasswordSchema.parse(await apiRequest<unknown>('/users', { method: 'POST', body }))
}

export async function updateUser(
  id: string,
  body: { display_name?: string; role?: Role; is_active?: boolean },
): Promise<UserDetail> {
  return userDetailSchema.parse(
    await apiRequest<unknown>(`/users/${encodeURIComponent(id)}`, { method: 'PATCH', body }),
  )
}

export async function resetPassword(id: string): Promise<TemporaryPassword> {
  return temporaryPasswordSchema.parse(
    await apiRequest<unknown>(`/users/${encodeURIComponent(id)}/reset-password`, { method: 'POST' }),
  )
}

export async function revokeUserSessions(id: string): Promise<number> {
  const raw = await apiRequest<unknown>(`/users/${encodeURIComponent(id)}/revoke-sessions`, { method: 'POST' })
  return z.object({ revoked: z.number() }).parse(raw).revoked
}

export async function unlockUser(id: string): Promise<UserDetail> {
  return userDetailSchema.parse(
    await apiRequest<unknown>(`/users/${encodeURIComponent(id)}/unlock`, { method: 'POST' }),
  )
}

export async function fetchModelPermissions(id: string, signal?: AbortSignal): Promise<ModelPermission[]> {
  const raw = await apiRequest<unknown>(`/users/${encodeURIComponent(id)}/model-permissions`, { signal })
  return modelPermissionsSchema.parse(raw).items
}

export async function saveModelPermissions(
  id: string,
  overrides: { model_config_id: string; allowed: boolean }[],
): Promise<ModelPermission[]> {
  const raw = await apiRequest<unknown>(`/users/${encodeURIComponent(id)}/model-permissions`, {
    method: 'PUT',
    body: { overrides },
  })
  return modelPermissionsSchema.parse(raw).items
}
