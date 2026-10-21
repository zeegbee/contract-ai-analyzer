import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'

import {
  fetchRedactionSettings,
  REDACTION_CONFIG_KEY,
  REDACTION_SETTINGS_KEY,
  type RedactionCategory,
  type RedactionSettings,
  updateRedactionSettings,
} from '../../api/redaction'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Modal } from '../../components/Modal'
import { Button, inputClass, Notice } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatDateTime } from '../../lib/format'

const t = ru.redaction.settings
const GROUPS = ['personal', 'organization', 'commercial', 'technical']

function CategoryToggle({
  category,
  checked,
  disabled,
  onChange,
}: {
  category: RedactionCategory
  checked: boolean
  disabled: boolean
  onChange: (value: boolean) => void
}) {
  const id = useId()
  return (
    <li className="space-y-0.5">
      <label htmlFor={id} className="flex items-start gap-2">
        <input
          id={id}
          type="checkbox"
          checked={checked}
          disabled={disabled || !category.supported}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span>
          {category.title}
          <span className="block text-xs text-slate-600">
            {category.method} · {t.reliability[category.reliability] ?? category.reliability} · ⟦{category.label}_N⟧
          </span>
          {category.warning && checked && <span className="block text-xs text-amber-800">{category.warning}</span>}
        </span>
      </label>
    </li>
  )
}

/**
 * Настройки маскировки: категории, разрешение отправки без маскировки, словарь, шаблоны, исключения,
 * срок предпросмотра. Admin — только чтение; изменения и проверку шаблонов выполняет сервер (Superadmin).
 */
