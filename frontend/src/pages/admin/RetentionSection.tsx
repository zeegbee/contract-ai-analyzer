import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { fetchRetention, RETENTION_KEY, updateRetention } from '../../api/settings'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Button, inputClass, Notice } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatDateTime } from '../../lib/format'

const t = ru.settings.retention

/**
 * Автоматическое удаление документов по сроку. По умолчанию выключено; меняет только Superadmin.
 * Включение и сокращение срока — с предпросмотром числа документов и явным подтверждением
 * (сервер всё равно требует `confirm`, интерфейс лишь показывает последствия заранее).
 */
export function RetentionSection() {
  const ids = { enabled: useId(), days: useId(), confirm: useId() }
  const queryClient = useQueryClient()
  const current = useQuery({ queryKey: RETENTION_KEY, queryFn: ({ signal }) => fetchRetention(null, signal) })
  const [enabled, setEnabled] = useState<boolean | null>(null)
  // null — поле не меняли (показываем текущий срок или 365), '' — пользователь очистил поле.
  const [days, setDays] = useState<string | null>(null)
  const [confirmed, setConfirmed] = useState(false)

  const data = current.data
  const isEnabled = enabled ?? data?.retention_days != null
  const daysValue = days ?? (data?.retention_days ? String(data.retention_days) : '365')
  const parsed = Number(daysValue)
  const validDays = data !== undefined && daysValue !== '' && Number.isInteger(parsed) && parsed >= data.min_days && parsed <= data.max_days
  const target = isEnabled ? (validDays ? parsed : null) : null
  const stricter = target !== null && (data?.retention_days == null || target < data.retention_days)

  const preview = useQuery({
    queryKey: [...RETENTION_KEY, 'preview', target],
    queryFn: ({ signal }) => fetchRetention(target, signal),
    enabled: Boolean(data?.can_manage && stricter && target),
  })
  const save = useMutation({
    mutationFn: () => updateRetention({ retention_days: target, confirm: stricter && confirmed }),
    onSuccess: (result) => {
      queryClient.setQueryData(RETENTION_KEY, result)
      setEnabled(null)
      setDays(null)
      setConfirmed(false)
    },
  })

  const unchanged = target === (data?.retention_days ?? null)
  const invalid = isEnabled && !validDays

  return (
    <section aria-labelledby="retention-title" className="space-y-3 rounded-lg border border-slate-200 bg-white p-4 text-sm">
      <h2 id="retention-title" className="text-lg font-semibold">
        {t.title}
      </h2>
      <p className="text-slate-700">{t.description}</p>
      {current.isPending && <p>{ru.common.loading}</p>}
      {current.isError && <ErrorNotice error={current.error} onRetry={() => void current.refetch()} />}
      {data && (
        <>
          <p aria-live="polite" className="font-medium">
            {data.retention_days == null ? t.off : t.on(data.retention_days)}
            {data.eligible_now != null && data.eligible_now > 0 && ` ${t.pending(data.eligible_now)}`}
          </p>
          {data.updated_at && (
            <p className="text-slate-600">
              {t.updated}: {formatDateTime(data.updated_at)}
            </p>
          )}

          {!data.can_manage ? (
            <p className="text-slate-600">{t.superadminOnly}</p>
          ) : (
            <form
              className="space-y-3"
              onSubmit={(e) => {
                e.preventDefault()
                save.mutate()
              }}
            >
              <label htmlFor={ids.enabled} className="flex items-center gap-2">
                <input
                  id={ids.enabled}
                  type="checkbox"
                  checked={isEnabled}
                  onChange={(e) => {
                    setEnabled(e.target.checked)
                    setConfirmed(false)
                  }}
                />
                {t.enable}
              </label>
              {isEnabled && (
                <div className="space-y-1">
                  <label htmlFor={ids.days} className="block font-medium">
                    {t.days}
                  </label>
                  <input
                    id={ids.days}
                    type="number"
                    inputMode="numeric"
                    min={data.min_days}
                    max={data.max_days}
                    className={`${inputClass} max-w-40`}
                    value={daysValue}
                    aria-invalid={invalid}
                    aria-describedby={`${ids.days}-hint`}
                    onChange={(e) => {
                      setDays(e.target.value)
                      setConfirmed(false)
                    }}
                  />
                  <p id={`${ids.days}-hint`} className="text-slate-600">
                    {invalid ? t.range(data.min_days, data.max_days) : t.hint}
                  </p>
                </div>
              )}
              {stricter && !invalid && (
                <Notice tone="warning">
                  <span className="block">
                    {preview.data?.preview_count != null ? t.preview(preview.data.preview_count) : ru.common.loading}
                  </span>
                  <span className="block">{t.backups}</span>
                  <label htmlFor={ids.confirm} className="mt-2 flex items-start gap-2">
                    <input
                      id={ids.confirm}
                      type="checkbox"
                      checked={confirmed}
                      onChange={(e) => setConfirmed(e.target.checked)}
                    />
                    <span>{t.confirm}</span>
                  </label>
                </Notice>
              )}
              {preview.isError && <ErrorNotice error={preview.error} />}
              {save.isError && <ErrorNotice error={save.error} />}
              {save.isSuccess && <Notice tone="success">{t.saved}</Notice>}
              <Button
                type="submit"
                variant={stricter ? 'danger' : 'primary'}
                disabled={save.isPending || unchanged || invalid || (stricter && !confirmed)}
              >
                {t.save}
              </Button>
            </form>
          )}
        </>
      )}
    </section>
  )
}
