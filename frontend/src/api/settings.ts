import { z } from 'zod'

import { apiRequest } from './client'

const parameterSchema = z.object({
  key: z.string(),
  value: z.union([z.number(), z.string(), z.boolean()]),
  env: z.string(),
})
const systemSettingsSchema = z.object({
  groups: z.record(z.string(), z.array(parameterSchema)),
  editable: z.boolean(),
})
export type SystemSettings = z.infer<typeof systemSettingsSchema>

export async function fetchSystemSettings(signal?: AbortSignal): Promise<SystemSettings> {
  return systemSettingsSchema.parse(await apiRequest<unknown>('/admin/system-settings', { signal }))
}

const retentionSchema = z.object({
  retention_days: z.number().int().nullable(),
  updated_at: z.string().nullable(),
  updated_by_id: z.string().nullable(),
  eligible_now: z.number().int().nullable(),
  preview_days: z.number().int().nullable(),
  preview_count: z.number().int().nullable(),
  can_manage: z.boolean(),
  min_days: z.number().int(),
  max_days: z.number().int(),
})
export type Retention = z.infer<typeof retentionSchema>

export const RETENTION_KEY = ['settings', 'retention'] as const

export async function fetchRetention(previewDays: number | null, signal?: AbortSignal): Promise<Retention> {
  const query = previewDays ? `?preview_days=${previewDays}` : ''
  return retentionSchema.parse(await apiRequest<unknown>(`/admin/retention${query}`, { signal }))
}

export async function updateRetention(body: { retention_days: number | null; confirm: boolean }): Promise<Retention> {
  return retentionSchema.parse(await apiRequest<unknown>('/admin/retention', { method: 'PUT', body }))
}
