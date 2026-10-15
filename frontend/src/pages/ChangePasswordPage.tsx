import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { useNavigate } from 'react-router'
import { z } from 'zod'

import { changePassword, ME_QUERY_KEY } from '../api/auth'
import { useCurrentUser } from '../app/session'
import { ErrorNotice } from '../components/ErrorNotice'
import { Button, Field, inputClass, Notice, PageTitle } from '../components/ui'
import { ru } from '../i18n/ru'

const schema = z
  .object({
    current: z.string().min(1, ru.changePassword.required),
    next: z.string().min(12, ru.changePassword.tooShort).max(128, ru.changePassword.tooLong),
    confirm: z.string().min(1, ru.changePassword.required),
  })
  .refine((v) => v.next === v.confirm, { path: ['confirm'], message: ru.changePassword.mismatch })
type FormValues = z.infer<typeof schema>

export function ChangePasswordPage() {
  const me = useCurrentUser()
  const forced = me.must_change_password
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [done, setDone] = useState(false)
  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { current: '', next: '', confirm: '' },
  })

  const mutation = useMutation({
    mutationFn: (v: FormValues) => changePassword(v.current, v.next),
    onSuccess: (user) => {
      form.reset()
      queryClient.setQueryData(ME_QUERY_KEY, user)
      void queryClient.invalidateQueries({ queryKey: ['auth', 'sessions'] })
      if (forced) void navigate('/', { replace: true })
      else setDone(true)
    },
    onError: () => form.reset({ current: '', next: '', confirm: '' }),
  })

  const { errors } = form.formState
  return (
    <section aria-labelledby="change-password-title" className="max-w-md space-y-4">
      <PageTitle id="change-password-title">{ru.changePassword.title}</PageTitle>
      {forced && <Notice tone="warning">{ru.changePassword.forcedNotice}</Notice>}
      {done && <Notice tone="success">{ru.changePassword.success}</Notice>}
      {mutation.isError && <ErrorNotice error={mutation.error} />}
      <form noValidate className="space-y-3" onSubmit={form.handleSubmit((v) => mutation.mutate(v))}>
        <Field id="current-password" label={ru.changePassword.current} error={errors.current?.message}>
          <input
            id="current-password"
            type="password"
            autoComplete="current-password"
            className={inputClass}
            {...form.register('current')}
          />
        </Field>
        <Field
          id="new-password"
          label={ru.changePassword.next}
          error={errors.next?.message}
          hint={ru.changePassword.rules}
        >
          <input
            id="new-password"
            type="password"
            autoComplete="new-password"
            className={inputClass}
            {...form.register('next')}
          />
        </Field>
        <Field id="confirm-password" label={ru.changePassword.confirm} error={errors.confirm?.message}>
          <input
            id="confirm-password"
            type="password"
            autoComplete="new-password"
            className={inputClass}
            {...form.register('confirm')}
          />
        </Field>
        <Button type="submit" variant="primary" disabled={mutation.isPending}>
          {ru.changePassword.submit}
        </Button>
      </form>
    </section>
  )
}
