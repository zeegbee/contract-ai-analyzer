import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'

import { fetchLlmSettings, LLM_SETTINGS_KEY } from '../../api/catalog'
import { fetchSystemSettings } from '../../api/settings'
import { ErrorNotice } from '../../components/ErrorNotice'
import { PageTitle } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { BackupSection } from './BackupSection'
import { RedactionSection } from './RedactionSection'
import { RetentionSection } from './RetentionSection'

const t = ru.settings
const number = new Intl.NumberFormat('ru-RU')

export function SettingsPage() {
  const system = useQuery({ queryKey: ['settings', 'system'], queryFn: ({ signal }) => fetchSystemSettings(signal) })
  const llm = useQuery({ queryKey: LLM_SETTINGS_KEY, queryFn: ({ signal }) => fetchLlmSettings(signal) })

  return (
    <section aria-labelledby="settings-title" className="space-y-4">
      <PageTitle id="settings-title">{t.title}</PageTitle>

      <section aria-labelledby="llm-settings-title" className="space-y-2 rounded-lg border border-slate-200 bg-white p-4 text-sm">
        <h2 id="llm-settings-title" className="text-lg font-semibold">
          {t.llm}
        </h2>
        {llm.isPending && <p>{ru.common.loading}</p>}
        {llm.isError && <ErrorNotice error={llm.error} onRetry={() => void llm.refetch()} />}
        {llm.data && (
          <>
            <p>{llm.data.cloud_models_enabled ? t.cloudOn : t.cloudOff}</p>
            <p className="text-slate-700">
              {t.noticeVersion}: {llm.data.cloud_notice.version}
            </p>
          </>
        )}
        <Link to="/admin/models" className="text-blue-800 underline">
          {t.llmLink}
        </Link>
      </section>

      <RedactionSection />

      <RetentionSection />

      <BackupSection />

      <p className="text-sm text-slate-700">{t.readOnly}</p>
      {system.isPending && <p>{ru.common.loading}</p>}
      {system.isError && <ErrorNotice error={system.error} onRetry={() => void system.refetch()} />}
      {system.data && (
        <div className="grid gap-4 md:grid-cols-2">
          {Object.entries(system.data.groups).map(([group, params]) => (
            <section key={group} aria-label={t.groups[group] ?? group} className="rounded-lg border border-slate-200 bg-white p-4">
              <h2 className="mb-2 font-semibold">{t.groups[group] ?? group}</h2>
              <dl className="space-y-1 text-sm">
                {params.map((p) => (
                  <div key={p.key} className="flex items-baseline justify-between gap-4">
                    {/* Длинное название переносится внутри колонки, значение остаётся справа в той же строке. */}
                    <dt className="min-w-0 text-slate-700">
                      {t.params[p.key] ?? p.key}
                      <code className="block text-xs break-words text-slate-500">{p.env}</code>
                    </dt>
                    <dd className="shrink-0 text-right font-medium whitespace-nowrap tabular-nums">
                      {typeof p.value === 'number' ? number.format(p.value) : String(p.value)}
                    </dd>
                  </div>
                ))}
              </dl>
            </section>
          ))}
        </div>
      )}
    </section>
  )
}
