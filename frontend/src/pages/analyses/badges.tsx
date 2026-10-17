import type { AnalysisStatus, RiskKind, Severity, ValidationStatus } from '../../api/analyses'
import { ru } from '../../i18n/ru'
import { validationHint } from './labels'

const t = ru.analyses

const base = 'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium'

const STATUS_TONE: Record<AnalysisStatus, string> = {
  queued: 'border-slate-300 bg-slate-100 text-slate-800',
  running: 'border-blue-300 bg-blue-50 text-blue-900',
  verifying: 'border-blue-300 bg-blue-50 text-blue-900',
  completed: 'border-green-300 bg-green-50 text-green-900',
  completed_with_warnings: 'border-amber-300 bg-amber-50 text-amber-900',
  failed: 'border-red-300 bg-red-50 text-red-900',
  cancelling: 'border-slate-300 bg-slate-100 text-slate-800',
  cancelled: 'border-slate-300 bg-slate-100 text-slate-800',
}

export function AnalysisStatusBadge({ status }: { status: AnalysisStatus }) {
  return <span className={`${base} ${STATUS_TONE[status]}`}>{t.status[status] ?? status}</span>
}

const SEVERITY_TONE: Record<Severity, string> = {
  critical: 'border-red-400 bg-red-100 text-red-950',
  high: 'border-orange-300 bg-orange-50 text-orange-950',
  medium: 'border-amber-300 bg-amber-50 text-amber-950',
  low: 'border-slate-300 bg-slate-50 text-slate-800',
  informational: 'border-slate-200 bg-white text-slate-700',
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <span className={`${base} ${SEVERITY_TONE[severity]}`}>
      {t.risks.severity}: {t.risks.severityLabels[severity]}
    </span>
  )
}

// Три состояния различаются не только цветом, но и значком и текстом.
const VALIDATION_STYLE: Record<ValidationStatus, { tone: string; icon: string }> = {
  verified_match: { tone: 'border-green-400 bg-green-50 text-green-900', icon: '✓' },
  needs_review: { tone: 'border-amber-400 bg-amber-50 text-amber-950', icon: '!' },
  rejected: { tone: 'border-red-300 bg-red-50 text-red-900', icon: '✕' },
  pending: { tone: 'border-slate-300 bg-slate-50 text-slate-800', icon: '…' },
}

export function ValidationBadge({ status, kind }: { status: ValidationStatus; kind: RiskKind }) {
  const style = VALIDATION_STYLE[status]
  return (
    <span className={`${base} ${style.tone}`} title={validationHint(status, kind)}>
      <span aria-hidden="true">{style.icon}</span>
      {t.risks.validationLabels[status]}
    </span>
  )
}
