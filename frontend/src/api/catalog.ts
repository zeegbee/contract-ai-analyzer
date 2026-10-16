import { z } from 'zod'

import { apiRequest } from './client'

export const CATALOG_KEY = ['catalog'] as const
export const PROVIDERS_KEY = [...CATALOG_KEY, 'providers'] as const
export const MODELS_KEY = [...CATALOG_KEY, 'models'] as const
export const AVAILABLE_MODELS_KEY = ['models', 'available'] as const

const providerTypeSchema = z.enum(['openrouter', 'ollama'])
export type ProviderType = z.infer<typeof providerTypeSchema>

const structuredSchema = z.enum(['json_schema', 'json_mode', 'none', 'unknown'])
export type StructuredOutput = z.infer<typeof structuredSchema>

export const PURPOSES = ['analysis', 'verifier', 'structure'] as const
export type Purpose = (typeof PURPOSES)[number]

/** Провайдер. Секрета в ответе нет — только признак и последние 4 символа. */
export const providerSchema = z.object({
  id: z.string(),
  name: z.string(),
  type: providerTypeSchema,
  base_url: z.string(),
  is_cloud: z.boolean(),
  enabled: z.boolean(),
  has_secret: z.boolean(),
  secret_last4: z.string().nullable(),
  allow_private_network: z.boolean(),
  timeout_s: z.number(),
  max_concurrency: z.number(),
  extra_headers: z.record(z.string(), z.string()),
  last_check_at: z.string().nullable(),
  last_check_status: z.string().nullable(),
  last_check_error_safe: z.string().nullable(),
  url_warnings: z.array(z.string()),
  model_count: z.number(),
  version: z.number(),
  created_at: z.string(),
  updated_at: z.string(),
})
export type Provider = z.infer<typeof providerSchema>

const healthSchema = z.object({
  ok: z.boolean(),
  status: z.string(),
  message: z.string(),
  details: z.record(z.string(), z.unknown()),
})
export type Health = z.infer<typeof healthSchema>

export const pricingSchema = z.object({
  prompt_per_mtok: z.string(),
  completion_per_mtok: z.string(),
  currency: z.string(),
  source: z.enum(['provider_api', 'admin_manual']),
  fetched_at: z.string().nullable().optional(),
})
export type Pricing = z.infer<typeof pricingSchema>

export const modelSchema = z.object({
  id: z.string(),
  provider_config_id: z.string(),
  provider_name: z.string(),
  provider_type: providerTypeSchema,
  provider_enabled: z.boolean(),
  is_cloud: z.boolean(),
  display_name: z.string(),
  external_model_id: z.string(),
  enabled: z.boolean(),
  purpose: z.array(z.string()),
  priority: z.number(),
  is_default: z.boolean(),
  allowed_roles: z.array(z.string()),
  context_window: z.number().nullable(),
  max_output_tokens: z.number().nullable(),
  generation_params: z.record(z.string(), z.unknown()),
  structured_output: structuredSchema,
  pricing: pricingSchema.nullable(),
  notes: z.string().nullable(),
  provider_metadata: z.record(z.string(), z.unknown()).nullable(),
  last_tested_at: z.string().nullable(),
  last_test_status: z.string().nullable(),
  last_test_error_safe: z.string().nullable(),
  version: z.number(),
  created_at: z.string(),
  updated_at: z.string(),
})
export type Model = z.infer<typeof modelSchema>

const remoteModelSchema = z.object({
  external_model_id: z.string(),
  display_name: z.string(),
  context_window: z.number().nullable(),
  max_output_tokens: z.number().nullable(),
  prompt_price_per_mtok: z.string().nullable(),
  completion_price_per_mtok: z.string().nullable(),
  advertises_structured_output: z.boolean().nullable(),
  details: z.record(z.string(), z.unknown()),
  already_added: z.boolean(),
})
export type RemoteModel = z.infer<typeof remoteModelSchema>

const testStepSchema = z.object({ name: z.string(), ok: z.boolean(), code: z.string(), message: z.string() })
const modelTestSchema = z.object({ model: modelSchema, ok: z.boolean(), steps: z.array(testStepSchema) })
export type ModelTest = z.infer<typeof modelTestSchema>

const cloudNoticeSchema = z.object({ version: z.string(), text: z.string() })
const llmSettingsSchema = z.object({
  cloud_models_enabled: z.boolean(),
  cloud_notice: cloudNoticeSchema,
  can_edit: z.boolean(),
})
export type LlmSettings = z.infer<typeof llmSettingsSchema>

const revisionSchema = z.object({
  id: z.string(),
  entity_type: z.string(),
  entity_id: z.string(),
  revision: z.number(),
  diff: z.record(z.string(), z.unknown()),
  changed_by_id: z.string().nullable(),
  created_at: z.string(),
})
export type Revision = z.infer<typeof revisionSchema>

export const availableModelSchema = z.object({
  id: z.string(),
  display_name: z.string(),
  provider_name: z.string(),
  provider_type: providerTypeSchema,
  is_cloud: z.boolean(),
  is_default: z.boolean(),
  purpose: z.array(z.string()),
  context_window: z.number().nullable(),
  pricing: pricingSchema.nullable(),
  consent_required: z.boolean(),
})
export type AvailableModel = z.infer<typeof availableModelSchema>
const availableModelsSchema = z.object({
  items: z.array(availableModelSchema),
  cloud_models_enabled: z.boolean(),
  cloud_notice: cloudNoticeSchema,
})
export type AvailableModels = z.infer<typeof availableModelsSchema>

