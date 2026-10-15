import { z } from 'zod'

import { apiRequest } from './client'

const auditEntrySchema = z.object({
  id: z.number(),
  created_at: z.string(),
  actor_user_id: z.string().nullable(),
  actor_email: z.string().nullable(),
  action: z.string(),
  target_type: z.string().nullable(),
  target_id: z.string().nullable(),
  ip: z.string().nullable(),
  request_id: z.string().nullable(),
  details: z.record(z.string(), z.unknown()),
})
export type AuditEntry = z.infer<typeof auditEntrySchema>

const auditListSchema = z.object({ items: z.array(auditEntrySchema), next_cursor: z.number().nullable() })
export type AuditList = z.infer<typeof auditListSchema>

export async function fetchAudit(
  filters: { action?: string; beforeId?: number | null },
  signal?: AbortSignal,
): Promise<AuditList> {
  const params = new URLSearchParams({ limit: '50' })
  if (filters.action) params.set('action', filters.action)
  if (filters.beforeId) params.set('before_id', String(filters.beforeId))
  return auditListSchema.parse(await apiRequest<unknown>(`/admin/audit?${params.toString()}`, { signal }))
}
