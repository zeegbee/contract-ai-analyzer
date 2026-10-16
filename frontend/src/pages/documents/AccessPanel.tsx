import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'

import { DOCUMENTS_KEY, type DocumentPermission, fetchAccess, grantAccess, revokeAccess } from '../../api/documents'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Button, Field, inputClass } from '../../components/ui'
import { ru } from '../../i18n/ru'

const t = ru.documents

/** Выдача доступа владельцем. Права проверяет backend; здесь — только форма. */
export function AccessPanel({ documentId }: { documentId: string }) {
  const queryClient = useQueryClient()
  const key = [...DOCUMENTS_KEY, documentId, 'access']
  const access = useQuery({ queryKey: key, queryFn: ({ signal }) => fetchAccess(documentId, signal) })
  const [email, setEmail] = useState('')
  const [permission, setPermission] = useState<DocumentPermission>('view')

  const grant = useMutation({
    mutationFn: () => grantAccess(documentId, email.trim(), permission),
    onSuccess: (items) => {
      queryClient.setQueryData(key, items)
      setEmail('')
    },
  })
  const revoke = useMutation({
    mutationFn: (userId: string) => revokeAccess(documentId, userId),
    onSuccess: (items) => queryClient.setQueryData(key, items),
  })

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (email.trim()) grant.mutate()
  }

  return (
    <section aria-labelledby="access-title" className="space-y-3 rounded-lg border border-slate-200 bg-white p-4">
      <h2 id="access-title" className="font-semibold">
        {t.shareTitle}
      </h2>
      <p className="text-xs text-slate-600">{t.shareHint}</p>
      {access.isError && <ErrorNotice error={access.error} onRetry={() => void access.refetch()} />}
      {access.data && access.data.length === 0 && <p className="text-sm text-slate-600">{t.shareEmpty}</p>}
      {access.data && access.data.length > 0 && (
        <ul className="divide-y divide-slate-100 text-sm">
          {access.data.map((entry) => (
            <li key={entry.user_id} className="flex flex-wrap items-center justify-between gap-2 py-2">
              <span>
                {entry.display_name} <span className="text-slate-600">({entry.email})</span> —{' '}
                {t.permissions[entry.permission]}
              </span>
              <Button variant="danger" disabled={revoke.isPending} onClick={() => revoke.mutate(entry.user_id)}>
                {t.shareRevoke}
              </Button>
            </li>
          ))}
        </ul>
      )}
      {revoke.isError && <ErrorNotice error={revoke.error} />}
      <form noValidate onSubmit={submit} className="grid gap-3 sm:grid-cols-[1fr_auto_auto] sm:items-end">
        <Field id="share-email" label={t.shareEmail}>
          <input
            id="share-email"
            type="email"
            autoComplete="off"
            className={inputClass}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <Field id="share-permission" label={t.sharePermission}>
          <select
            id="share-permission"
            className={inputClass}
            value={permission}
            onChange={(e) => setPermission(e.target.value as DocumentPermission)}
          >
            <option value="view">{t.permissions.view}</option>
            <option value="analyze">{t.permissions.analyze}</option>
          </select>
        </Field>
        <Button type="submit" variant="primary" disabled={grant.isPending || !email.trim()}>
          {t.shareSubmit}
        </Button>
      </form>
      {grant.isError && <ErrorNotice error={grant.error} />}
    </section>
  )
}
