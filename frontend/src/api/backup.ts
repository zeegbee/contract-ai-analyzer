import { z } from 'zod'

import { apiRequest } from './client'

const configSchema = z.object({
  schedule_enabled: z.boolean().nullable(),
  schedule: z.string().nullable(),
  timezone: z.string().nullable(),
  target: z.string().nullable(),
  keep_local: z.number().int().nullable(),
  keep_remote_daily: z.number().int().nullable(),
  keep_remote_weekly: z.number().int().nullable(),
  keep_remote_monthly: z.number().int().nullable(),
  remote_prune: z.string().nullable(),
})
export type BackupConfig = z.infer<typeof configSchema>
export type BackupField = keyof BackupConfig

const runSchema = z.object({
  id: z.string(),
  started_at: z.string(),
  finished_at: z.string().nullable(),
  status: z.string(),
  trigger: z.string(),
  target: z.string(),
  encrypted: z.boolean().nullable(),
  name: z.string().nullable(),
  size_bytes: z.number().nullable(),
  uploaded: z.boolean().nullable(),
  verified: z.boolean().nullable(),
  pruned_local: z.number().nullable(),
  pruned_remote: z.number().nullable(),
  error_code: z.string().nullable(),
})
export type BackupRun = z.infer<typeof runSchema>

const backupStateSchema = z.object({
  config: configSchema,
  sources: z.record(z.string(), z.enum(['ui', 'env', 'default'])),
  env: configSchema,
  server: z.object({
    backup_dir: z.string(),
    min_interval_minutes: z.number().nullable(),
    s3: z.object({
      configured: z.boolean(),
      endpoint: z.string().nullable(),
      bucket: z.string().nullable(),
      prefix: z.string().nullable(),
    }),
    local: z.object({ configured: z.boolean(), dir: z.string().nullable() }),
    encryption: z.object({ configured: z.boolean(), valid: z.boolean(), recipient_hint: z.string().nullable() }),
  }),
  problems: z.array(z.object({ code: z.string(), message: z.string() })),
  previous_run_at: z.string().nullable(),
  next_run_at: z.string().nullable(),
  scheduler_seen_at: z.string().nullable(),
  requested_at: z.string().nullable(),
  last_success_at: z.string().nullable(),
  overdue: z.boolean(),
  runs: z.array(runSchema),
  can_manage: z.boolean(),
})
export type BackupState = z.infer<typeof backupStateSchema>

export const BACKUP_KEY = ['settings', 'backup'] as const

export async function fetchBackup(signal?: AbortSignal): Promise<BackupState> {
  return backupStateSchema.parse(await apiRequest<unknown>('/admin/backup', { signal }))
}

export async function updateBackup(body: Partial<BackupConfig> & { reset?: BackupField[] }): Promise<BackupState> {
  return backupStateSchema.parse(await apiRequest<unknown>('/admin/backup', { method: 'PUT', body }))
}

export async function requestBackupRun(): Promise<BackupState> {
  return backupStateSchema.parse(await apiRequest<unknown>('/admin/backup/run', { method: 'POST' }))
}
