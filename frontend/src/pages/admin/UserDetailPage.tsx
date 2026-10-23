import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router'

import type { Role } from '../../api/auth'
import {
  fetchModelPermissions,
  fetchUser,
  resetPassword,
  revokeUserSessions,
  saveModelPermissions,
  type ModelPermission,
  type TemporaryPassword,
  unlockUser,
  updateUser,
  type UserDetail,
  USERS_KEY,
} from '../../api/users'
import { useCurrentUser } from '../../app/session'
import { ErrorNotice } from '../../components/ErrorNotice'
import { TemporaryPasswordNotice } from '../../components/TemporaryPasswordNotice'
import { Button, Field, inputClass, Notice, PageTitle } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatDateTime } from '../../lib/format'

const t = ru.users.detail

function ProfileForm({ user, canEditRole, roles }: { user: UserDetail; canEditRole: boolean; roles: Role[] }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState(user.display_name)
  const [role, setRole] = useState<Role>(user.role)
  const mutation = useMutation({
    mutationFn: () =>
      updateUser(user.id, {
        ...(name.trim() !== user.display_name ? { display_name: name.trim() } : {}),
        ...(role !== user.role ? { role } : {}),
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData([...USERS_KEY, updated.id], updated)
      void queryClient.invalidateQueries({ queryKey: USERS_KEY })
    },
  })
  // Текущая роль показывается, даже если её нельзя назначить (например, Superadmin для Admin).
  const roleOptions = roles.includes(user.role) ? roles : [user.role, ...roles]
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault()
        mutation.mutate()
      }}
    >
      {mutation.isError && <ErrorNotice error={mutation.error} />}
      {mutation.isSuccess && <Notice tone="success">{t.saved}</Notice>}
      <div className="grid gap-3 sm:grid-cols-2">
        <Field id="user-name" label={t.displayName}>
          <input id="user-name" className={inputClass} value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field id="user-role" label={t.role}>
          <select
            id="user-role"
            className={inputClass}
            value={role}
            disabled={!canEditRole}
            onChange={(e) => setRole(e.target.value as Role)}
          >
            {roleOptions.map((r) => (
              <option key={r} value={r}>
                {ru.roles[r]}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <Button
        type="submit"
        variant="primary"
        disabled={mutation.isPending || !name.trim() || (name.trim() === user.display_name && role === user.role)}
      >
        {ru.common.save}
      </Button>
    </form>
  )
}

function ModelAccess({ userId, editable }: { userId: string; editable: boolean }) {
  const queryClient = useQueryClient()
  const key = [...USERS_KEY, userId, 'models']
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => fetchModelPermissions(userId, signal) })
  const [draft, setDraft] = useState<Record<string, boolean | null> | null>(null)
  const current = (m: ModelPermission) => (draft && m.model_config_id in draft ? draft[m.model_config_id] : m.override)
  const mutation = useMutation({
    mutationFn: (items: ModelPermission[]) =>
      saveModelPermissions(
        userId,
        items.flatMap((m) => {
          const value = current(m)
          return value === null || value === undefined ? [] : [{ model_config_id: m.model_config_id, allowed: value }]
        }),
      ),
    onSuccess: (items) => {
      queryClient.setQueryData(key, items)
      setDraft(null)
    },
  })

  return (
    <section aria-labelledby="models-title" className="space-y-3">
      <h2 id="models-title" className="text-lg font-semibold">
        {ru.users.models.title}
      </h2>
      <p className="text-sm text-slate-600">{ru.users.models.hint}</p>
      {query.isPending && <p>{ru.common.loading}</p>}
      {query.isError && <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />}
      {mutation.isError && <ErrorNotice error={mutation.error} />}
      {query.data?.length === 0 && <p className="text-sm text-slate-600">{ru.users.models.empty}</p>}
      {query.data && query.data.length > 0 && (
        <>
          <ul className="divide-y divide-slate-200 rounded-lg border border-slate-300 bg-white shadow-sm text-sm">
            {query.data.map((m) => {
              const value = current(m)
              return (
                <li key={m.model_config_id} className="flex flex-wrap items-center justify-between gap-3 p-3">
                  <div>
                    <p className="font-medium">{m.display_name}</p>
                    <p className="text-slate-600">
                      {m.external_model_id}
                      {!m.enabled && ` · ${ru.users.models.disabled}`}
                    </p>
                  </div>
                  <div className="flex items-center gap-3">
                    <select
                      aria-label={`${ru.users.models.title}: ${m.display_name}`}
                      className={`${inputClass} w-auto`}
                      disabled={!editable}
                      value={value === null || value === undefined ? 'role' : value ? 'allow' : 'deny'}
                      onChange={(e) =>
                        setDraft((d) => ({
                          ...d,
                          [m.model_config_id]: e.target.value === 'role' ? null : e.target.value === 'allow',
                        }))
                      }
                    >
                      <option value="role">
                        {ru.users.models.byRole} ({m.allowed_by_role ? ru.users.models.allow : ru.users.models.deny})
                      </option>
                      <option value="allow">{ru.users.models.allow}</option>
                      <option value="deny">{ru.users.models.deny}</option>
                    </select>
                    <span className={m.effective ? 'text-green-800' : 'text-slate-600'}>
                      {ru.users.models.effective}: {m.effective ? ru.users.models.available : ru.users.models.unavailable}
                    </span>
                  </div>
                </li>
              )
            })}
          </ul>
          {editable && (
            <Button
              variant="primary"
              disabled={!draft || mutation.isPending}
              onClick={() => query.data && mutation.mutate(query.data)}
            >
              {ru.users.models.save}
            </Button>
          )}
        </>
      )}
    </section>
  )
}

