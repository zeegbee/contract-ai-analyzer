import { z } from 'zod'

import { apiRequest } from './client'

export const ANALYSES_KEY = ['analyses'] as const

export const CATEGORIES = [
  'payment',
  'liability',
  'penalties',
  'deadlines',
  'renewal',
  'termination',
  'unilateral_changes',
  'warranties',
  'confidentiality',
  'personal_data',
  'intellectual_property',
  'governing_law',
  'force_majeure',
  'acceptance',
  'undefined_terms',
  'contradiction',
  'missing_clause',
  'other',
] as const
export type Category = (typeof CATEGORIES)[number]

export const analysisStatusSchema = z.enum([
  'queued',
  'running',
  'verifying',
  'completed',
  'completed_with_warnings',
  'failed',
  'cancelling',
  'cancelled',
])
export type AnalysisStatus = z.infer<typeof analysisStatusSchema>
export const ACTIVE_ANALYSIS_STATUSES = new Set<AnalysisStatus>(['queued', 'running', 'verifying', 'cancelling'])
export const FINISHED_STATUSES = new Set<AnalysisStatus>(['completed', 'completed_with_warnings'])

const partySchema = z.enum(['party_a', 'party_b', 'named', 'unknown'])
export type Party = z.infer<typeof partySchema>
const depthSchema = z.enum(['quick', 'standard', 'deep'])
export type Depth = z.infer<typeof depthSchema>
export const severitySchema = z.enum(['critical', 'high', 'medium', 'low', 'informational'])
export type Severity = z.infer<typeof severitySchema>
export const validationSchema = z.enum(['pending', 'verified_match', 'needs_review', 'rejected'])
export type ValidationStatus = z.infer<typeof validationSchema>
const kindSchema = z.enum(['clause_risk', 'contradiction', 'missing_clause'])
export type RiskKind = z.infer<typeof kindSchema>
export const affectedSchema = z.enum(['party_a', 'party_b', 'both', 'unclear'])
export type AffectedParty = z.infer<typeof affectedSchema>
const matchSchema = z.enum(['pending', 'exact', 'normalized', 'not_found', 'fragment_mismatch', 'too_short'])
export type MatchStatus = z.infer<typeof matchSchema>
const relevanceSchema = z.enum(['supports', 'partially_supports', 'does_not_support', 'unchecked'])
export type RelevanceStatus = z.infer<typeof relevanceSchema>

const record = z.record(z.string(), z.unknown())

const progressSchema = z.object({
  stage: z.string().nullable(),
  chunks_total: z.number().nullable(),
  chunks_done: z.number().nullable(),
  risks: z.number().nullable(),
  retry_scheduled: z.string().nullable(),
})

export const analysisSummarySchema = z.object({
  id: z.string(),
  document_id: z.string(),
  document_title: z.string(),
  version_id: z.string(),
  created_by_id: z.string().nullable(),
  status: analysisStatusSchema,
  party: partySchema,
  party_name: z.string().nullable(),
  categories: z.array(z.string()),
  depth: depthSchema,
  model_display_name: z.string().nullable(),
  cloud_transfer: z.boolean(),
  progress: progressSchema,
  error_code: z.string().nullable(),
  error_message: z.string().nullable(),
  created_at: z.string(),
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
  can_cancel: z.boolean(),
  can_retry: z.boolean(),
})
export type AnalysisSummary = z.infer<typeof analysisSummarySchema>

export const estimateSchema = z.object({
  chunks: z.number(),
  estimated_input_tokens: z.number(),
  estimated_output_tokens: z.number(),
  estimated_tokens: z.number(),
  token_limit: z.number(),
  within_limit: z.boolean(),
  estimated_cost: z.string().nullable(),
  cost_currency: z.string().nullable(),
  pricing_source: z.string().nullable(),
  cloud_transfer: z.boolean(),
  note: z.string(),
})
export type Estimate = z.infer<typeof estimateSchema>

