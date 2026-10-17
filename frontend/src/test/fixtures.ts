import { http, HttpResponse } from 'msw'

import type { AnalysisDetail, Evidence, Risk } from '../api/analyses'
import type { DocumentDetail, Version } from '../api/documents'

/** Объекты API для MSW-обработчиков в тестах (синтетические, без реальных данных). */

export function makeVersion(overrides: Partial<Version> = {}): Version {
  return {
    id: 'v-1',
    version_no: 1,
    original_filename: 'Договор поставки.pdf',
    detected_format: 'pdf',
    size_bytes: 2048,
    extraction_status: 'ready',
    extraction_error_code: null,
    page_count: 2,
    page_source: 'pdf_native',
    created_at: '2026-10-16T10:00:00Z',
    mime_type: 'application/pdf',
    sha256: 'a'.repeat(64),
    extractor_version: '1.0.0',
    warnings: [],
    fragment_count: 3,
    converted_from_doc: false,
    updated_at: '2026-10-16T10:00:05Z',
    ...overrides,
  }
}

export function makeDocument(overrides: Partial<DocumentDetail> = {}): DocumentDetail {
  const version = makeVersion()
  return {
    id: 'd-1',
    title: 'Договор поставки',
    owner_id: 'id-analyst',
    owner_display_name: 'Тест analyst',
    status: 'active',
    current_version_id: version.id,
    current_version: version,
    created_at: '2026-10-16T10:00:00Z',
    is_owner: true,
    access: 'owner',
    can_manage: true,
    can_share: true,
    can_analyze: true,
    current_version_detail: version,
    versions: [version],
    ...overrides,
  }
}

export function makeAnalysis(overrides: Partial<AnalysisDetail> = {}): AnalysisDetail {
  return {
    id: 'a-1',
    document_id: 'd-1',
    document_title: 'Договор поставки',
    version_id: 'v-1',
    created_by_id: 'id-analyst',
    status: 'completed',
    party: 'party_a',
    party_name: null,
    categories: ['payment', 'liability'],
    depth: 'standard',
    model_display_name: 'Локальная модель',
    cloud_transfer: false,
    progress: { stage: 'done', chunks_total: 2, chunks_done: 2, risks: 3, retry_scheduled: null },
    error_code: null,
    error_message: null,
    created_at: '2026-10-17T10:00:00Z',
    started_at: '2026-10-17T10:00:01Z',
    finished_at: '2026-10-17T10:05:00Z',
    can_cancel: false,
    can_retry: false,
    config_snapshot: {
      model: { display_name: 'Локальная модель', external_model_id: 'test/model' },
      provider: { name: 'Ollama', is_cloud: false },
      verifier: { same_as_analysis: true },
      pipeline: { pipeline: 'pipeline-1' },
      captured_at: '2026-10-17T10:00:00Z',
    },
    cloud_consent_at: null,
    summary: {
      party: { code: 'party_a', name: null, resolved_name: 'ООО «Альфа» (Заказчик)' },
      model: { display_name: 'Локальная модель', external_model_id: 'test/model', provider_name: 'Ollama', is_cloud: false },
      verifier_model: { same_as_analysis: true, display_name: 'Локальная модель' },
      contract: { contract_type: 'договор поставки', subject: null, parties: [] },
      counts: {
        total: 4,
        shown: 3,
        by_validation_status: { verified_match: 1, needs_review: 2, rejected: 1 },
        by_severity: { high: 2, medium: 1 },
        missing_clauses: 1,
        contradictions: 0,
      },
      statement: 'Рисков с подтверждёнными цитатами: 1; требуют проверки: 2.',
      warnings: [],
      prompt_injection: [],
      limitations: ['Смысловая проверка цитат выполнена той же моделью, что и анализ.'],
      disclaimer: 'Результат подготовлен автоматически и не заменяет консультацию юриста.',
    },
    coverage: {
      fragments_total: 10,
      fragments_analyzed: 10,
      chunks_total: 2,
      chunks_done: 2,
      chunks_failed: [],
      chunks_skipped: [],
      sections: [{ title: '1. Предмет', start_key: 'F000001', clause_ref: '1', status: 'analyzed' }],
      cross_section: { status: 'done' },
      page_source: 'pdf_native',
      pages_approximate: false,
    },
    usage: { prompt_tokens: 1000, completion_tokens: 200 },
    estimate: null,
    ...overrides,
  }
}

