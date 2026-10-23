import { http, HttpResponse } from 'msw'

import type { AvailableRule, RiskRule } from '../api/riskRules'

/** Правила рисков организации для MSW-обработчиков в тестах (синтетические). */

export const LIMITS = { catalog: 100, per_analysis: 20, text_chars: 1000, prompt_chars: 8000 }

export function makeRule(overrides: Partial<RiskRule> = {}): RiskRule {
  return {
    id: 'rr-1',
    code: 'RR-001',
    title: 'Высокая неустойка',
    category: 'penalties',
    kind: 'clause_risk',
    description: 'Неустойка выше 0,1 % в день от суммы задолженности',
    signs: null,
    severity: 'high',
    recommendation: null,
    enabled: true,
    default_selected: true,
    archived_at: null,
    version: 1,
    created_at: '2026-10-23T10:00:00Z',
    updated_at: '2026-10-23T10:00:00Z',
    ...overrides,
  }
}

export function availableRule(rule: RiskRule): AvailableRule {
  return {
    id: rule.id,
    code: rule.code,
    title: rule.title,
    category: rule.category,
    kind: rule.kind,
    description: rule.description,
    severity: rule.severity,
    default_selected: rule.default_selected,
  }
}

export const availableRulesHandler = (rules: RiskRule[], limits: Partial<typeof LIMITS> = {}) =>
  http.get('*/api/risk-rules/available', () =>
    HttpResponse.json({ items: rules.map(availableRule), limits: { ...LIMITS, ...limits } }),
  )
