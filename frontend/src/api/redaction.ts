import { z } from 'zod'

import { apiRequest } from './client'

/** Маскировка перед облачным анализом (этап 10): настройки, предпросмотр, правки, подтверждение. */

const categorySchema = z.object({
  code: z.string(),
  title: z.string(),
  short: z.string(),
  group: z.string(),
  method: z.string(),
  reliability: z.enum(['high', 'medium', 'low', 'unsupported']),
  label: z.string(),
  default_enabled: z.boolean(),
  warning: z.string().nullable(),
  supported: z.boolean(),
})
export type RedactionCategory = z.infer<typeof categorySchema>

const configSchema = z.object({
  allow_unmasked: z.boolean(),
  unmasked_notice_version: z.string(),
  preview_ttl_hours: z.number(),
  categories: z.array(categorySchema),
})
export type RedactionConfig = z.infer<typeof configSchema>

const occurrenceSchema = z.object({
  id: z.string(),
  fragment: z.string(),
  start: z.number(),
  end: z.number(),
  text: z.string(),
  source: z.string(),
  masked: z.boolean(),
})
export type Occurrence = z.infer<typeof occurrenceSchema>

const entitySchema = z.object({
  id: z.string(),
  category: z.string(),
  canonical: z.string(),
  confidence: z.enum(['high', 'medium', 'low']),
  source: z.string(),
  masked: z.boolean(),
  label: z.string().nullable(),
  occurrences: z.array(occurrenceSchema),
})
export type Entity = z.infer<typeof entitySchema>

const countsSchema = z.looseObject({
  categories: z.record(z.string(), z.number()).optional(),
  values: z.number().optional(),
  occurrences: z.number().optional(),
  manual: z.number().optional(),
  unmarked: z.number().optional(),
  review: z.number().optional(),
})

const previewSchema = z.object({
  id: z.string(),
  document_id: z.string(),
  version_id: z.string(),
  status: z.enum(['scanning', 'ready', 'failed', 'confirmed', 'expired']),
  error_code: z.string().nullable(),
  error_message: z.string().nullable(),
  created_at: z.string(),
  expires_at: z.string(),
  confirmed_at: z.string().nullable(),
  mode: z.string().nullable(),
  stale: z.boolean(),
  stale_reason: z.enum(['expired', 'settings_changed']).nullable(),
  used: z.boolean(),
  counts: countsSchema,
  ner_available: z.boolean().nullable(),
  allow_unmasked: z.boolean(),
  entities: z.array(entitySchema),
})
export type Preview = z.infer<typeof previewSchema>

export type EditOp =
  | { op: 'set_entity'; entity_id: string; masked: boolean }
  | { op: 'set_occurrence'; occurrence_id: string; masked: boolean }
  | { op: 'set_category'; entity_id: string; category: string }
  | { op: 'add'; fragment: string; start: number; end: number; category: string; all_occurrences: boolean }

export interface ConfirmBody {
  mode: 'masked' | 'unmasked'
  reviewed: boolean
  confirm_unmasked?: boolean
  unmasked_notice_version?: string
}

export const REDACTION_CONFIG_KEY = ['redaction', 'config'] as const
export const previewKey = (previewId: string) => ['redaction', 'preview', previewId] as const

const id = (value: string) => encodeURIComponent(value)

export async function fetchRedactionConfig(signal?: AbortSignal): Promise<RedactionConfig> {
  return configSchema.parse(await apiRequest<unknown>('/redaction/config', { signal }))
}

export async function createPreview(documentId: string, versionId: string): Promise<Preview> {
  const path = `/documents/${id(documentId)}/versions/${id(versionId)}/redaction-previews`
  return previewSchema.parse(await apiRequest<unknown>(path, { method: 'POST' }))
}

export async function fetchPreview(previewId: string, signal?: AbortSignal): Promise<Preview> {
  return previewSchema.parse(await apiRequest<unknown>(`/redaction-previews/${id(previewId)}`, { signal }))
}

export async function editPreview(previewId: string, ops: EditOp[]): Promise<Preview> {
  return previewSchema.parse(
    await apiRequest<unknown>(`/redaction-previews/${id(previewId)}`, { method: 'PATCH', body: { ops } }),
  )
}

export async function confirmPreview(previewId: string, body: ConfirmBody): Promise<Preview> {
  return previewSchema.parse(
    await apiRequest<unknown>(`/redaction-previews/${id(previewId)}/confirm`, { method: 'POST', body }),
  )
}

// ------------------------------------------------------------------ настройки (Admin — чтение, Superadmin — изменение)

const settingsSchema = z.object({
  categories: z.record(z.string(), z.boolean()),
  allow_unmasked: z.boolean(),
  custom_dictionary: z.array(z.object({ text: z.string(), category: z.string() })),
  custom_patterns: z.array(z.object({ pattern: z.string(), category: z.string() })),
  allow_list: z.array(z.string()),
  preview_ttl_hours: z.number(),
})
export type RedactionSettings = z.infer<typeof settingsSchema>

const settingsOutSchema = z.object({
  settings: settingsSchema,
  revision: z.number(),
  updated_at: z.string().nullable(),
  updated_by_id: z.string().nullable(),
  can_manage: z.boolean(),
  categories: z.array(categorySchema),
})
export type RedactionSettingsOut = z.infer<typeof settingsOutSchema>

export const REDACTION_SETTINGS_KEY = ['settings', 'redaction'] as const

export async function fetchRedactionSettings(signal?: AbortSignal): Promise<RedactionSettingsOut> {
  return settingsOutSchema.parse(await apiRequest<unknown>('/admin/redaction', { signal }))
}

export async function updateRedactionSettings(
  settings: RedactionSettings,
  confirmAllowUnmasked: boolean,
): Promise<RedactionSettingsOut> {
  return settingsOutSchema.parse(
    await apiRequest<unknown>('/admin/redaction', {
      method: 'PUT',
      body: { ...settings, confirm_allow_unmasked: confirmAllowUnmasked },
    }),
  )
}
