import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId } from 'react'

import { AVAILABLE_MODELS_KEY, type AvailableModel, fetchAvailableModels, recordCloudConsent } from '../api/catalog'
import { ru } from '../i18n/ru'
import { pricingText } from '../lib/pricing'
import { ErrorNotice } from './ErrorNotice'
import { inputClass, Notice } from './ui'

const t = ru.llm.available

export interface ModelChoice {
  modelId: string
  /** Для облачной модели — пользователь подтвердил передачу текста (сервер проверяет это ещё раз). */
  cloudConsent: boolean
}

interface Props {
  value: ModelChoice | null
  onChange: (value: ModelChoice | null) => void
}

/**
 * Выбор модели для анализа. Список приходит с сервера уже отфильтрованным: включённые модели
 * включённых провайдеров, разрешённые роли/пользователю, облачные — только если облако разрешено.
 * Это удобство UI; права и согласие повторно проверяются на сервере при запуске анализа.
 */
export function ModelSelect({ value, onChange }: Props) {
  const id = useId()
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: AVAILABLE_MODELS_KEY, queryFn: ({ signal }) => fetchAvailableModels(signal) })
  const consent = useMutation({
    mutationFn: ({ model, version }: { model: AvailableModel; version: string }) => recordCloudConsent(model.id, version),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: AVAILABLE_MODELS_KEY }),
  })

  if (query.isPending) return <p>{ru.common.loading}</p>
  if (query.isError) return <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />
  const { items, cloud_models_enabled: cloudEnabled, cloud_notice: notice } = query.data
  const selected = items.find((m) => m.id === value?.modelId) ?? null

  return (
    <div className="space-y-3">
      {!cloudEnabled && <p className="text-xs text-slate-600">{t.cloudOff}</p>}
      {items.length === 0 ? (
        <p className="text-sm text-slate-600">{t.empty}</p>
      ) : (
        <div className="space-y-1">
          <label htmlFor={id} className="block text-sm font-medium text-slate-800">
            {t.select}
          </label>
          <select
            id={id}
            className={inputClass}
            value={value?.modelId ?? ''}
            onChange={(e) => onChange(e.target.value ? { modelId: e.target.value, cloudConsent: false } : null)}
          >
            <option value="">—</option>
            {items.map((m) => (
              <option key={m.id} value={m.id}>
                {m.display_name} · {m.provider_name} · {m.is_cloud ? t.cloudBadge : t.localBadge}
                {m.is_default ? ` · ${ru.llm.models.default}` : ''}
              </option>
            ))}
          </select>
          {selected && (
            <p className="text-xs text-slate-600">
              {ru.llm.models.contextWindow}: {selected.context_window ?? ru.common.unknown} · {ru.llm.models.pricing}:{' '}
              {pricingText(selected.pricing)}
            </p>
          )}
        </div>
      )}
      {selected?.is_cloud && value && (
        <div className="space-y-2">
          <Notice tone="warning">
            <span className="block whitespace-pre-line">{notice.text}</span>
          </Notice>
          {consent.isError && <ErrorNotice error={consent.error} />}
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={value.cloudConsent}
              disabled={consent.isPending}
              onChange={(e) => {
                const checked = e.target.checked
                onChange({ ...value, cloudConsent: checked })
                if (checked && selected.consent_required) consent.mutate({ model: selected, version: notice.version })
              }}
            />
            <span>{t.consentLabel}</span>
          </label>
        </div>
      )}
    </div>
  )
}