const sectionSchema = z.object({
  title: z.string(),
  start_key: z.string(),
  clause_ref: z.string().nullable().optional(),
  status: z.enum(['analyzed', 'partial', 'not_analyzed']),
})
const chunkRefSchema = z.object({
  index: z.number(),
  from: z.string().nullable(),
  to: z.string().nullable(),
  code: z.string().nullable(),
})
export const coverageSchema = z.looseObject({
  fragments_total: z.number(),
  fragments_analyzed: z.number(),
  chunks_total: z.number(),
  chunks_done: z.number(),
  chunks_failed: z.array(chunkRefSchema),
  chunks_skipped: z.array(chunkRefSchema),
  sections: z.array(sectionSchema),
  cross_section: z.looseObject({ status: z.string().optional(), reason: z.string().optional() }),
  page_source: z.string().nullable(),
  pages_approximate: z.boolean(),
})
export type Coverage = z.infer<typeof coverageSchema>

export const summarySchema = z.looseObject({
  party: z.object({ code: z.string(), name: z.string().nullable(), resolved_name: z.string().nullable() }),
  model: z.looseObject({
    display_name: z.string().nullable().optional(),
    external_model_id: z.string().nullable().optional(),
    provider_name: z.string().nullable().optional(),
    is_cloud: z.boolean().nullable().optional(),
  }),
  verifier_model: z.looseObject({ same_as_analysis: z.boolean(), display_name: z.string().nullable().optional() }),
  contract: z.looseObject({
    contract_type: z.string().nullable().optional(),
    subject: z.string().nullable().optional(),
    parties: z.array(z.object({ label: z.string(), name: z.string(), role: z.string().nullable().optional() })),
  }),
  counts: z.looseObject({
    total: z.number(),
    shown: z.number(),
    by_validation_status: z.record(z.string(), z.number()),
    by_severity: z.record(z.string(), z.number()),
    missing_clauses: z.number(),
    contradictions: z.number(),
  }),
  statement: z.string(),
  warnings: z.array(z.string()),
  prompt_injection: z.array(z.object({ fragment_key: z.string(), code: z.string() })),
  limitations: z.array(z.string()),
  disclaimer: z.string(),
})
export type Summary = z.infer<typeof summarySchema>

export const analysisDetailSchema = analysisSummarySchema.extend({
  config_snapshot: record,
  cloud_consent_at: z.string().nullable(),
  summary: summarySchema.nullable(),
  coverage: coverageSchema.nullable(),
  usage: record.nullable(),
  estimate: estimateSchema.nullable(),
})
export type AnalysisDetail = z.infer<typeof analysisDetailSchema>

const analysisListSchema = z.object({ items: z.array(analysisSummarySchema), next_cursor: z.string().nullable() })
export type AnalysisList = z.infer<typeof analysisListSchema>

export const evidenceSchema = z.object({
  id: z.string(),
  claimed_fragment_key: z.string(),
  fragment_id: z.string().nullable(),
  fragment_key: z.string().nullable(),
  span_fragment_keys: z.array(z.string()),
  quote: z.string(),
  match_status: matchSchema,
  match_char_start: z.number().nullable(),
  match_char_end: z.number().nullable(),
  page_start: z.number().nullable(),
  page_end: z.number().nullable(),
  page_source: z.string().nullable(),
  pages_approximate: z.boolean(),
  clause_ref: z.string().nullable(),
  relevance_status: relevanceSchema,
  relevance_rationale: z.string().nullable(),
  relevance_checked_by: z.string().nullable(),
  match_notes: z.array(z.string()),
  attempt: z.number(),
  superseded: z.boolean(),
})
export type Evidence = z.infer<typeof evidenceSchema>

export const searchScopeSchema = z.looseObject({
  model_scope: z.string().nullable().optional(),
  checked_sections: z.array(z.string()).optional(),
  checked_fragments: z.number().optional(),
  limitations: z.array(z.string()).optional(),
})
export type SearchScope = z.infer<typeof searchScopeSchema>

export const riskSchema = z.object({
  id: z.string(),
  risk_key: z.string(),
  kind: kindSchema,
  title: z.string(),
  category: z.string(),
  severity: severitySchema,
  description: z.string(),
  reason: z.string(),
  consequences: z.string(),
  affected_party: affectedSchema,
  recommendation: z.string(),
  suggested_revision: z.string().nullable(),
  model_confidence: z.number().nullable(),
  validation_status: validationSchema,
  validation_notes: record,
  search_scope: searchScopeSchema.nullable(),
  dedup_group: z.string().nullable(),
  evidence: z.array(evidenceSchema),
})
export type Risk = z.infer<typeof riskSchema>

