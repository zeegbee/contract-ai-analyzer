import { http, HttpResponse } from 'msw'

import type { FragmentList } from '../api/documents'
import type { Preview, RedactionCategory, RedactionConfig, RedactionSettingsOut } from '../api/redaction'

/** Фикстуры маскировки (этап 10): синтетические значения, без реальных данных. */

function category(
  code: string,
  short: string,
  group: string,
  label: string,
  overrides: Partial<RedactionCategory> = {},
): RedactionCategory {
  return {
    code,
    title: short,
    short,
    group,
    method: 'шаблон',
    reliability: 'high',
    label,
    default_enabled: true,
    warning: null,
    supported: true,
    ...overrides,
  }
}

export const REDACTION_CATEGORIES: RedactionCategory[] = [
  category('person_name', 'ФИО', 'personal', 'ЛИЦО', { title: 'ФИО (в любом падеже)', reliability: 'medium' }),
  category('special_category', 'особые категории', 'personal', 'ОСОБЫЕ_ДАННЫЕ', { reliability: 'low' }),
  category('org_name', 'организация', 'organization', 'ОРГАНИЗАЦИЯ', { title: 'Наименования организаций' }),
  category('inn_org', 'ИНН юрлица', 'organization', 'ИНН_ЮЛ'),
  category('money_amount', 'сумма', 'commercial', 'СУММА', {
    default_enabled: false,
    warning: 'Маскировка ухудшает анализ',
  }),
  category('biometric_signature', 'биометрия', 'personal', 'БИОМЕТРИЯ', {
    supported: false,
    reliability: 'unsupported',
    default_enabled: false,
  }),
]

export function makeRedactionConfig(overrides: Partial<RedactionConfig> = {}): RedactionConfig {
  return {
    allow_unmasked: false,
    unmasked_notice_version: 'unmasked-1',
    preview_ttl_hours: 24,
    categories: REDACTION_CATEGORIES,
    ...overrides,
  }
}

export const PREAMBLE = 'ООО «Альфа» в лице Иванова Ивана Петровича, ИНН 7707083893, заключили договор.'
export const TERMS = 'Альфа оплачивает услуги в течение 5 дней.'

export function makeFragments(): FragmentList {
  const base = {
    page_start: 1,
    page_end: 1,
    clause_ref: null,
    heading_path: [],
    table_ref: null,
    meta: {},
  }
  return {
    items: [
      { ...base, id: 'f-0', seq: 0, fragment_key: 'F000000', kind: 'paragraph', text: PREAMBLE, char_start: 0, char_end: PREAMBLE.length },
      {
        ...base,
        id: 'f-1',
        seq: 1,
        fragment_key: 'F000001',
        kind: 'paragraph',
        text: TERMS,
        char_start: PREAMBLE.length + 2,
        char_end: PREAMBLE.length + 2 + TERMS.length,
      },
    ],
    next_after_seq: null,
    total: 2,
  }
}

const at = (text: string, value: string) => text.indexOf(value)

