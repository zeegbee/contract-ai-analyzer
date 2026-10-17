import type { RiskKind, ValidationStatus } from '../../api/analyses'
import { ru } from '../../i18n/ru'

const hints = ru.analyses.risks.validationHints

/** Пояснение статуса проверки; для отсутствующего условия — отдельный текст (цитаты нет по определению). */
export function validationHint(status: ValidationStatus, kind: RiskKind): string {
  return status === 'needs_review' && kind === 'missing_clause'
    ? (hints.needs_review_missing ?? '')
    : (hints[status] ?? '')
}
