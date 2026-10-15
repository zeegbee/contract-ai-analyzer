import { useQuery } from '@tanstack/react-query'

import { fetchReadiness } from '../api/health'
import { ErrorNotice } from '../components/ErrorNotice'
import { ru } from '../i18n/ru'

export function StatusPage() {
  const query = useQuery({
    queryKey: ['health', 'ready'],
    queryFn: ({ signal }) => fetchReadiness(signal),
    refetchInterval: 30_000,
  })

  return (
    <section aria-labelledby="status-title" className="space-y-4">
      <div className="flex items-center justify-between gap-4">
        <h1 id="status-title" className="text-xl font-semibold">
          {ru.status.title}
        </h1>
        <button
          type="button"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
          className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-100 disabled:opacity-50"
        >
          {ru.status.refresh}
        </button>
      </div>

      {query.isPending && <p aria-live="polite">{ru.status.loading}</p>}
      {query.isError && <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />}
      {query.data && (
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <p
            aria-live="polite"
            className={query.data.status === 'ready' ? 'font-medium text-green-800' : 'font-medium text-amber-800'}
          >
            {query.data.status === 'ready' ? ru.status.ready : ru.status.notReady}
          </p>
          <dl className="mt-3 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
            {Object.entries(query.data.checks).map(([name, value]) => (
              <div key={name} className="contents">
                <dt className="text-slate-600">{ru.status.checks[name] ?? name}</dt>
                <dd>{ru.status.values[value] ?? value}</dd>
              </div>
            ))}
            <dt className="text-slate-600">{ru.status.version}</dt>
            <dd>{query.data.version}</dd>
          </dl>
        </div>
      )}
    </section>
  )
}
