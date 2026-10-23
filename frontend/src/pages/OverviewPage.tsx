import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'

import { ACTIVE_ANALYSIS_STATUSES, ANALYSES_KEY, fetchAnalyses } from '../api/analyses'
import { DOCUMENTS_KEY, fetchDocuments } from '../api/documents'
import { useCurrentUser } from '../app/session'
import { ErrorNotice } from '../components/ErrorNotice'
import { PageTitle } from '../components/ui'
import { ru } from '../i18n/ru'
import { formatDateTime } from '../lib/format'
import { AnalysesTable } from './analyses/AnalysesTable'
import { StatusBadge } from './documents/StatusBadge'

const t = ru.overview
const POLL_MS = 3000

export function OverviewPage() {
  const me = useCurrentUser()
  const analyses = useQuery({
    queryKey: [...ANALYSES_KEY, 'overview'],
    queryFn: ({ signal }) => fetchAnalyses({ limit: 10 }, signal),
    refetchInterval: (q) => (q.state.data?.items.some((a) => ACTIVE_ANALYSIS_STATUSES.has(a.status)) ? POLL_MS : false),
  })
  const documents = useQuery({
    queryKey: [...DOCUMENTS_KEY, 'list', 'overview'],
    queryFn: ({ signal }) => fetchDocuments(null, signal),
  })
  const active = analyses.data?.items.filter((a) => ACTIVE_ANALYSIS_STATUSES.has(a.status)) ?? []
  const recent = analyses.data?.items.slice(0, 5) ?? []
  const docs = documents.data?.items.slice(0, 5) ?? []

  return (
    <section aria-labelledby="overview-title" className="space-y-6">
      <PageTitle
        id="overview-title"
        actions={
          me.capabilities.upload_documents && (
            <Link to="/analyses/new" className="rounded-md border border-blue-700 bg-blue-700 px-3 py-1.5 text-sm text-white hover:bg-blue-800">
              {t.newAnalysis}
            </Link>
          )
        }
      >
        {t.title}
      </PageTitle>

      {analyses.isError && <ErrorNotice error={analyses.error} onRetry={() => void analyses.refetch()} />}
      {active.length > 0 && (
        <section aria-labelledby="active-title" className="space-y-2">
          <h2 id="active-title" className="text-lg font-semibold">
            {t.active}
          </h2>
          <AnalysesTable items={active} />
        </section>
      )}

      <section aria-labelledby="recent-analyses-title" className="space-y-2">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="recent-analyses-title" className="text-lg font-semibold">
            {t.recentAnalyses}
          </h2>
          <Link to="/analyses" className="text-sm text-blue-800 underline">
            {t.allAnalyses}
          </Link>
        </div>
        {analyses.isPending && <p aria-live="polite">{ru.common.loading}</p>}
        {analyses.isSuccess && recent.length === 0 && <p className="text-sm text-slate-600">{t.noAnalyses}</p>}
        {recent.length > 0 && <AnalysesTable items={recent} />}
      </section>

      <section aria-labelledby="recent-documents-title" className="space-y-2">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="recent-documents-title" className="text-lg font-semibold">
            {t.recentDocuments}
          </h2>
          <Link to="/documents" className="text-sm text-blue-800 underline">
            {t.allDocuments}
          </Link>
        </div>
        {documents.isPending && <p aria-live="polite">{ru.common.loading}</p>}
        {documents.isError && <ErrorNotice error={documents.error} onRetry={() => void documents.refetch()} />}
        {documents.isSuccess && docs.length === 0 && (
          <p className="text-sm text-slate-600">
            {t.noDocuments} {me.capabilities.upload_documents && t.uploadHint}
          </p>
        )}
        {docs.length > 0 && (
          <ul className="divide-y divide-slate-100 rounded-lg border border-slate-300 bg-white shadow-sm text-sm">
            {docs.map((d) => (
              <li key={d.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                <Link to={`/documents/${encodeURIComponent(d.id)}`} className="break-words text-blue-800 underline">
                  {d.title}
                </Link>
                <span className="flex items-center gap-2 text-slate-600">
                  {d.current_version && <StatusBadge status={d.current_version.extraction_status} />}
                  {formatDateTime(d.created_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </section>
  )
}
