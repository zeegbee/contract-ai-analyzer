import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useState, type ReactNode } from 'react'

import {
  BACKUP_KEY,
  fetchBackup,
  requestBackupRun,
  updateBackup,
  type BackupConfig,
  type BackupField,
  type BackupRun,
  type BackupState,
} from '../../api/backup'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Button, inputClass, Notice } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatBytes, formatDateTime } from '../../lib/format'

const t = ru.settings.backup
// Планировщик опрашивает настройки раз в 5 минут; дольше 15 минут тишины — он не установлен или не работает.
const SCHEDULER_STALE_MS = 15 * 60 * 1000
const PRESETS = [
  { label: t.presetDaily, value: '0 3 * * *' },
  { label: t.presetSixHours, value: '0 */6 * * *' },
  { label: t.presetWeekly, value: '0 3 * * 0' },
]
const TARGETS = ['none', 's3', 'local'] as const
const PRUNES = ['lifecycle', 'script'] as const

/**
 * Резервное копирование в «Настройках». Сам бэкап выполняет скрипт на сервере; здесь — параметры,
 * которые можно переопределить в UI (только Superadmin), состояние планировщика и журнал запусков.
 */
export function BackupSection() {
  const query = useQuery({ queryKey: BACKUP_KEY, queryFn: ({ signal }) => fetchBackup(signal), refetchInterval: 60_000 })
  const data = query.data
  const [saved, setSaved] = useState(false)

  return (
    <section aria-labelledby="backup-title" className="space-y-4 rounded-lg border border-slate-200 bg-white p-4 text-sm">
      <h2 id="backup-title" className="text-lg font-semibold">
        {t.title}
      </h2>
      <p className="text-slate-700">{t.description}</p>
      {query.isPending && <p>{ru.common.loading}</p>}
      {query.isError && <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />}
      {data && (
        <>
          <BackupStatus state={data} fetchedAt={query.dataUpdatedAt} />
          {saved && <Notice tone="success">{t.saved}</Notice>}
          <BackupForm
            key={JSON.stringify([data.config, data.sources])}
            state={data}
            onChange={() => setSaved(false)}
            onSaved={() => setSaved(true)}
          />
          <BackupRuns runs={data.runs} />
        </>
      )}
    </section>
  )
}

function BackupStatus({ state, fetchedAt }: { state: BackupState; fetchedAt: number }) {
  const queryClient = useQueryClient()
  const run = useMutation({
    mutationFn: requestBackupRun,
    onSuccess: (result) => queryClient.setQueryData(BACKUP_KEY, result),
  })
  const seen = state.scheduler_seen_at ? Date.parse(state.scheduler_seen_at) : null
  // Сравнение со временем получения данных (а не Date.now при рендере).
  const schedulerStale = seen === null || fetchedAt - seen > SCHEDULER_STALE_MS

  return (
    <div className="space-y-2" aria-live="polite">
      <h3 className="font-semibold">{t.status}</h3>
      {state.problems.length > 0 && (
        <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-3 text-red-900">
          <p className="font-medium">{t.problems}</p>
          <ul className="list-disc pl-5">
            {state.problems.map((p) => (
              <li key={p.code}>{p.message}</li>
            ))}
          </ul>
        </div>
      )}
      {schedulerStale && <Notice tone="warning">{t.schedulerMissing}</Notice>}
      {state.overdue && <Notice tone="warning">{t.overdue}</Notice>}
      <dl className="grid gap-x-6 gap-y-1 sm:grid-cols-[max-content_1fr]">
        <dt className="text-slate-600">{t.lastSuccess}</dt>
        <dd>{state.last_success_at ? formatDateTime(state.last_success_at) : t.never}</dd>
        <dt className="text-slate-600">{t.nextRun}</dt>
        <dd>
          {state.next_run_at ? `${formatDateTime(state.next_run_at)} (${state.config.timezone ?? ''})` : t.scheduleOff}
        </dd>
        <dt className="text-slate-600">{t.schedulerSeen}</dt>
        <dd>{formatDateTime(state.scheduler_seen_at, t.never)}</dd>
      </dl>
      {state.requested_at && <p>{t.requested(formatDateTime(state.requested_at))}</p>}
      {run.isError && <ErrorNotice error={run.error} />}
      {state.can_manage && (
        <Button disabled={run.isPending || state.requested_at !== null} onClick={() => run.mutate()}>
          {t.runNow}
        </Button>
      )}
    </div>
  )
}

