import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'

import { ANALYSES_KEY, fetchAnalyses } from '../../api/analyses'
import {
  ACTIVE_STATUSES,
  deleteDocument,
  DOCUMENTS_KEY,
  type ExtractionWarning,
  fetchDocument,
  originalFileUrl,
  retryExtraction,
  type Version,
} from '../../api/documents'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Button, Notice, PageTitle } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatBytes, formatDateTime } from '../../lib/format'
import { AnalysesTable } from '../analyses/AnalysesTable'
import { AccessPanel } from './AccessPanel'
import { StatusBadge } from './StatusBadge'
import { TextViewer } from './TextViewer'

const POLL_MS = 2000
const t = ru.documents.detail

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap gap-x-2">
      <dt className="text-slate-600">{label}:</dt>
      <dd className="break-all">{children}</dd>
    </div>
  )
}

function warningText(w: ExtractionWarning): string {
  const base = ru.documents.warnings[w.code] ?? w.code
  if (w.code === 'pages_without_text' && Array.isArray(w.pages)) {
    return `${base} (${ru.documents.text.pageShort} ${w.pages.join(', ')})`
  }
  return base
}

function ExtractionState({ version, canManage, onRetry, retrying }: {
  version: Version
  canManage: boolean
  onRetry: () => void
  retrying: boolean
}) {
  const code = version.extraction_error_code
  if (ACTIVE_STATUSES.has(version.extraction_status)) {
    return (
      <Notice>
        {t.processing}
        {code && ` ${ru.documents.errors[code] ?? ''}`}
      </Notice>
    )
  }
  if (version.extraction_status === 'unsupported_no_text') return <Notice tone="warning">{t.noText}</Notice>
  if (version.extraction_status === 'failed') {
    return (
      <div role="alert" className="space-y-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900">
        <p>
          {t.failed} {code ? (ru.documents.errors[code] ?? code) : ''}
        </p>
        {canManage && (
          <Button onClick={onRetry} disabled={retrying}>
            {t.retry}
          </Button>
        )}
      </div>
    )
  }
  return null
}

function DocumentAnalyses({ documentId, canAnalyze, ready }: { documentId: string; canAnalyze: boolean; ready: boolean }) {
  const query = useQuery({
    queryKey: [...ANALYSES_KEY, 'document', documentId],
    queryFn: ({ signal }) => fetchAnalyses({ documentId, limit: 20 }, signal),
  })
  return (
    <section aria-labelledby="document-analyses-title" className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="document-analyses-title" className="text-lg font-semibold">
          {t.analyses}
        </h2>
        {canAnalyze && ready && (
          <Link
            to={`/analyses/new?document=${encodeURIComponent(documentId)}`}
            className="rounded-md border border-blue-700 bg-blue-700 px-3 py-1.5 text-sm text-white hover:bg-blue-800"
          >
            {t.startAnalysis}
          </Link>
        )}
      </div>
      {query.isError && <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />}
      {query.isSuccess && query.data.items.length === 0 && <p className="text-sm text-slate-600">{t.noAnalyses}</p>}
      {query.data && query.data.items.length > 0 && <AnalysesTable items={query.data.items} showDocument={false} />}
    </section>
  )
}

export function DocumentPage() {
  const { documentId = '' } = useParams()
  const [search] = useSearchParams()
  const focusKey = search.get('fragment')
  const focusPageRaw = Number(search.get('page'))
  const focusPage = Number.isInteger(focusPageRaw) && focusPageRaw > 0 ? focusPageRaw : null
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const key = [...DOCUMENTS_KEY, documentId]
  const query = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => fetchDocument(documentId, signal),
    refetchInterval: (q) => {
      const status = q.state.data?.current_version_detail?.extraction_status
      return status && ACTIVE_STATUSES.has(status) ? POLL_MS : false
    },
  })
  const remove = useMutation({
    mutationFn: () => deleteDocument(documentId),
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: key })
      void queryClient.invalidateQueries({ queryKey: DOCUMENTS_KEY })
      void navigate('/documents', { replace: true })
    },
  })
  const retry = useMutation({
    mutationFn: (versionId: string) => retryExtraction(documentId, versionId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: key }),
  })

  if (query.isPending) return <p aria-live="polite">{ru.common.loading}</p>
  if (query.isError) return <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />

  const doc = query.data
  const version = doc.current_version_detail
  return (
    <section aria-labelledby="document-title" className="space-y-4">
      <Link to="/documents" className="text-sm text-blue-800 hover:underline">
        ← {t.back}
      </Link>
      <PageTitle
        id="document-title"
        actions={
          <div className="flex flex-wrap gap-2">
            {version && (
              <a
                href={originalFileUrl(doc.id, version.id)}
                download
                className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-100"
              >
                {t.download}
              </a>
            )}
            {doc.can_manage && (
              <Button
                variant="danger"
                disabled={remove.isPending}
                onClick={() => window.confirm(t.confirmDelete) && remove.mutate()}
              >
                {t.delete}
              </Button>
            )}
          </div>
        }
      >
        {doc.title}
      </PageTitle>
      {remove.isError && <ErrorNotice error={remove.error} />}

      {version && (
        <dl className="grid gap-1 rounded-lg border border-slate-300 bg-white shadow-sm p-4 text-sm sm:grid-cols-2">
          <Row label={t.file}>{version.original_filename}</Row>
          <Row label={t.format}>
            {ru.documents.formats[version.detected_format]}
            {version.converted_from_doc && ` · ${t.convertedFromDoc}`}
          </Row>
          <Row label={t.size}>{formatBytes(version.size_bytes)}</Row>
          <Row label={t.uploaded}>{formatDateTime(version.created_at)}</Row>
          <Row label={t.owner}>{doc.is_owner ? ru.documents.you : doc.owner_display_name}</Row>
          <Row label={t.access}>{ru.documents.access[doc.access]}</Row>
          <Row label={ru.documents.columns.status}>
            <StatusBadge status={version.extraction_status} />
          </Row>
          {version.page_count !== null && <Row label={t.pages}>{version.page_count}</Row>}
          {version.extraction_status === 'ready' && <Row label={t.fragments}>{version.fragment_count}</Row>}
          <Row label={t.sha256}>
            <code className="text-xs">{version.sha256}</code>
          </Row>
          {version.extractor_version && <Row label={t.extractor}>{version.extractor_version}</Row>}
        </dl>
      )}

      {version && (
        <ExtractionState
          version={version}
          canManage={doc.can_manage}
          retrying={retry.isPending}
          onRetry={() => retry.mutate(version.id)}
        />
      )}
      {retry.isError && <ErrorNotice error={retry.error} />}

      {version && version.warnings.length > 0 && (
        <section aria-labelledby="warnings-title" className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm">
          <h2 id="warnings-title" className="font-medium text-amber-900">
            {t.warnings}
          </h2>
          <ul className="mt-1 list-disc pl-5 text-amber-900">
            {version.warnings.map((w) => (
              <li key={w.code}>{warningText(w)}</li>
            ))}
          </ul>
        </section>
      )}

      {version?.extraction_status === 'ready' && (
        <TextViewer
          documentId={doc.id}
          versionId={version.id}
          pageCount={version.page_count}
          pageSource={version.page_source}
          focusKey={focusKey}
          focusPage={focusPage}
        />
      )}

      <DocumentAnalyses
        documentId={doc.id}
        canAnalyze={doc.can_analyze}
        ready={version?.extraction_status === 'ready'}
      />

      {doc.can_share && <AccessPanel documentId={doc.id} />}
    </section>
  )
}