const riskListSchema = z.object({ items: z.array(riskSchema), total: z.number(), hidden_rejected: z.number() })
export type RiskList = z.infer<typeof riskListSchema>

export const exportFormatSchema = z.enum(['pdf', 'docx', 'csv', 'xlsx'])
export type ExportFormat = z.infer<typeof exportFormatSchema>
export const exportSchema = z.object({
  id: z.string(),
  analysis_id: z.string(),
  format: exportFormatSchema,
  status: z.enum(['queued', 'ready', 'failed', 'expired']),
  created_at: z.string(),
  size_bytes: z.number().nullable(),
  expires_at: z.string().nullable(),
  error_code: z.string().nullable(),
  // Ссылка используется в href: только относительный путь API (не javascript:, не внешний адрес).
  download_url: z.string().regex(/^\/api\/exports\/[0-9a-f-]+\/download$/).nullable(),
})
export type ExportItem = z.infer<typeof exportSchema>
const exportListSchema = z.object({ items: z.array(exportSchema) })

export interface AnalysisRequest {
  document_id: string
  version_id: string | null
  party: Party
  party_name: string | null
  categories: Category[]
  depth: Depth
  model_id: string
  cloud_consent: boolean
}

const id = (value: string) => encodeURIComponent(value)

export async function fetchAnalyses(
  options: { documentId?: string; status?: AnalysisStatus | ''; cursor?: string | null; limit?: number },
  signal?: AbortSignal,
): Promise<AnalysisList> {
  const params = new URLSearchParams({ limit: String(options.limit ?? 50) })
  if (options.documentId) params.set('document_id', options.documentId)
  if (options.status) params.set('status', options.status)
  if (options.cursor) params.set('cursor', options.cursor)
  return analysisListSchema.parse(await apiRequest<unknown>(`/analyses?${params.toString()}`, { signal }))
}

export async function fetchAnalysis(analysisId: string, signal?: AbortSignal): Promise<AnalysisDetail> {
  return analysisDetailSchema.parse(await apiRequest<unknown>(`/analyses/${id(analysisId)}`, { signal }))
}

export async function fetchRisks(analysisId: string, signal?: AbortSignal): Promise<RiskList> {
  return riskListSchema.parse(
    await apiRequest<unknown>(`/analyses/${id(analysisId)}/risks?include_rejected=true`, { signal }),
  )
}

export async function estimateAnalysis(body: AnalysisRequest): Promise<Estimate> {
  return estimateSchema.parse(await apiRequest<unknown>('/analyses/estimate', { method: 'POST', body }))
}

export async function createAnalysis(body: AnalysisRequest, idempotencyKey: string): Promise<AnalysisDetail> {
  const raw = await apiRequest<unknown>('/analyses', {
    method: 'POST',
    body,
    headers: { 'Idempotency-Key': idempotencyKey },
  })
  return analysisDetailSchema.parse(raw)
}

export async function cancelAnalysis(analysisId: string): Promise<AnalysisDetail> {
  return analysisDetailSchema.parse(await apiRequest<unknown>(`/analyses/${id(analysisId)}/cancel`, { method: 'POST' }))
}

export async function retryAnalysis(analysisId: string, cloudConsent: boolean): Promise<AnalysisDetail> {
  const raw = await apiRequest<unknown>(`/analyses/${id(analysisId)}/retry`, {
    method: 'POST',
    body: { cloud_consent: cloudConsent },
  })
  return analysisDetailSchema.parse(raw)
}

export async function fetchExports(analysisId: string, signal?: AbortSignal): Promise<ExportItem[]> {
  return exportListSchema.parse(await apiRequest<unknown>(`/analyses/${id(analysisId)}/exports`, { signal })).items
}

export async function createExport(analysisId: string, format: ExportFormat): Promise<ExportItem> {
  const raw = await apiRequest<unknown>(`/analyses/${id(analysisId)}/exports`, { method: 'POST', body: { format } })
  return exportSchema.parse(raw)
}
