import { Link } from 'react-router'

import type { AnalysisSummary } from '../../api/analyses'
import { ru } from '../../i18n/ru'
import { formatDateTime } from '../../lib/format'
import { AnalysisStatusBadge } from './badges'

const c = ru.analyses.columns

function progress(a: AnalysisSummary): string {
  const { stage, chunks_done: done, chunks_total: total } = a.progress
  const label = ru.analyses.stages[stage ?? ''] ?? ''
  return total ? `${label} · ${done ?? 0}/${total}` : label
}

export function AnalysesTable({ items, showDocument = true }: { items: AnalysisSummary[]; showDocument?: boolean }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-300 bg-white shadow-sm">
      <table className="w-full text-left text-sm">
        <thead className="bg-slate-50 text-slate-600">
          <tr>
            {showDocument && <th className="px-3 py-2 font-medium">{c.document}</th>}
            <th className="px-3 py-2 font-medium">{c.status}</th>
            <th className="px-3 py-2 font-medium">{c.progress}</th>
            <th className="px-3 py-2 font-medium">{c.model}</th>
            <th className="px-3 py-2 font-medium">{c.party}</th>
            <th className="px-3 py-2 font-medium">{c.created}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {items.map((a) => (
            <tr key={a.id}>
              {showDocument && (
                <td className="px-3 py-2">
                  <Link to={`/analyses/${encodeURIComponent(a.id)}`} className="text-blue-800 underline">
                    {a.document_title}
                  </Link>
                </td>
              )}
              <td className="px-3 py-2">
                {showDocument ? (
                  <AnalysisStatusBadge status={a.status} />
                ) : (
                  <Link to={`/analyses/${encodeURIComponent(a.id)}`} aria-label={ru.analyses.status[a.status]}>
                    <AnalysisStatusBadge status={a.status} />
                  </Link>
                )}
              </td>
              <td className="px-3 py-2 text-slate-700">{progress(a)}</td>
              <td className="px-3 py-2">{a.model_display_name ?? '—'}</td>
              <td className="px-3 py-2">{ru.analyses.party[a.party]}</td>
              <td className="px-3 py-2 whitespace-nowrap">{formatDateTime(a.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