export function makePreview(overrides: Partial<Preview> = {}): Preview {
  const alfa = at(PREAMBLE, 'Альфа')
  const person = at(PREAMBLE, 'Иванова Ивана Петровича')
  const inn = at(PREAMBLE, '7707083893')
  return {
    id: 'rp-1',
    document_id: 'd-1',
    version_id: 'v-1',
    status: 'ready',
    error_code: null,
    error_message: null,
    created_at: '2026-10-21T10:00:00Z',
    expires_at: '2026-10-22T10:00:00Z',
    confirmed_at: null,
    mode: null,
    stale: false,
    stale_reason: null,
    used: false,
    counts: { categories: { org_name: 1, person_name: 1, inn_org: 1 }, values: 3, occurrences: 4, manual: 0, unmarked: 0 },
    ner_available: true,
    allow_unmasked: false,
    entities: [
      {
        id: 'E0001',
        category: 'org_name',
        canonical: 'Альфа',
        confidence: 'high',
        source: 'pattern',
        masked: true,
        label: '⟦ОРГАНИЗАЦИЯ_1⟧',
        occurrences: [
          { id: 'E0001.1', fragment: 'F000000', start: alfa, end: alfa + 5, text: 'Альфа', source: 'pattern', masked: true },
          { id: 'E0001.2', fragment: 'F000001', start: 0, end: 5, text: 'Альфа', source: 'propagated', masked: true },
        ],
      },
      {
        id: 'E0002',
        category: 'person_name',
        canonical: 'Иванов Иван Петрович',
        confidence: 'medium',
        source: 'ner',
        masked: true,
        label: '⟦ЛИЦО_1⟧',
        occurrences: [
          {
            id: 'E0002.1',
            fragment: 'F000000',
            start: person,
            end: person + 'Иванова Ивана Петровича'.length,
            text: 'Иванова Ивана Петровича',
            source: 'ner',
            masked: true,
          },
        ],
      },
      {
        id: 'E0003',
        category: 'inn_org',
        canonical: '7707083893',
        confidence: 'low',
        source: 'pattern',
        masked: true,
        label: '⟦ИНН_ЮЛ_1⟧',
        occurrences: [
          { id: 'E0003.1', fragment: 'F000000', start: inn, end: inn + 10, text: '7707083893', source: 'pattern', masked: true },
        ],
      },
    ],
    ...overrides,
  }
}

export interface RedactionLog {
  created: number
  edits: unknown[]
  confirms: Record<string, unknown>[]
}

/** Обработчики предпросмотра: создание сразу возвращает готовый предпросмотр; запросы записываются в log. */
export function redactionHandlers(
  log: RedactionLog,
  options: { preview?: Partial<Preview>; config?: Partial<RedactionConfig> } = {},
) {
  let current = makePreview({ ...options.preview, allow_unmasked: options.config?.allow_unmasked ?? false })
  return [
    http.get('*/api/redaction/config', () => HttpResponse.json(makeRedactionConfig(options.config))),
    http.get('*/api/documents/d-1/versions/v-1/fragments', () => HttpResponse.json(makeFragments())),
    http.post('*/api/documents/d-1/versions/v-1/redaction-previews', () => {
      log.created += 1
      current = { ...current, id: `rp-${log.created}`, stale: log.created > 1 ? false : current.stale }
      return HttpResponse.json(current, { status: 201 })
    }),
    http.get('*/api/redaction-previews/:id', () => HttpResponse.json(current)),
    http.patch('*/api/redaction-previews/:id', async ({ request }) => {
      const body = (await request.json()) as { ops: { op: string; entity_id?: string; masked?: boolean }[] }
      log.edits.push(body)
      for (const op of body.ops) {
        if (op.op === 'set_entity') {
          current = {
            ...current,
            entities: current.entities.map((e) =>
              e.id === op.entity_id ? { ...e, masked: Boolean(op.masked), label: op.masked ? e.label : null } : e,
            ),
          }
        }
      }
      return HttpResponse.json(current)
    }),
    http.post('*/api/redaction-previews/:id/confirm', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>
      log.confirms.push(body)
      current = { ...current, status: 'confirmed', mode: body.mode === 'unmasked' ? 'unmasked_by_user' : 'masked' }
      return HttpResponse.json(current)
    }),
  ]
}

export function makeRedactionSettings(overrides: Partial<RedactionSettingsOut> = {}): RedactionSettingsOut {
  return {
    settings: {
      categories: Object.fromEntries(REDACTION_CATEGORIES.map((c) => [c.code, c.default_enabled])),
      allow_unmasked: false,
      custom_dictionary: [],
      custom_patterns: [],
      allow_list: [],
      preview_ttl_hours: 24,
    },
    revision: 0,
    updated_at: null,
    updated_by_id: null,
    can_manage: false,
    categories: REDACTION_CATEGORIES,
    ...overrides,
  }
}

export const redactionSettingsHandler = (overrides: Partial<RedactionSettingsOut> = {}) =>
  http.get('*/api/admin/redaction', () => HttpResponse.json(makeRedactionSettings(overrides)))
