import { z } from 'zod'

import { type Category, CATEGORIES, severitySchema } from './analyses'
import { type Revision, revisionSchema } from './catalog'
import { apiRequest } from './client'

/** Правила рисков организации (этап 11). Управляют Admin и Superadmin; форма анализа — `available`. */
export const RISK_RULES_KEY = ['risk-rules'] as const
export const AVAILABLE_RULES_KEY = [...RISK_RULES_KEY, 'available'] as const

export const RULE_KINDS = ['clause_risk', 'missing_clause', 'any'] as const
export type RuleKind = (typeof RULE_KINDS)[number]

const limitsSchema = z.object({
  catalog: z.number(),
  per_analysis: z.number(),
  text_chars: z.number(),
  prompt_chars: z.number(),
})
export type RuleLimits = z.infer<typeof limitsSchema>

export const riskRuleSchema = z.object({
  id: z.string(),
  code: z.string(),
  title: z.string(),
  category: z.enum(CATEGORIES),
  kind: z.enum(RULE_KINDS),
  description: z.string(),
  signs: z.string().nullable(),
  severity: severitySchema.nullable(),
  recommendation: z.string().nullable(),
  enabled: z.boolean(),
  default_selected: z.boolean(),
  archived_at: z.string().nullable(),
  version: z.number(),
  created_at: z.string(),
  updated_at: z.string(),
})
export type RiskRule = z.infer<typeof riskRuleSchema>

const ruleListSchema = z.object({ items: z.array(riskRuleSchema), limits: limitsSchema })
export type RiskRuleList = z.infer<typeof ruleListSchema>

export const availableRuleSchema = z.object({
  id: z.string(),
  code: z.string(),
  title: z.string(),
  category: z.enum(CATEGORIES),
  kind: z.enum(RULE_KINDS),
  description: z.string(),
  severity: severitySchema.nullable(),
  default_selected: z.boolean(),
})
export type AvailableRule = z.infer<typeof availableRuleSchema>
const availableListSchema = z.object({ items: z.array(availableRuleSchema), limits: limitsSchema })
export type AvailableRules = z.infer<typeof availableListSchema>

export interface RiskRuleInput {
  title: string
  category: Category
  kind: RuleKind
  description: string
  signs: string | null
  severity: z.infer<typeof severitySchema> | null
  recommendation: string | null
  enabled: boolean
  default_selected: boolean
}

const id = (value: string) => encodeURIComponent(value)

export async function fetchRiskRules(archived: boolean, signal?: AbortSignal): Promise<RiskRuleList> {
  const raw = await apiRequest<unknown>(`/admin/risk-rules?archived=${archived ? 'true' : 'false'}`, { signal })
  return ruleListSchema.parse(raw)
}

export async function createRiskRule(body: RiskRuleInput): Promise<RiskRule> {
  return riskRuleSchema.parse(await apiRequest<unknown>('/admin/risk-rules', { method: 'POST', body }))
}

export async function updateRiskRule(
  ruleId: string,
  body: Partial<RiskRuleInput> & { version: number },
): Promise<RiskRule> {
  const raw = await apiRequest<unknown>(`/admin/risk-rules/${id(ruleId)}`, { method: 'PATCH', body })
  return riskRuleSchema.parse(raw)
}

export async function setRiskRuleArchived(rule: RiskRule, archived: boolean): Promise<RiskRule> {
  const action = archived ? 'archive' : 'restore'
  const raw = await apiRequest<unknown>(`/admin/risk-rules/${id(rule.id)}/${action}`, {
    method: 'POST',
    body: { version: rule.version },
  })
  return riskRuleSchema.parse(raw)
}

export async function fetchRiskRuleRevisions(ruleId: string, signal?: AbortSignal): Promise<Revision[]> {
  const raw = await apiRequest<unknown>(`/admin/risk-rules/${id(ruleId)}/revisions`, { signal })
  return z.object({ items: z.array(revisionSchema) }).parse(raw).items
}

export async function fetchAvailableRules(signal?: AbortSignal): Promise<AvailableRules> {
  return availableListSchema.parse(await apiRequest<unknown>('/risk-rules/available', { signal }))
}
