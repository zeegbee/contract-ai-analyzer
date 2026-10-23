import { useQuery } from '@tanstack/react-query'
import { useId, useMemo, useState } from 'react'

import {
  ANALYSES_KEY,
  affectedSchema,
  fetchRisks,
  type Risk,
  severitySchema,
  validationSchema,
} from '../../api/analyses'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Button, inputClass } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { RiskCard } from './RiskCard'

const t = ru.analyses.risks

interface Filters {
  severity: string
  category: string
  party: string
  validation: string
  kind: string
  showRejected: boolean
}

const EMPTY: Filters = { severity: '', category: '', party: '', validation: '', kind: '', showRejected: false }

function matches(risk: Risk, f: Filters): boolean {
  if (!f.showRejected && !f.validation && risk.validation_status === 'rejected') return false
  if (f.severity && risk.severity !== f.severity) return false
  if (f.category && risk.category !== f.category) return false
  if (f.party && risk.affected_party !== f.party) return false
  if (f.validation && risk.validation_status !== f.validation) return false
  if (f.kind && risk.kind !== f.kind) return false
  return true
}

function Select({
  label,
  value,
  onChange,
  options,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  options: [string, string][]
}) {
  const id = useId()
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-xs font-medium text-slate-700">
        {label}
      </label>
      <select id={id} className={inputClass} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">{t.all}</option>
        {options.map(([v, l]) => (
          <option key={v} value={v}>
            {l}
          </option>
        ))}
      </select>
    </div>
  )
}

export function RisksSection({ analysisId, documentId }: { analysisId: string; documentId: string }) {
  const [filters, setFilters] = useState<Filters>(EMPTY)
  const query = useQuery({
    queryKey: [...ANALYSES_KEY, analysisId, 'risks'],
    queryFn: ({ signal }) => fetchRisks(analysisId, signal),
  })
  const risks = useMemo(() => query.data?.items ?? [], [query.data])
  const categories = useMemo(() => [...new Set(risks.map((r) => r.category))], [risks])
  const visible = risks.filter((r) => matches(r, filters))
  const rejected = risks.filter((r) => r.validation_status === 'rejected').length
  const set = (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch }))

  if (query.isPending) return <p aria-live="polite">{ru.common.loading}</p>
  if (query.isError) return <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />

  return (
    <section aria-labelledby="risks-title" className="space-y-4">
      <h2 id="risks-title" className="text-lg font-semibold">
        {t.title}
      </h2>
      {risks.length === 0 ? (
        <p className="text-sm text-slate-700">{t.none}</p>
      ) : (
        <>
          <fieldset className="grid gap-3 rounded-lg border border-slate-300 bg-white shadow-sm p-3 sm:grid-cols-2 lg:grid-cols-5">
            <legend className="px-1 text-sm font-medium">{t.filters}</legend>
            <Select
              label={t.severity}
              value={filters.severity}
              onChange={(v) => set({ severity: v })}
              options={severitySchema.options.map((s) => [s, t.severityLabels[s] ?? s])}
            />
            <Select
              label={t.category}
              value={filters.category}
              onChange={(v) => set({ category: v })}
              options={categories.map((c) => [c, ru.analyses.categories[c] ?? c])}
            />
            <Select
              label={t.party}
              value={filters.party}
              onChange={(v) => set({ party: v })}
              options={affectedSchema.options.map((p) => [p, t.affected[p] ?? p])}
            />
            <Select
              label={t.validation}
              value={filters.validation}
              onChange={(v) => set({ validation: v })}
              options={validationSchema.options
                .filter((v) => v !== 'pending')
                .map((v) => [v, t.validationLabels[v] ?? v])}
            />
            <Select
              label={t.kind}
              value={filters.kind}
              onChange={(v) => set({ kind: v })}
              options={Object.entries(t.kinds)}
            />
            <div className="flex flex-wrap items-center gap-3 sm:col-span-2 lg:col-span-5">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={filters.showRejected}
                  onChange={(e) => set({ showRejected: e.target.checked })}
                />
                {t.showRejected} ({rejected})
              </label>
              <Button onClick={() => setFilters(EMPTY)}>{t.reset}</Button>
              <span className="text-sm text-slate-700" aria-live="polite">
                {t.shown}: {visible.length} / {risks.length}
              </span>
            </div>
          </fieldset>
          {visible.length === 0 ? (
            <p className="text-sm text-slate-700">{t.empty}</p>
          ) : (
            <div className="space-y-4">
              {visible.map((r) => (
                <RiskCard key={r.id} risk={r} documentId={documentId} />
              ))}
            </div>
          )}
        </>
      )}
    </section>
  )
}