export function UserDetailPage() {
  const { userId = '' } = useParams()
  const me = useCurrentUser()
  const queryClient = useQueryClient()
  const key = [...USERS_KEY, userId]
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => fetchUser(userId, signal) })
  const [temporary, setTemporary] = useState<TemporaryPassword | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [now] = useState(() => Date.now())

  const onUpdated = (user: UserDetail) => {
    queryClient.setQueryData(key, user)
    void queryClient.invalidateQueries({ queryKey: USERS_KEY })
  }
  const toggleActive = useMutation({
    mutationFn: (active: boolean) => updateUser(userId, { is_active: active }),
    onSuccess: onUpdated,
  })
  const reset = useMutation({
    mutationFn: () => resetPassword(userId),
    onSuccess: (result) => {
      setTemporary(result)
      void queryClient.invalidateQueries({ queryKey: key })
    },
  })
  const revoke = useMutation({
    mutationFn: () => revokeUserSessions(userId),
    onSuccess: (n) => {
      setMessage(t.revokedSessions(n))
      void queryClient.invalidateQueries({ queryKey: key })
    },
  })
  const unlock = useMutation({ mutationFn: () => unlockUser(userId), onSuccess: onUpdated })
  const actionError = toggleActive.error ?? reset.error ?? revoke.error ?? unlock.error

  if (query.isPending) return <p>{ru.common.loading}</p>
  if (query.isError) return <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />
  const user = query.data
  const isSelf = user.id === me.id
  const protectedTarget = user.role === 'superadmin' && me.role !== 'superadmin'
  const editable = !protectedTarget
  const locked = user.locked_until !== null && Date.parse(user.locked_until) > now

  return (
    <section aria-labelledby="user-title" className="space-y-6">
      <Link to="/admin/users" className="text-sm text-blue-700 underline">
        ← {ru.users.title}
      </Link>
      <PageTitle id="user-title">{user.display_name}</PageTitle>
      {protectedTarget && <Notice tone="warning">{t.superadminProtected}</Notice>}
      {isSelf && <Notice>{t.self}</Notice>}
      {temporary && (
        <TemporaryPasswordNotice email={user.email} password={temporary.temporary_password} onDone={() => setTemporary(null)} />
      )}
      {message && <Notice tone="success">{message}</Notice>}
      {actionError && <ErrorNotice error={actionError} />}

      <dl className="grid max-w-xl grid-cols-[max-content_1fr] gap-x-6 gap-y-1 rounded-lg border border-slate-300 bg-white shadow-sm p-4 text-sm">
        <dt className="text-slate-600">{ru.users.columns.email}</dt>
        <dd>{user.email}</dd>
        <dt className="text-slate-600">{t.status}</dt>
        <dd>{user.is_active ? ru.users.active : ru.users.blocked}</dd>
        <dt className="text-slate-600">{t.mustChange}</dt>
        <dd>{user.must_change_password ? ru.common.yes : ru.common.no}</dd>
        <dt className="text-slate-600">{t.failedLogins}</dt>
        <dd>{user.failed_login_count}</dd>
        {locked && (
          <>
            <dt className="text-slate-600">{t.lockedUntil}</dt>
            <dd>{formatDateTime(user.locked_until)}</dd>
          </>
        )}
        <dt className="text-slate-600">{t.lastLogin}</dt>
        <dd>{formatDateTime(user.last_login_at, ru.common.never)}</dd>
        <dt className="text-slate-600">{t.created}</dt>
        <dd>{formatDateTime(user.created_at)}</dd>
        <dt className="text-slate-600">{t.activeSessions}</dt>
        <dd>{user.active_sessions}</dd>
      </dl>

      {editable && (
        <section aria-labelledby="profile-title" className="max-w-xl space-y-3">
          <h2 id="profile-title" className="text-lg font-semibold">
            {t.profile}
          </h2>
          <ProfileForm
            key={`${user.id}-${user.role}-${user.display_name}`}
            user={user}
            canEditRole={!isSelf}
            roles={me.capabilities.assignable_roles}
          />
        </section>
      )}

      {editable && (
        <section aria-labelledby="actions-title" className="space-y-3">
          <h2 id="actions-title" className="text-lg font-semibold">
            {t.actions}
          </h2>
          <div className="flex flex-wrap gap-3">
            {!isSelf &&
              (user.is_active ? (
                <Button
                  variant="danger"
                  disabled={toggleActive.isPending}
                  onClick={() => window.confirm(t.confirmBlock) && toggleActive.mutate(false)}
                >
                  {t.block}
                </Button>
              ) : (
                <Button disabled={toggleActive.isPending} onClick={() => toggleActive.mutate(true)}>
                  {t.unblock}
                </Button>
              ))}
            {!isSelf && (
              <Button
                variant="danger"
                disabled={reset.isPending}
                onClick={() => window.confirm(t.confirmReset) && reset.mutate()}
              >
                {t.resetPassword}
              </Button>
            )}
            {!isSelf && (
              <Button disabled={revoke.isPending || user.active_sessions === 0} onClick={() => revoke.mutate()}>
                {t.revokeSessions}
              </Button>
            )}
            {(locked || user.failed_login_count > 0) && (
              <Button disabled={unlock.isPending} onClick={() => unlock.mutate()}>
                {t.unlock}
              </Button>
            )}
          </div>
        </section>
      )}

      <ModelAccess userId={user.id} editable={editable} />
    </section>
  )
}