function BackupForm({ state, onChange, onSaved }: { state: BackupState; onChange: () => void; onSaved: () => void }) {
  const ids = {
    enabled: useId(),
    schedule: useId(),
    preset: useId(),
    timezone: useId(),
    keepLocal: useId(),
    daily: useId(),
    weekly: useId(),
    monthly: useId(),
  }
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<BackupConfig>(state.config)
  const [reset, setReset] = useState<BackupField[]>([])
  const save = useMutation({
    mutationFn: () => {
      const changes: Partial<BackupConfig> = {}
      for (const key of Object.keys(draft) as BackupField[]) {
        if (draft[key] !== state.config[key] && !reset.includes(key)) Object.assign(changes, { [key]: draft[key] })
      }
      return updateBackup({ ...changes, reset })
    },
    onSuccess: (result) => {
      queryClient.setQueryData(BACKUP_KEY, result)
      onSaved()
    },
  })
  const editable = state.can_manage
  const set = <K extends BackupField>(key: K, value: BackupConfig[K]) => {
    setDraft((d) => ({ ...d, [key]: value }))
    setReset((r) => r.filter((k) => k !== key))
    onChange()
  }
  const number = (value: string) => (value === '' ? null : Number(value))
  const dirty = reset.length > 0 || (Object.keys(draft) as BackupField[]).some((k) => draft[k] !== state.config[k])

  const source = (key: BackupField): ReactNode => {
    if (reset.includes(key)) return <span className="text-xs text-slate-600">({t.sourceEnv})</span>
    const value = state.sources[key]
    return (
      <span className="text-xs text-slate-600">
        ({value === 'ui' ? t.sourceUi : value === 'default' ? t.sourceDefault : t.sourceEnv})
        {value === 'ui' && editable && (
          <button
            type="button"
            className="ml-2 text-blue-800 underline"
            onClick={() => {
              setReset((r) => [...r, key])
              setDraft((d) => ({ ...d, [key]: state.env[key] }))
            }}
          >
            {t.reset}
          </button>
        )}
      </span>
    )
  }
  const targetConfigured = (target: string) =>
    target === 'none' || (target === 's3' ? state.server.s3.configured : state.server.local.configured)

  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault()
        save.mutate()
      }}
    >
      <h3 className="font-semibold">{t.settings}</h3>
      {!editable && <p className="text-slate-600">{t.readOnly}</p>}
      <fieldset disabled={!editable} className="space-y-4">
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor={ids.enabled} className="flex items-center gap-2">
              <input
                id={ids.enabled}
                type="checkbox"
                checked={draft.schedule_enabled ?? false}
                onChange={(e) => set('schedule_enabled', e.target.checked)}
              />
              {t.scheduleEnabled}
            </label>
            {source('schedule_enabled')}
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <div>
                <label htmlFor={ids.schedule} className="font-medium">
                  {t.schedule}
                </label>{' '}
                {source('schedule')}
              </div>
              <input
                id={ids.schedule}
                className={`${inputClass} font-mono`}
                value={draft.schedule ?? ''}
                maxLength={100}
                onChange={(e) => set('schedule', e.target.value)}
              />
              <label htmlFor={ids.preset} className="sr-only">
                {t.presets}
              </label>
              <select
                id={ids.preset}
                className={inputClass}
                value=""
                onChange={(e) => e.target.value && set('schedule', e.target.value)}
              >
                <option value="">{t.presets}…</option>
                {PRESETS.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label} ({p.value})
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <div>
                <label htmlFor={ids.timezone} className="font-medium">
                  {t.timezone}
                </label>{' '}
                {source('timezone')}
              </div>
              <input
                id={ids.timezone}
                className={inputClass}
                value={draft.timezone ?? ''}
                maxLength={64}
                placeholder="Europe/Moscow"
                onChange={(e) => set('timezone', e.target.value)}
              />
            </div>
          </div>
        </div>

        <fieldset className="space-y-1">
          <legend className="font-medium">
            {t.target} {source('target')}
          </legend>
          {TARGETS.map((target) => (
            <label key={target} className="flex items-start gap-2">
              <input
                type="radio"
                name="backup-target"
                value={target}
                checked={draft.target === target}
                disabled={!targetConfigured(target)}
                onChange={() => set('target', target)}
              />
              <span>
                {t.targets[target]}
                {target === 's3' && state.server.s3.configured && (
                  <span className="text-slate-600">
                    {' '}
                    — {state.server.s3.endpoint} / {state.server.s3.bucket}
                    {state.server.s3.prefix ? `/${state.server.s3.prefix}` : ''}
                  </span>
                )}
                {target === 'local' && state.server.local.configured && (
                  <span className="text-slate-600"> — {state.server.local.dir}</span>
                )}
                {!targetConfigured(target) && <span className="text-slate-600"> — {t.notConfigured}</span>}
              </span>
            </label>
          ))}
        </fieldset>

        <p>
          <span className="font-medium">{t.encryption}:</span>{' '}
          {state.server.encryption.configured && state.server.encryption.recipient_hint
            ? t.encryptionOn(state.server.encryption.recipient_hint)
            : t.encryptionOff}
        </p>

        <NumberField id={ids.keepLocal} label={t.keepLocal} extra={source('keep_local')} min={1}
          value={draft.keep_local} onChange={(v) => set('keep_local', number(v))} />

        {draft.target !== 'none' && (
          <fieldset className="space-y-2">
            <legend className="font-medium">
              {t.prune} {source('remote_prune')}
            </legend>
            {PRUNES.map((mode) => (
              <label key={mode} className="flex items-start gap-2">
                <input
                  type="radio"
                  name="backup-prune"
                  value={mode}
                  checked={draft.remote_prune === mode}
                  onChange={() => set('remote_prune', mode)}
                />
                <span>{t.prunes[mode]}</span>
              </label>
            ))}
            {draft.remote_prune === 'script' && (
              <div className="grid gap-3 sm:grid-cols-3">
                <NumberField id={ids.daily} label={t.keepDaily} extra={source('keep_remote_daily')} min={0}
                  value={draft.keep_remote_daily} onChange={(v) => set('keep_remote_daily', number(v))} />
                <NumberField id={ids.weekly} label={t.keepWeekly} extra={source('keep_remote_weekly')} min={0}
                  value={draft.keep_remote_weekly} onChange={(v) => set('keep_remote_weekly', number(v))} />
                <NumberField id={ids.monthly} label={t.keepMonthly} extra={source('keep_remote_monthly')} min={0}
                  value={draft.keep_remote_monthly} onChange={(v) => set('keep_remote_monthly', number(v))} />
              </div>
            )}
          </fieldset>
        )}
      </fieldset>
      {save.isError && <ErrorNotice error={save.error} />}
      {editable && (
        <Button type="submit" variant="primary" disabled={!dirty || save.isPending}>
          {t.save}
        </Button>
      )}
    </form>
  )
}

