import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'

import { fetchMySessions, revokeMySession, revokeOtherSessions } from '../api/auth'
import { AVAILABLE_MODELS_KEY, fetchAvailableModels } from '../api/catalog'
import { useCurrentUser } from '../app/session'
import { ErrorNotice } from '../components/ErrorNotice'
import { Button, Notice, PageTitle } from '../components/ui'
import { ru } from '../i18n/ru'
import { formatDateTime } from '../lib/format'
import { pricingText } from '../lib/pricing'

const SESSIONS_KEY = ['auth', 'sessions'] as const

/** Справочный список моделей, доступных пользователю (выбор модели и согласие — в «Новом анализе»). */
function AvailableModels() {
  const t = ru.llm.available
  const query = useQuery({ queryKey: AVAILABLE_MODELS_KEY, queryFn: ({ signal }) => fetchAvailableModels(signal) })
  if (query.isPending) return <p>{ru.common.loading}</p>
  if (query.isError) return <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />
  const { items, cloud_models_enabled: cloudEnabled } = query.data
  return (
    <div className="space-y-2">
      {!cloudEnabled && <p className="text-xs text-slate-600">{t.cloudOff}</p>}
      {items.length === 0 ? (
        <p className="text-sm text-slate-600">{t.empty}</p>
      ) : (
        <ul className="divide-y divide-slate-200 rounded-lg border border-slate-300 bg-white shadow-sm text-sm">
          {items.map((m) => (
            <li key={m.id} className="space-y-0.5 p-3">
              <p className="font-medium">
                {m.display_name}
                <span
                  className={`ml-2 rounded px-1.5 text-xs ${m.is_cloud ? 'bg-amber-100 text-amber-900' : 'bg-green-100 text-green-900'}`}
                >
                  {m.is_cloud ? t.cloudBadge : t.localBadge}
                </span>
                {m.is_default && (
                  <span className="ml-2 rounded bg-slate-100 px-1.5 text-xs text-slate-800">{ru.llm.models.default}</span>
                )}
              </p>
              <p className="text-slate-600">
                {m.provider_name} · {ru.llm.models.contextWindow}: {m.context_window ?? ru.common.unknown} ·{' '}
                {ru.llm.models.pricing}: {pricingText(m.pricing)}
              </p>
            </li>
          ))}
        </ul>
      )}
      <p className="text-xs text-slate-600">{t.hint}</p>
    </div>
  )
}

export function AccountPage() {
  const me = useCurrentUser()
  const queryClient = useQueryClient()
  const sessions = useQuery({ queryKey: SESSIONS_KEY, queryFn: ({ signal }) => fetchMySessions(signal) })
  const refresh = () => void queryClient.invalidateQueries({ queryKey: SESSIONS_KEY })
  const revokeOne = useMutation({ mutationFn: revokeMySession, onSuccess: refresh })
  const revokeOthers = useMutation({ mutationFn: revokeOtherSessions, onSuccess: refresh })
  const others = sessions.data?.filter((s) => !s.current).length ?? 0

  return (
    <section aria-labelledby="account-title" className="space-y-6">
      <PageTitle id="account-title">{ru.account.title}</PageTitle>
      <dl className="grid max-w-lg grid-cols-[max-content_1fr] gap-x-6 gap-y-1 rounded-lg border border-slate-300 bg-white shadow-sm p-4 text-sm">
        <dt className="text-slate-600">{ru.users.columns.name}</dt>
        <dd>{me.display_name}</dd>
        <dt className="text-slate-600">{ru.account.email}</dt>
        <dd>{me.email}</dd>
        <dt className="text-slate-600">{ru.account.role}</dt>
        <dd>{ru.roles[me.role]}</dd>
      </dl>
      <Link to="/change-password" className="inline-block text-sm text-blue-700 underline">
        {ru.account.changePassword}
      </Link>

      {me.capabilities.upload_documents && (
        <section aria-labelledby="available-models-title" className="max-w-2xl space-y-3">
          <h2 id="available-models-title" className="text-lg font-semibold">
            {ru.llm.available.title}
          </h2>
          <AvailableModels />
        </section>
      )}

      <section aria-labelledby="sessions-title" className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="sessions-title" className="text-lg font-semibold">
            {ru.account.sessionsTitle}
          </h2>
          <Button onClick={() => revokeOthers.mutate()} disabled={others === 0 || revokeOthers.isPending}>
            {ru.account.revokeOthers}
          </Button>
        </div>
        {revokeOthers.isSuccess && <Notice tone="success">{ru.account.revokedOthers(revokeOthers.data)}</Notice>}
        {(revokeOne.isError || revokeOthers.isError) && <ErrorNotice error={revokeOne.error ?? revokeOthers.error} />}
        {sessions.isPending && <p>{ru.common.loading}</p>}
        {sessions.isError && <ErrorNotice error={sessions.error} onRetry={() => void sessions.refetch()} />}
        {sessions.data && (
          <ul className="divide-y divide-slate-200 rounded-lg border border-slate-300 bg-white shadow-sm">
            {sessions.data.map((s) => (
              <li key={s.id} className="flex flex-wrap items-center justify-between gap-3 p-3 text-sm">
                <div className="space-y-0.5">
                  <p className="font-medium">
                    {s.user_agent ?? ru.common.unknown}
                    {s.current && <span className="ml-2 rounded bg-green-100 px-1.5 text-xs text-green-900">{ru.account.sessionCurrent}</span>}
                  </p>
                  <p className="text-slate-600">
                    {ru.account.sessionIp}: {s.ip ?? '—'} · {ru.account.sessionStarted}: {formatDateTime(s.created_at)} ·{' '}
                    {ru.account.sessionLastSeen}: {formatDateTime(s.last_seen_at)} · {ru.account.sessionExpires}:{' '}
                    {formatDateTime(s.expires_at)}
                  </p>
                </div>
                {!s.current && (
                  <Button variant="danger" onClick={() => revokeOne.mutate(s.id)} disabled={revokeOne.isPending}>
                    {ru.account.sessionRevoke}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </section>
  )
}
