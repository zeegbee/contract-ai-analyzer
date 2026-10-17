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
