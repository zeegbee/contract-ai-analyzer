import { useInfiniteQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'

import {
  ACTIVE_ANALYSIS_STATUSES,
  ANALYSES_KEY,
  type AnalysisStatus,
  analysisStatusSchema,
  fetchAnalyses,
} from '../../api/analyses'
import { useCurrentUser } from '../../app/session'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Button, inputClass, PageTitle } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { AnalysesTable } from './AnalysesTable'

const POLL_MS = 3000

export function AnalysesPage() {
  const me = useCurrentUser()
  const [status, setStatus] = useState<AnalysisStatus | ''>('')
  const query = useInfiniteQuery({
    queryKey: [...ANALYSES_KEY, 'list', status],
    queryFn: ({ pageParam, signal }) => fetchAnalyses({ status, cursor: pageParam }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    refetchInterval: (q) =>
      q.state.data?.pages.some((p) => p.items.some((a) => ACTIVE_ANALYSIS_STATUSES.has(a.status))) ? POLL_MS : false,
  })
  const items = query.data?.pages.flatMap((p) => p.items) ?? []

  return (
    <section aria-labelledby="analyses-title" className="space-y-4">
      <PageTitle
        id="analyses-title"
        actions={
          me.capabilities.upload_documents && (
            <Link to="/analyses/new" className="rounded-md border border-blue-700 bg-blue-700 px-3 py-1.5 text-sm text-white hover:bg-blue-800">
              {ru.analyses.new}
            </Link>
          )
        }
      >
        {ru.analyses.title}
      </PageTitle>
      <select
        aria-label={ru.analyses.filterStatus}
        className={`${inputClass} max-w-xs`}
        value={status}
        onChange={(e) => setStatus(e.target.value as AnalysisStatus | '')}
      >
        <option value="">{ru.analyses.allStatuses}</option>
        {analysisStatusSchema.options.map((s) => (
          <option key={s} value={s}>
            {ru.analyses.status[s]}
          </option>
        ))}
      </select>
      {query.isPending && <p aria-live="polite">{ru.common.loading}</p>}
      {query.isError && <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />}
      {query.isSuccess && items.length === 0 && <p className="text-slate-600">{ru.analyses.empty}</p>}
      {items.length > 0 && <AnalysesTable items={items} />}
      {query.hasNextPage && (
        <Button disabled={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>
          {ru.common.loadMore}
        </Button>
      )}
    </section>
  )
}
