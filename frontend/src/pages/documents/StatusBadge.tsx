import type { ExtractionStatus } from '../../api/documents'
import { ru } from '../../i18n/ru'

const TONES: Record<ExtractionStatus, string> = {
  pending: 'border-slate-300 bg-slate-50 text-slate-800',
  processing: 'border-blue-200 bg-blue-50 text-blue-900',
  ready: 'border-green-200 bg-green-50 text-green-900',
  failed: 'border-red-200 bg-red-50 text-red-900',
  unsupported_no_text: 'border-amber-200 bg-amber-50 text-amber-900',
}

export function StatusBadge({ status }: { status: ExtractionStatus }) {
  return (
    <span className={`inline-block rounded border px-1.5 py-0.5 text-xs whitespace-nowrap ${TONES[status]}`}>
      {ru.documents.status[status] ?? status}
    </span>
  )
}
