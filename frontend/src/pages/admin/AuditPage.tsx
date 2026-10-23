import { useInfiniteQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { fetchAudit } from '../../api/audit'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Button, inputClass, PageTitle } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatDateTime } from '../../lib/format'

function formatDetails(details: Record<string, unknown>): string {
  const entries = Object.entries(details)
  if (entries.length === 0) return ''
  return entries.map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`).join('; ')
}

export function AuditPage() {
  const [action, setAction] = useState('')
  const query = useInfiniteQuery({
    queryKey: ['audit', action],
    queryFn: ({ pageParam, signal }) => fetchAudit({ action, beforeId: pageParam }, signal),
    initialPageParam: null as number | null,
    getNextPageParam: (last) => last.next_cursor,
  })
  const entries = query.data?.pages.flatMap((p) => p.items) ?? []

  return (
    <section aria-labelledby="audit-title" className="space-y-4">
      <PageTitle id="audit-title">{ru.audit.title}</PageTitle>
      <select
        aria-label={ru.audit.filter}
        className={`${inputClass} max-w-xs`}
        value={action}
        onChange={(e) => setAction(e.target.value)}
      >
        <option value="">{ru.audit.allActions}</option>
        {Object.entries(ru.audit.groups).map(([prefix, label]) => (
          <option key={prefix} value={prefix}>
            {label}
          </option>
        ))}
        {Object.entries(ru.audit.actions).map(([code, label]) => (
          <option key={code} value={code}>
            — {label}
          </option>
        ))}
      </select>

      {query.isPending && <p>{ru.common.loading}</p>}
      {query.isError && <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />}
      {query.isSuccess && entries.length === 0 && <p className="text-slate-600">{ru.audit.empty}</p>}
      {entries.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-300 bg-white shadow-sm">
          <table className="w-full text-left text-sm">
            <thead className="bg-slate-50 text-slate-600">
              <tr>
                <th className="px-3 py-2 font-medium">{ru.audit.columns.time}</th>
                <th className="px-3 py-2 font-medium">{ru.audit.columns.actor}</th>
                <th className="px-3 py-2 font-medium">{ru.audit.columns.action}</th>
                <th className="px-3 py-2 font-medium">{ru.audit.columns.target}</th>
                <th className="px-3 py-2 font-medium">{ru.audit.columns.ip}</th>
                <th className="px-3 py-2 font-medium">{ru.audit.columns.details}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200">
              {entries.map((e) => (
                <tr key={e.id} className="align-top">
                  <td className="whitespace-nowrap px-3 py-2">{formatDateTime(e.created_at)}</td>
                  <td className="px-3 py-2">{e.actor_email ?? ru.audit.system}</td>
                  <td className="px-3 py-2">{ru.audit.actions[e.action] ?? e.action}</td>
                  <td className="px-3 py-2 font-mono text-xs">
                    {e.target_type ? `${e.target_type}: ${e.target_id ?? ''}` : '—'}
                  </td>
                  <td className="px-3 py-2">{e.ip ?? '—'}</td>
                  <td className="px-3 py-2 text-xs text-slate-700">{formatDetails(e.details)}</td>
                </tr>
              ))}
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
