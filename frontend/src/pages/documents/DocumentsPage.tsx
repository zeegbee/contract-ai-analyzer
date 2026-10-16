import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'

import { ACTIVE_STATUSES, DOCUMENTS_KEY, fetchDocuments, fetchUploadLimits } from '../../api/documents'
import { useCurrentUser } from '../../app/session'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Button, PageTitle } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatBytes, formatDateTime } from '../../lib/format'
import { StatusBadge } from './StatusBadge'
import { UploadPanel } from './UploadPanel'

const POLL_MS = 3000
const c = ru.documents.columns

export function DocumentsPage() {
  const me = useCurrentUser()
  const limits = useQuery({
    queryKey: [...DOCUMENTS_KEY, 'upload-limits'],
    queryFn: ({ signal }) => fetchUploadLimits(signal),
    enabled: me.capabilities.upload_documents,
    staleTime: 5 * 60_000,
  })
  const query = useInfiniteQuery({
    queryKey: [...DOCUMENTS_KEY, 'list'],
    queryFn: ({ pageParam, signal }) => fetchDocuments(pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    // Пока у какого-то документа извлекается текст — обновляем список.
    refetchInterval: (q) =>
      q.state.data?.pages.some((p) =>
        p.items.some((d) => d.current_version && ACTIVE_STATUSES.has(d.current_version.extraction_status)),
      )
        ? POLL_MS
        : false,
  })
  const documents = query.data?.pages.flatMap((p) => p.items) ?? []

  return (
    <section aria-labelledby="documents-title" className="space-y-4">
      <PageTitle id="documents-title">{ru.documents.title}</PageTitle>

      {me.capabilities.upload_documents && limits.data && <UploadPanel limits={limits.data} />}
      {limits.isError && <ErrorNotice error={limits.error} onRetry={() => void limits.refetch()} />}

      {query.isPending && <p aria-live="polite">{ru.common.loading}</p>}
      {query.isError && <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />}
      {query.isSuccess && documents.length === 0 && (
        <p className="rounded-md border border-slate-200 bg-white p-4 text-sm text-slate-700">
          {me.capabilities.upload_documents ? ru.documents.empty : ru.documents.emptyViewer}
        </p>
      )}
      {documents.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 bg-slate-50 text-xs text-slate-700">
              <tr>
                <th className="px-3 py-2 font-medium">{c.title}</th>
                <th className="px-3 py-2 font-medium">{c.format}</th>
                <th className="px-3 py-2 font-medium">{c.size}</th>
                <th className="px-3 py-2 font-medium">{c.status}</th>
                <th className="px-3 py-2 font-medium">{c.pages}</th>
                <th className="px-3 py-2 font-medium">{c.owner}</th>
                <th className="px-3 py-2 font-medium">{c.uploaded}</th>
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => {
                const v = doc.current_version
                return (
                  <tr key={doc.id} className="border-b border-slate-100 last:border-0">
                    <td className="px-3 py-2">
                      <Link to={`/documents/${doc.id}`} className="text-blue-800 hover:underline">
                        {doc.title}
                      </Link>
                    </td>
                    <td className="px-3 py-2">{v ? ru.documents.formats[v.detected_format] : '—'}</td>
                    <td className="px-3 py-2 whitespace-nowrap">{v ? formatBytes(v.size_bytes) : '—'}</td>
                    <td className="px-3 py-2">{v ? <StatusBadge status={v.extraction_status} /> : '—'}</td>
                    <td className="px-3 py-2">{v?.page_count ?? '—'}</td>
                    <td className="px-3 py-2">{doc.is_owner ? ru.documents.you : doc.owner_display_name}</td>
                    <td className="px-3 py-2 whitespace-nowrap">{formatDateTime(doc.created_at)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      {query.hasNextPage && (
        <Button onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}>
          {ru.common.loadMore}
        </Button>
      )}
    </section>
  )
}
