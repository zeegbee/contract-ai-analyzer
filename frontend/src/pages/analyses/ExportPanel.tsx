import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ANALYSES_KEY, createExport, type ExportFormat, exportFormatSchema, fetchExports } from '../../api/analyses'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Button } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatBytes, formatDateTime } from '../../lib/format'

const t = ru.exports
const POLL_MS = 2000

export function ExportPanel({ analysisId, finished }: { analysisId: string; finished: boolean }) {
  const queryClient = useQueryClient()
  const key = [...ANALYSES_KEY, analysisId, 'exports']
  const query = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => fetchExports(analysisId, signal),
    enabled: finished,
    refetchInterval: (q) => (q.state.data?.some((e) => e.status === 'queued') ? POLL_MS : false),
  })
  const create = useMutation({
    mutationFn: (format: ExportFormat) => createExport(analysisId, format),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: key }),
  })

  return (
    <section aria-labelledby="exports-title" className="space-y-3 rounded-lg border border-slate-300 bg-white shadow-sm p-4">
      <h2 id="exports-title" className="text-lg font-semibold">
        {t.title}
      </h2>
      {!finished ? (
        <p className="text-sm text-slate-700">{t.notReady}</p>
      ) : (
        <>
          <p className="text-sm text-slate-700">{t.hint}</p>
          <div className="flex flex-wrap gap-2">
            {exportFormatSchema.options.map((f) => (
              <Button key={f} disabled={create.isPending} onClick={() => create.mutate(f)}>
                {t.formats[f]}
              </Button>
            ))}
          </div>
          {create.isError && <ErrorNotice error={create.error} />}
          {query.isError && <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />}
          {query.data && query.data.length === 0 && <p className="text-sm text-slate-600">{t.empty}</p>}
          {query.data && query.data.length > 0 && (
            <ul className="divide-y divide-slate-100 text-sm" aria-live="polite">
              {query.data.map((e) => (
                <li key={e.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                  <span>
                    {t.formats[e.format]} · {formatDateTime(e.created_at)} · {t.status[e.status]}
                    {e.size_bytes !== null && ` · ${formatBytes(e.size_bytes)}`}
                    {e.expires_at && e.status === 'ready' && ` · ${t.expires} ${formatDateTime(e.expires_at)}`}
                  </span>
                  {e.download_url && (
                    <a
                      href={e.download_url}
                      download
                      className="rounded-md border border-slate-300 bg-white px-3 py-1 hover:bg-slate-100"
                    >
                      {t.download}
                    </a>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  )
}