export function RedactionSection() {
  const ids = { allow: useId(), allowConfirm: useId(), ttl: useId(), allowList: useId() }
  const queryClient = useQueryClient()
  const current = useQuery({ queryKey: REDACTION_SETTINGS_KEY, queryFn: ({ signal }) => fetchRedactionSettings(signal) })
  const [draft, setDraft] = useState<RedactionSettings | null>(null)
  const [allowList, setAllowList] = useState<string | null>(null)
  const [askAllow, setAskAllow] = useState(false)
  const [allowConfirmed, setAllowConfirmed] = useState(false)

  const save = useMutation({
    mutationFn: ({ settings, confirm }: { settings: RedactionSettings; confirm: boolean }) =>
      updateRedactionSettings(settings, confirm),
    onSuccess: (result) => {
      queryClient.setQueryData(REDACTION_SETTINGS_KEY, result)
      void queryClient.invalidateQueries({ queryKey: REDACTION_CONFIG_KEY })
      setDraft(null)
      setAllowList(null)
      setAskAllow(false)
      setAllowConfirmed(false)
    },
  })

  const data = current.data
  const settings = draft ?? data?.settings ?? null
  const canManage = Boolean(data?.can_manage)
  const update = (patch: Partial<RedactionSettings>) => {
    if (settings) setDraft({ ...settings, ...patch })
  }
  const supported = (data?.categories ?? []).filter((c) => c.supported)
  const allowListText = allowList ?? settings?.allow_list.join('\n') ?? ''
  const finalSettings = (): RedactionSettings | null =>
    settings && {
      ...settings,
      allow_list: allowListText
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean),
    }
  const enablingUnmasked = Boolean(settings?.allow_unmasked && !data?.settings.allow_unmasked)

  return (
    <section aria-labelledby="redaction-settings-title" className="space-y-3 rounded-lg border border-slate-200 bg-white p-4 text-sm">
      <h2 id="redaction-settings-title" className="text-lg font-semibold">
        {t.title}
      </h2>
      <p className="text-slate-700">{t.description}</p>
      {current.isPending && <p>{ru.common.loading}</p>}
      {current.isError && <ErrorNotice error={current.error} onRetry={() => void current.refetch()} />}
      {data && settings && (
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault()
            const body = finalSettings()
            if (!body) return
            // Включение отправки без маскировки — только после отдельного предупреждения и подтверждения.
            if (enablingUnmasked) setAskAllow(true)
            else save.mutate({ settings: body, confirm: false })
          }}
        >
          <p className="text-slate-600">
            {data.updated_at ? `${t.updated}: ${formatDateTime(data.updated_at)}, ` : ''}
            {t.revision} {data.revision}
          </p>
          {!canManage && <p className="text-slate-600">{t.superadminOnly}</p>}

          <div className="grid gap-4 md:grid-cols-2">
            {GROUPS.map((group) => (
              <fieldset key={group} className="space-y-2">
                <legend className="font-semibold">{t.groups[group] ?? group}</legend>
                <ul className="space-y-1.5">
                  {data.categories
                    .filter((c) => c.group === group)
                    .map((c) => (
                      <CategoryToggle
                        key={c.code}
                        category={c}
                        checked={Boolean(settings.categories[c.code])}
                        disabled={!canManage}
                        onChange={(value) => update({ categories: { ...settings.categories, [c.code]: value } })}
                      />
                    ))}
                </ul>
              </fieldset>
            ))}
          </div>

          <div className="space-y-1 rounded-md border border-amber-200 bg-amber-50 p-3">
            <label htmlFor={ids.allow} className="flex items-start gap-2 font-medium">
              <input
                id={ids.allow}
                type="checkbox"
                checked={settings.allow_unmasked}
                disabled={!canManage}
                onChange={(e) => update({ allow_unmasked: e.target.checked })}
              />
              {t.allowUnmasked}
            </label>
            <p className="text-xs text-amber-900">{t.allowUnmaskedHint}</p>
          </div>

          <fieldset className="space-y-2">
            <legend className="font-semibold">{t.dictionary}</legend>
            {settings.custom_dictionary.map((item, index) => (
              <div key={index} className="flex flex-wrap gap-2">
                <input
                  aria-label={t.phrase}
                  className={`${inputClass} max-w-72`}
                  value={item.text}
                  disabled={!canManage}
                  maxLength={200}
                  onChange={(e) => {
                    const next = [...settings.custom_dictionary]
                    next[index] = { ...item, text: e.target.value }
                    update({ custom_dictionary: next })
                  }}
                />
                <CategorySelect
                  value={item.category}
                  categories={supported}
                  disabled={!canManage}
                  onChange={(category) => {
                    const next = [...settings.custom_dictionary]
                    next[index] = { ...item, category }
                    update({ custom_dictionary: next })
                  }}
                />
                {canManage && (
                  <Button
                    onClick={() => update({ custom_dictionary: settings.custom_dictionary.filter((_, i) => i !== index) })}
                  >
                    {t.remove}
                  </Button>
                )}
              </div>
            ))}
            {canManage && (
              <Button
                onClick={() =>
                  update({
                    custom_dictionary: [...settings.custom_dictionary, { text: '', category: 'goods_and_projects' }],
                  })
                }
              >
                {t.add}
              </Button>
            )}
          </fieldset>

          <fieldset className="space-y-2">
            <legend className="font-semibold">{t.patterns}</legend>
            {settings.custom_patterns.map((item, index) => (
              <div key={index} className="flex flex-wrap gap-2">
                <input
                  aria-label={t.pattern}
                  className={`${inputClass} max-w-72 font-mono`}
                  value={item.pattern}
                  disabled={!canManage}
                  maxLength={300}
                  onChange={(e) => {
                    const next = [...settings.custom_patterns]
                    next[index] = { ...item, pattern: e.target.value }
                    update({ custom_patterns: next })
                  }}
                />
                <CategorySelect
                  value={item.category}
                  categories={supported}
                  disabled={!canManage}
                  onChange={(category) => {
                    const next = [...settings.custom_patterns]
                    next[index] = { ...item, category }
                    update({ custom_patterns: next })
                  }}
                />
                {canManage && (
                  <Button
                    onClick={() => update({ custom_patterns: settings.custom_patterns.filter((_, i) => i !== index) })}
                  >
                    {t.remove}
                  </Button>
                )}
              </div>
            ))}
            {canManage && (
              <Button
                onClick={() =>
                  update({ custom_patterns: [...settings.custom_patterns, { pattern: '', category: 'goods_and_projects' }] })
                }
              >
                {t.add}
              </Button>
            )}
          </fieldset>

          <div className="space-y-1">
            <label htmlFor={ids.allowList} className="block font-semibold">
              {t.allowList}
            </label>
            <textarea
              id={ids.allowList}
              className={`${inputClass} min-h-24`}
              value={allowListText}
              disabled={!canManage}
              onChange={(e) => setAllowList(e.target.value)}
            />
          </div>

          <div className="space-y-1">
            <label htmlFor={ids.ttl} className="block font-semibold">
              {t.ttl}
            </label>
            <input
              id={ids.ttl}
              type="number"
              min={1}
              max={168}
              className={`${inputClass} max-w-32`}
              value={settings.preview_ttl_hours}
              disabled={!canManage}
              onChange={(e) => update({ preview_ttl_hours: Number(e.target.value) })}
            />
          </div>

          {save.isError && <ErrorNotice error={save.error} />}
          {save.isSuccess && <Notice tone="success">{t.saved}</Notice>}
          {canManage && (
            <Button type="submit" variant="primary" disabled={save.isPending || (draft === null && allowList === null)}>
              {t.save}
            </Button>
          )}
        </form>
      )}

      {askAllow && (
        <Modal title={t.allowTitle} onClose={() => setAskAllow(false)}>
          <p className="text-red-900">{t.allowWarning}</p>
          <label htmlFor={ids.allowConfirm} className="flex items-start gap-2 font-medium">
            <input
              id={ids.allowConfirm}
              type="checkbox"
              checked={allowConfirmed}
              onChange={(e) => setAllowConfirmed(e.target.checked)}
            />
            {t.allowConfirm}
          </label>
          {save.isError && <ErrorNotice error={save.error} />}
          <div className="flex flex-wrap gap-2">
            <Button
              variant="danger"
              disabled={!allowConfirmed || save.isPending}
              onClick={() => {
                const body = finalSettings()
                if (body) save.mutate({ settings: body, confirm: true })
              }}
            >
              {t.allow}
            </Button>
            <Button onClick={() => setAskAllow(false)}>{t.cancel}</Button>
          </div>
        </Modal>
      )}
    </section>
  )
}

function CategorySelect({
  value,
  categories,
  disabled,
  onChange,
}: {
  value: string
  categories: RedactionCategory[]
  disabled: boolean
  onChange: (value: string) => void
}) {
  return (
    <select
      aria-label={t.category}
      className={`${inputClass} w-auto`}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    >
      {categories.map((c) => (
        <option key={c.code} value={c.code}>
          {c.short}
        </option>
      ))}
    </select>
  )
}
