import { zodResolver } from '@hookform/resolvers/zod'
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link } from 'react-router'
import { z } from 'zod'

import { roleSchema, type Role } from '../../api/auth'
import { createUser, fetchUsers, type TemporaryPassword, type UserFilters, USERS_KEY } from '../../api/users'
import { useCurrentUser } from '../../app/session'
import { ErrorNotice } from '../../components/ErrorNotice'
import { TemporaryPasswordNotice } from '../../components/TemporaryPasswordNotice'
import { Button, Field, inputClass, PageTitle } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatDateTime } from '../../lib/format'

const createSchema = z.object({
  email: z.string().trim().max(320).pipe(z.email(ru.users.form.emailInvalid)),
  display_name: z.string().trim().min(1, ru.users.form.nameRequired).max(200),
  role: roleSchema,
})
type CreateValues = z.infer<typeof createSchema>

function CreateUserForm({ roles, onCreated }: { roles: Role[]; onCreated: (r: TemporaryPassword) => void }) {
  const queryClient = useQueryClient()
  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { email: '', display_name: '', role: roles.includes('analyst') ? 'analyst' : (roles[0] ?? 'viewer') },
  })
  const mutation = useMutation({
    mutationFn: createUser,
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: USERS_KEY })
      form.reset()
      onCreated(result)
    },
  })
  const { errors } = form.formState
  return (
    <form
      noValidate
      aria-labelledby="create-user-title"
      className="space-y-3 rounded-lg border border-slate-200 bg-white p-4"
      onSubmit={form.handleSubmit((v) => mutation.mutate(v))}
    >
      <h2 id="create-user-title" className="font-semibold">
        {ru.users.form.title}
      </h2>
      {mutation.isError && <ErrorNotice error={mutation.error} />}
      <div className="grid gap-3 sm:grid-cols-3">
        <Field id="new-email" label={ru.users.form.email} error={errors.email?.message}>
          <input id="new-email" type="email" autoComplete="off" className={inputClass} {...form.register('email')} />
        </Field>
        <Field id="new-name" label={ru.users.form.name} error={errors.display_name?.message}>
          <input id="new-name" autoComplete="off" className={inputClass} {...form.register('display_name')} />
        </Field>
        <Field id="new-role" label={ru.users.form.role}>
          <select id="new-role" className={inputClass} {...form.register('role')}>
            {/* Только роли, которые backend разрешает назначать этому пользователю */}
            {roles.map((role) => (
              <option key={role} value={role}>
                {ru.roles[role]}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <Button type="submit" variant="primary" disabled={mutation.isPending}>
        {ru.users.form.submit}
      </Button>
    </form>
  )
}

export function UsersPage() {
  const me = useCurrentUser()
  const [filters, setFilters] = useState<UserFilters>({ q: '', role: '', isActive: '' })
  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<TemporaryPassword | null>(null)

  const query = useInfiniteQuery({
    queryKey: [...USERS_KEY, filters],
    queryFn: ({ pageParam, signal }) => fetchUsers({ ...filters, cursor: pageParam }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  })
  const users = query.data?.pages.flatMap((p) => p.items) ?? []

  return (
    <section aria-labelledby="users-title" className="space-y-4">
      <PageTitle
        id="users-title"
        actions={
          <Button variant="primary" onClick={() => setCreating((v) => !v)} aria-expanded={creating}>
            {ru.users.create}
          </Button>
        }
      >
        {ru.users.title}
      </PageTitle>

      {created && (
        <TemporaryPasswordNotice
          email={created.user.email}
          password={created.temporary_password}
          onDone={() => setCreated(null)}
        />
      )}
      {creating && (
        <CreateUserForm
          roles={me.capabilities.assignable_roles}
          onCreated={(result) => {
            setCreating(false)
            setCreated(result)
          }}
        />
      )}

      <search className="flex flex-wrap gap-3">
        <input
          aria-label={ru.users.search}
          placeholder={ru.users.search}
          className={`${inputClass} max-w-xs`}
          value={filters.q}
          onChange={(e) => setFilters((f) => ({ ...f, q: e.target.value }))}
        />
        <select
          aria-label={ru.users.columns.role}
          className={`${inputClass} max-w-48`}
          value={filters.role}
          onChange={(e) => setFilters((f) => ({ ...f, role: e.target.value as Role | '' }))}
        >
          <option value="">{ru.users.allRoles}</option>
          {roleSchema.options.map((role) => (
            <option key={role} value={role}>
              {ru.roles[role]}
            </option>
          ))}
        </select>
        <select
          aria-label={ru.users.columns.status}
          className={`${inputClass} max-w-48`}
          value={filters.isActive}
          onChange={(e) => setFilters((f) => ({ ...f, isActive: e.target.value as UserFilters['isActive'] }))}
        >
          <option value="">{ru.users.allStatuses}</option>
          <option value="true">{ru.users.active}</option>
          <option value="false">{ru.users.blocked}</option>
        </select>
      </search>

      {query.isPending && <p>{ru.common.loading}</p>}
      {query.isError && <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />}
      {query.isSuccess && users.length === 0 && <p className="text-slate-600">{ru.users.empty}</p>}
      {users.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead className="bg-slate-50 text-slate-600">
              <tr>
                <th className="px-3 py-2 font-medium">{ru.users.columns.name}</th>
                <th className="px-3 py-2 font-medium">{ru.users.columns.email}</th>
                <th className="px-3 py-2 font-medium">{ru.users.columns.role}</th>
                <th className="px-3 py-2 font-medium">{ru.users.columns.status}</th>
                <th className="px-3 py-2 font-medium">{ru.users.columns.lastLogin}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200">
              {users.map((u) => (
                <tr key={u.id}>
                  <td className="px-3 py-2">
                    <Link to={`/admin/users/${u.id}`} className="text-blue-700 underline">
                      {u.display_name}
                    </Link>
                  </td>
                  <td className="px-3 py-2">{u.email}</td>
                  <td className="px-3 py-2">{ru.roles[u.role]}</td>
                  <td className="px-3 py-2">{u.is_active ? ru.users.active : ru.users.blocked}</td>
                  <td className="px-3 py-2">{formatDateTime(u.last_login_at, ru.common.never)}</td>
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