export function makeEvidence(overrides: Partial<Evidence> = {}): Evidence {
  return {
    id: 'e-1',
    claimed_fragment_key: 'F000006',
    fragment_id: 'f-6',
    fragment_key: 'F000006',
    span_fragment_keys: ['F000006'],
    quote: 'Поставщик вправе в одностороннем порядке изменить цену товара',
    match_status: 'exact',
    match_char_start: 100,
    match_char_end: 160,
    page_start: 1,
    page_end: 1,
    page_source: 'pdf_native',
    pages_approximate: false,
    clause_ref: '2.2',
    relevance_status: 'supports',
    relevance_rationale: 'Цитата прямо разрешает изменение цены',
    relevance_checked_by: 'verifier_llm',
    match_notes: [],
    attempt: 0,
    superseded: false,
    ...overrides,
  }
}

export function makeRisk(overrides: Partial<Risk> = {}): Risk {
  return {
    id: 'r-1',
    risk_key: 'R-001',
    kind: 'clause_risk',
    title: 'Одностороннее изменение цены',
    category: 'unilateral_changes',
    severity: 'high',
    description: 'Поставщик может изменить цену без согласия покупателя',
    reason: 'Пункт 2.2 разрешает одностороннее изменение',
    consequences: 'Рост расходов',
    affected_party: 'party_a',
    recommendation: 'Согласовывать изменение цены дополнительным соглашением',
    suggested_revision: 'Цена может быть изменена только по соглашению сторон',
    model_confidence: 0.8,
    validation_status: 'verified_match',
    validation_notes: { status_reasons: [] },
    search_scope: null,
    dedup_group: null,
    evidence: [makeEvidence()],
    ...overrides,
  }
}

export const RISKS: Risk[] = [
  makeRisk(),
  makeRisk({
    id: 'r-2',
    risk_key: 'R-002',
    title: 'Ограничение ответственности поставщика',
    category: 'liability',
    severity: 'medium',
    affected_party: 'party_b',
    validation_status: 'needs_review',
    validation_notes: { status_reasons: ['relevance_partial'], legal_reference_unverified: true },
    suggested_revision: null,
    evidence: [
      makeEvidence({
        id: 'e-2',
        quote: 'Ответственность поставщика ограничена суммой 1000 рублей',
        relevance_status: 'partially_supports',
        page_start: 2,
        page_end: 2,
        clause_ref: '3.2',
      }),
    ],
  }),
  makeRisk({
    id: 'r-3',
    risk_key: 'R-003',
    kind: 'missing_clause',
    title: 'Нет условия о форс-мажоре',
    category: 'force_majeure',
    severity: 'high',
    validation_status: 'needs_review',
    validation_notes: { status_reasons: ['missing_clause_requires_review'] },
    suggested_revision: null,
    evidence: [],
    search_scope: {
      model_scope: 'Просмотрены разделы 1–4',
      checked_sections: ['1. Предмет', '2. Цена'],
      checked_fragments: 10,
      limitations: ['Отсутствие условия установлено моделью — проверьте договор, включая приложения.'],
    },
  }),
  makeRisk({
    id: 'r-4',
    risk_key: 'R-004',
    title: 'Выдуманное условие о штрафе',
    category: 'penalties',
    severity: 'high',
    validation_status: 'rejected',
    validation_notes: { status_reasons: ['no_matching_quote'], quote_retry: 'withdrawn' },
    evidence: [
      makeEvidence({
        id: 'e-4',
        quote: 'Штраф 50 % за любую задержку',
        match_status: 'not_found',
        relevance_status: 'unchecked',
        relevance_rationale: null,
        page_start: null,
        page_end: null,
        clause_ref: null,
      }),
    ],
  }),
]

export const availableModels = (items: Record<string, unknown>[], cloud = true) =>
  HttpResponse.json({
    items,
    cloud_models_enabled: cloud,
    cloud_notice: { version: '3', text: 'Фрагменты договора будут переданы внешнему провайдеру.' },
  })

export const localModel = {
  id: 'm-local',
  display_name: 'Локальная модель',
  provider_name: 'Ollama',
  provider_type: 'ollama',
  is_cloud: false,
  is_default: true,
  purpose: ['analysis'],
  context_window: 32000,
  pricing: null,
  consent_required: false,
}

export const cloudModel = {
  ...localModel,
  id: 'm-cloud',
  display_name: 'Облачная модель',
  provider_name: 'OpenRouter',
  provider_type: 'openrouter',
  is_cloud: true,
  is_default: false,
  pricing: { prompt_per_mtok: '0.15', completion_per_mtok: '0.6', currency: 'USD', source: 'provider_api' },
  consent_required: true,
}

export const analysisHandlers = (analysis: AnalysisDetail, risks: Risk[] = RISKS) => [
  http.get('*/api/analyses/a-1', () => HttpResponse.json(analysis)),
  http.get('*/api/analyses/a-1/risks', () =>
    HttpResponse.json({ items: risks, total: risks.length, hidden_rejected: 0 }),
  ),
  http.get('*/api/analyses/a-1/exports', () => HttpResponse.json({ items: [] })),
]