function NumberField(props: {
  id: string
  label: string
  extra: ReactNode
  min: number
  value: number | null
  onChange: (value: string) => void
}) {
  return (
    <div className="space-y-1">
      <div>
        <label htmlFor={props.id} className="font-medium">
          {props.label}
        </label>{' '}
        {props.extra}
      </div>
      <input
        id={props.id}
        type="number"
        inputMode="numeric"
        min={props.min}
        max={1000}
        className={`${inputClass} max-w-32`}
        value={props.value ?? ''}
        onChange={(e) => props.onChange(e.target.value)}
      />
    </div>
  )
}

function BackupRuns({ runs }: { runs: BackupRun[] }) {
  const offsite = (run: BackupRun) =>
    run.target === 'none' ? t.offsite.none : run.verified ? t.offsite.verified : run.uploaded ? t.offsite.uploaded : t.offsite.failed
  return (
    <div className="space-y-2">
      <h3 className="font-semibold">{t.runs}</h3>
      {runs.length === 0 ? (
        <p className="text-slate-600">{t.noRuns}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[48rem] text-left">
            <thead className="text-slate-600">
              <tr>
                <th className="py-1 pr-3 font-medium">{t.columns.started}</th>
                <th className="py-1 pr-3 font-medium">{t.columns.status}</th>
                <th className="py-1 pr-3 font-medium">{t.columns.trigger}</th>
                <th className="py-1 pr-3 font-medium">{t.columns.target}</th>
                <th className="py-1 pr-3 font-medium">{t.columns.archive}</th>
                <th className="py-1 pr-3 font-medium">{t.columns.size}</th>
                <th className="py-1 pr-3 font-medium">{t.columns.offsite}</th>
                <th className="py-1 font-medium">{t.columns.error}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {runs.map((run) => (
                <tr key={run.id}>
                  <td className="py-1 pr-3">{formatDateTime(run.started_at)}</td>
                  <td className={`py-1 pr-3 ${run.status === 'success' ? 'text-green-800' : run.status === 'running' ? '' : 'text-red-800'}`}>
                    {t.runStatus[run.status] ?? run.status}
                  </td>
                  <td className="py-1 pr-3">{t.triggers[run.trigger] ?? run.trigger}</td>
                  <td className="py-1 pr-3">{t.targets[run.target] ?? run.target}</td>
                  <td className="py-1 pr-3 font-mono text-xs break-all">
                    {run.name ?? '—'}
                    {run.encrypted && <span className="ml-1 font-sans text-slate-600">({t.encrypted})</span>}
                  </td>
                  <td className="py-1 pr-3">{run.size_bytes !== null ? formatBytes(run.size_bytes) : '—'}</td>
                  <td className="py-1 pr-3">{run.status === 'running' ? '—' : offsite(run)}</td>
                  <td className="py-1">{run.error_code ? (t.errors[run.error_code] ?? run.error_code) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