const id = (value: string) => encodeURIComponent(value)

// ------------------------------------------------------------------ провайдеры

export async function fetchProviders(signal?: AbortSignal): Promise<Provider[]> {
  const raw = await apiRequest<unknown>('/admin/providers', { signal })
  return z.object({ items: z.array(providerSchema) }).parse(raw).items
}

export interface ProviderInput {
  name?: string
  type?: ProviderType
  base_url?: string
  is_cloud?: boolean
  enabled?: boolean
  allow_private_network?: boolean
  api_key?: string
  clear_api_key?: boolean
  timeout_s?: number
  max_concurrency?: number
}

export async function createProvider(body: ProviderInput): Promise<Provider> {
  return providerSchema.parse(await apiRequest<unknown>('/admin/providers', { method: 'POST', body }))
}

export async function updateProvider(
  providerId: string,
  body: ProviderInput & { version: number },
): Promise<Provider> {
  return providerSchema.parse(
    await apiRequest<unknown>(`/admin/providers/${id(providerId)}`, { method: 'PATCH', body }),
  )
}

export async function deleteProvider(providerId: string): Promise<void> {
  await apiRequest<unknown>(`/admin/providers/${id(providerId)}`, { method: 'DELETE' })
}

export async function checkProvider(providerId: string): Promise<{ provider: Provider; health: Health }> {
  const raw = await apiRequest<unknown>(`/admin/providers/${id(providerId)}/check`, { method: 'POST' })
  return z.object({ provider: providerSchema, health: healthSchema }).parse(raw)
}

export async function fetchRemoteModels(providerId: string, q: string, signal?: AbortSignal): Promise<RemoteModel[]> {
  const params = new URLSearchParams({ limit: '200' })
  if (q) params.set('q', q)
  const raw = await apiRequest<unknown>(`/admin/providers/${id(providerId)}/remote-models?${params.toString()}`, {
    signal,
  })
  return z.object({ items: z.array(remoteModelSchema) }).parse(raw).items
}

export async function importModels(providerId: string, externalIds: string[]): Promise<Model[]> {
  const raw = await apiRequest<unknown>(`/admin/providers/${id(providerId)}/import-models`, {
    method: 'POST',
    body: { external_model_ids: externalIds },
  })
  return z.object({ items: z.array(modelSchema) }).parse(raw).items
}

// ------------------------------------------------------------------ модели

export async function fetchModels(signal?: AbortSignal): Promise<Model[]> {
  const raw = await apiRequest<unknown>('/admin/models', { signal })
  return z.object({ items: z.array(modelSchema) }).parse(raw).items
}

export async function fetchModel(modelId: string, signal?: AbortSignal): Promise<Model> {
  return modelSchema.parse(await apiRequest<unknown>(`/admin/models/${id(modelId)}`, { signal }))
}

export interface ModelInput {
  provider_config_id?: string
  external_model_id?: string
  display_name?: string
  enabled?: boolean
  purpose?: Purpose[]
  priority?: number
  is_default?: boolean
  allowed_roles?: string[]
  context_window?: number | null
  max_output_tokens?: number | null
  generation_params?: { temperature?: number; top_p?: number; seed?: number; num_ctx?: number }
  structured_output?: Exclude<StructuredOutput, 'json_schema'>
  pricing?: { prompt_per_mtok: string; completion_per_mtok: string; currency: string } | null
  notes?: string | null
}

export async function createModel(body: ModelInput): Promise<Model> {
  return modelSchema.parse(await apiRequest<unknown>('/admin/models', { method: 'POST', body }))
}

export async function updateModel(modelId: string, body: ModelInput & { version: number }): Promise<Model> {
  return modelSchema.parse(await apiRequest<unknown>(`/admin/models/${id(modelId)}`, { method: 'PATCH', body }))
}

export async function deleteModel(modelId: string): Promise<void> {
  await apiRequest<unknown>(`/admin/models/${id(modelId)}`, { method: 'DELETE' })
}

export async function testModel(modelId: string, generate: boolean): Promise<ModelTest> {
  const raw = await apiRequest<unknown>(`/admin/models/${id(modelId)}/test`, { method: 'POST', body: { generate } })
  return modelTestSchema.parse(raw)
}

export async function fetchRevisions(entityType: string, entityId: string, signal?: AbortSignal): Promise<Revision[]> {
  const params = new URLSearchParams({ entity_type: entityType, entity_id: entityId, limit: '50' })
  const raw = await apiRequest<unknown>(`/admin/config-revisions?${params.toString()}`, { signal })
  return z.object({ items: z.array(revisionSchema) }).parse(raw).items
}

// ------------------------------------------------------------------ настройки и выбор модели

export async function fetchLlmSettings(signal?: AbortSignal): Promise<LlmSettings> {
  return llmSettingsSchema.parse(await apiRequest<unknown>('/admin/llm-settings', { signal }))
}

export async function updateLlmSettings(body: {
  cloud_models_enabled?: boolean
  cloud_notice_text?: string
}): Promise<LlmSettings> {
  return llmSettingsSchema.parse(await apiRequest<unknown>('/admin/llm-settings', { method: 'PUT', body }))
}

export async function fetchAvailableModels(signal?: AbortSignal): Promise<AvailableModels> {
  return availableModelsSchema.parse(await apiRequest<unknown>('/models/available', { signal }))
}

export async function recordCloudConsent(modelId: string, noticeVersion: string): Promise<void> {
  await apiRequest<unknown>(`/models/${id(modelId)}/cloud-consent`, {
    method: 'POST',
    body: { notice_version: noticeVersion },
  })
}
