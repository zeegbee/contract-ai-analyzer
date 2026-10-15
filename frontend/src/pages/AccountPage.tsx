import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'

import { fetchMySessions, revokeMySession, revokeOtherSessions } from '../api/auth'
import { useCurrentUser } from '../app/session'
import { ErrorNotice } from '../components/ErrorNotice'
import { Button, Notice, PageTitle } from '../components/ui'
import { ru } from '../i18n/ru'
import { formatDateTime } from '../lib/format'

const SESSIONS_KEY = ['auth', 'sessions'] as const

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
      <dl className="grid max-w-lg grid-cols-[max-content_1fr] gap-x-6 gap-y-1 rounded-lg border border-slate-200 bg-white p-4 text-sm">
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
          <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white">
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
