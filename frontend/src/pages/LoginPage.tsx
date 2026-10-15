import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useForm } from 'react-hook-form'
import { Navigate, useNavigate, useSearchParams } from 'react-router'
import { z } from 'zod'

import { login, ME_QUERY_KEY } from '../api/auth'
import { fetchReadiness } from '../api/health'
import { safeNextPath, useMe } from '../app/session'
import { ErrorNotice } from '../components/ErrorNotice'
import { Button, Field, inputClass, Notice } from '../components/ui'
import { ru } from '../i18n/ru'

const schema = z.object({
  email: z.string().trim().min(1, ru.login.emailRequired),
  password: z.string().min(1, ru.login.passwordRequired),
})
type FormValues = z.infer<typeof schema>

export function LoginPage() {
  const me = useMe()
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const next = safeNextPath(params.get('next'))
  // Без администратора войти некому — сообщаем об этом сразу на странице входа.
  const readiness = useQuery({ queryKey: ['health', 'ready'], queryFn: ({ signal }) => fetchReadiness(signal) })
  const bootstrapRequired = readiness.data?.checks.accounts === 'bootstrap_required'
  const form = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: { email: '', password: '' } })

  const mutation = useMutation({
    mutationFn: (values: FormValues) => login(values.email, values.password),
    onSuccess: (user) => {
      // Прочие данные могли относиться к предыдущему пользователю — сбрасываем кеш.
      queryClient.clear()
      queryClient.setQueryData(ME_QUERY_KEY, user)
      void navigate(user.must_change_password ? '/change-password' : next, { replace: true })
    },
    onError: () => form.resetField('password'),
  })

  if (me.data) return <Navigate to={next} replace />

  const { errors } = form.formState
  return (
    <main id="main" className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
      <section aria-labelledby="login-title" className="w-full max-w-sm space-y-4 rounded-lg border border-slate-200 bg-white p-6">
        <div>
          <p className="text-sm text-slate-600">{ru.appName}</p>
          <h1 id="login-title" className="text-xl font-semibold">
            {ru.login.title}
          </h1>
        </div>
        {bootstrapRequired && <Notice tone="warning">{ru.login.bootstrapRequired}</Notice>}
        {mutation.isError && <ErrorNotice error={mutation.error} />}
        <form noValidate className="space-y-3" onSubmit={form.handleSubmit((values) => mutation.mutate(values))}>
          <Field id="email" label={ru.login.email} error={errors.email?.message}>
            <input
              id="email"
              type="email"
              autoComplete="username"
              className={inputClass}
              aria-invalid={Boolean(errors.email)}
              {...form.register('email')}
            />
          </Field>
          <Field id="password" label={ru.login.password} error={errors.password?.message}>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              className={inputClass}
              aria-invalid={Boolean(errors.password)}
              {...form.register('password')}
            />
          </Field>
          <Button type="submit" variant="primary" className="w-full" disabled={mutation.isPending}>
            {mutation.isPending ? ru.login.submitting : ru.login.submit}
          </Button>
        </form>
        <p className="text-xs text-slate-600">{ru.disclaimer}</p>
      </section>
    </main>
  )
}
