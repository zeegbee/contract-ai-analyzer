import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useId, useState } from 'react'

import { CATEGORIES, type Category, type Severity } from '../../api/analyses'
import {
  createRiskRule,
  fetchRiskRuleRevisions,
  fetchRiskRules,
  RISK_RULES_KEY,
  type RiskRule,
  type RiskRuleInput,
  RULE_KINDS,
  type RuleKind,
  setRiskRuleArchived,
  updateRiskRule,
} from '../../api/riskRules'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Modal } from '../../components/Modal'
import { Button, Field, groupCardClass, inputClass, Notice, PageTitle } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatDateTime } from '../../lib/format'

const t = ru.riskRules
const SEVERITIES: Severity[] = ['critical', 'high', 'medium', 'low', 'informational']
const TITLE_MAX = 200

interface Draft {
  title: string
  category: Category
  kind: RuleKind
  description: string
  signs: string
  severity: Severity | ''
  recommendation: string
  enabled: boolean
  default_selected: boolean
}

const EMPTY: Draft = {
  title: '',
  category: 'other',
  kind: 'clause_risk',
  description: '',
  signs: '',
  severity: '',
  recommendation: '',
  enabled: true,
  default_selected: true,
}

function draftFrom(r: RiskRule): Draft {
  return {
    title: r.title,
    category: r.category,
    kind: r.kind,
    description: r.description,
    signs: r.signs ?? '',
    severity: r.severity ?? '',
    recommendation: r.recommendation ?? '',
    enabled: r.enabled,
    default_selected: r.default_selected,
  }
}

function inputFrom(d: Draft): RiskRuleInput {
  return {
    title: d.title.trim(),
    category: d.category,
    kind: d.kind,
    description: d.description.trim(),
    signs: d.signs.trim() || null,
    severity: d.severity || null,
    recommendation: d.recommendation.trim() || null,
    enabled: d.enabled,
    default_selected: d.default_selected,
  }
}

function Counter({ value, max }: { value: string; max: number }) {
  return (
    <span className={`text-xs ${value.length > max ? 'text-red-700' : 'text-slate-500'}`}>
      {value.length} / {max} {t.chars}
    </span>
  )
}

function RuleForm({
  rule,
  textMax,
  onDone,
  onCancel,
}: {
  rule: RiskRule | null
  textMax: number
  onDone: (saved: RiskRule) => void
  onCancel: () => void
}) {
  const ids = {
    title: useId(),
    category: useId(),
    kind: useId(),
    description: useId(),
    signs: useId(),
    severity: useId(),
    recommendation: useId(),
  }
  const [draft, setDraft] = useState<Draft>(rule ? draftFrom(rule) : EMPTY)
  const [problem, setProblem] = useState<string | null>(null)
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => ({ ...d, [key]: value }))
  const save = useMutation({
    mutationFn: () =>
      rule ? updateRiskRule(rule.id, { ...inputFrom(draft), version: rule.version }) : createRiskRule(inputFrom(draft)),
    onSuccess: onDone,
  })
  const tooLong =
    draft.title.length > TITLE_MAX ||
    [draft.description, draft.signs, draft.recommendation].some((v) => v.length > textMax)
  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (!draft.title.trim() || !draft.description.trim()) {
      setProblem(t.required)
      return
    }
    setProblem(null)
    save.mutate()
  }
  const area = (key: 'description' | 'signs' | 'recommendation', id: string, label: string, hint?: string) => (
    <Field id={id} label={label} hint={hint}>
      <textarea
        id={id}
        className={inputClass}
        rows={key === 'description' ? 4 : 3}
        value={draft[key]}
        onChange={(e) => set(key, e.target.value)}
      />
      <Counter value={draft[key]} max={textMax} />
    </Field>
  )

  return (
    <form onSubmit={onSubmit} noValidate className="space-y-3">
      <Notice tone="warning">{t.dataWarning}</Notice>
      <Field id={ids.title} label={t.fields.title}>
        <input
          id={ids.title}
          className={inputClass}
          value={draft.title}
          onChange={(e) => set('title', e.target.value)}
        />
        <Counter value={draft.title} max={TITLE_MAX} />
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field id={ids.category} label={t.fields.category}>
          <select
            id={ids.category}
            className={inputClass}
            value={draft.category}
            onChange={(e) => set('category', e.target.value as Category)}
          >
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {ru.analyses.categories[c]}
              </option>
            ))}
          </select>
        </Field>
        <Field id={ids.severity} label={t.fields.severity}>
          <select
            id={ids.severity}
            className={inputClass}
            value={draft.severity}
            onChange={(e) => set('severity', e.target.value as Severity | '')}
          >
            <option value="">{t.fields.severityNone}</option>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {ru.analyses.risks.severityLabels[s]}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <Field id={ids.kind} label={t.fields.kind}>
        <select
          id={ids.kind}
          className={inputClass}
          value={draft.kind}
          onChange={(e) => set('kind', e.target.value as RuleKind)}
        >
          {RULE_KINDS.map((k) => (
            <option key={k} value={k}>
              {t.kinds[k]}
            </option>
          ))}
        </select>
      </Field>
      {area('description', ids.description, t.fields.description, t.fields.descriptionHint)}
      {area('signs', ids.signs, t.fields.signs, t.fields.signsHint)}
      {area('recommendation', ids.recommendation, t.fields.recommendation)}
      <label className="flex items-center gap-2">
        <input type="checkbox" checked={draft.enabled} onChange={(e) => set('enabled', e.target.checked)} />
        {t.fields.enabled}
      </label>
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={draft.default_selected}
          onChange={(e) => set('default_selected', e.target.checked)}
        />
        {t.fields.defaultSelected}
      </label>
      {problem && (
        <p role="alert" className="text-sm text-red-800">
          {problem}
        </p>
      )}
      {save.isError && <ErrorNotice error={save.error} />}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="primary" disabled={save.isPending || tooLong}>
          {save.isPending ? t.saving : t.save}
        </Button>
        <Button onClick={onCancel}>{t.cancel}</Button>
      </div>
    </form>
  )
}

function Revisions({ ruleId }: { ruleId: string }) {
  const query = useQuery({
    queryKey: [...RISK_RULES_KEY, 'revisions', ruleId],
    queryFn: ({ signal }) => fetchRiskRuleRevisions(ruleId, signal),
  })
  return (
    <details className="text-sm">
      <summary className="cursor-pointer text-blue-800">{t.history}</summary>
      {query.isError && <ErrorNotice error={query.error} />}
      {query.data?.length === 0 && <p className="text-slate-600">{t.noHistory}</p>}
      <ul className="mt-1 space-y-1">
        {query.data?.map((r) => (
          <li key={r.id} className="text-xs text-slate-700">
            {t.revision} {r.revision} · {formatDateTime(r.created_at)} ·{' '}
            <span className="font-mono">{Object.keys(r.diff).join(', ')}</span>
          </li>
        ))}
      </ul>
    </details>
  )
}

function RuleRow({ rule, onEdit }: { rule: RiskRule; onEdit: () => void }) {
  const queryClient = useQueryClient()
  const refresh = () => void queryClient.invalidateQueries({ queryKey: RISK_RULES_KEY })
  const toggle = useMutation({
    mutationFn: (patch: { enabled?: boolean; default_selected?: boolean }) =>
      updateRiskRule(rule.id, { ...patch, version: rule.version }),
    onSettled: refresh,
  })
  const archive = useMutation({
    mutationFn: (archived: boolean) => setRiskRuleArchived(rule, archived),
    onSettled: refresh,
  })
  const archived = rule.archived_at !== null
  const label = `${rule.code} ${rule.title}`
  return (
    <tr className="align-top">
      <td className="px-3 py-2 font-mono whitespace-nowrap">{rule.code}</td>
      <td className="px-3 py-2">
        <p className="font-medium">{rule.title}</p>
        <p className="text-xs whitespace-pre-line text-slate-600">{rule.description}</p>
        <Revisions ruleId={rule.id} />
        {(toggle.isError || archive.isError) && <ErrorNotice error={toggle.error ?? archive.error} />}
      </td>
      <td className="px-3 py-2">{ru.analyses.categories[rule.category]}</td>
      <td className="px-3 py-2">{t.kindsShort[rule.kind]}</td>
      <td className="px-3 py-2 text-center">
        <input
          type="checkbox"
          aria-label={`${t.columns.enabled}: ${label}`}
          checked={rule.enabled}
          disabled={archived || toggle.isPending}
          onChange={(e) => toggle.mutate({ enabled: e.target.checked })}
        />
      </td>
      <td className="px-3 py-2 text-center">
        <input
          type="checkbox"
          aria-label={`${t.columns.defaultSelected}: ${label}`}
          checked={rule.default_selected}
          disabled={archived || toggle.isPending}
          onChange={(e) => toggle.mutate({ default_selected: e.target.checked })}
        />
      </td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap gap-1">
          {!archived && (
            <Button aria-label={`${t.edit}: ${label}`} onClick={onEdit}>
              {t.edit}
            </Button>
          )}
          <Button
            variant={archived ? 'secondary' : 'danger'}
            aria-label={`${archived ? t.restore : t.archive}: ${label}`}
            disabled={archive.isPending}
            onClick={() => {
              if (archived || window.confirm(t.confirmArchive)) archive.mutate(!archived)
            }}
          >
            {archived ? t.restore : t.archive}
          </Button>
        </div>
      </td>
    </tr>
  )
}

export function RiskRulesPage() {
  const queryClient = useQueryClient()
  const filterId = useId()
  const [showArchive, setShowArchive] = useState(false)
  const [category, setCategory] = useState<Category | ''>('')
  // null — форма закрыта, 'new' — новое правило, иначе — изменяемое правило.
  const [editing, setEditing] = useState<RiskRule | 'new' | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const query = useQuery({
    queryKey: [...RISK_RULES_KEY, 'admin', showArchive],
    queryFn: ({ signal }) => fetchRiskRules(showArchive, signal),
  })
  const items = (query.data?.items ?? []).filter((r) => !category || r.category === category)
  const limits = query.data?.limits

  return (
    <section aria-labelledby="risk-rules-title" className="space-y-4">
      <PageTitle
        id="risk-rules-title"
        actions={
          <Button
            variant="primary"
            disabled={limits !== undefined && !showArchive && (query.data?.items.length ?? 0) >= limits.catalog}
            onClick={() => {
              setMessage(null)
              setEditing('new')
            }}
          >
            {t.create}
          </Button>
        }
      >
        {t.title}
      </PageTitle>
      <p className="max-w-3xl text-sm text-slate-700">{t.intro}</p>
      {message && <Notice tone="success">{message}</Notice>}

      <div className={`flex flex-wrap items-end gap-4 ${groupCardClass}`}>
        <div className="space-y-1">
          <label htmlFor={filterId} className="block text-sm font-medium">
            {t.category}
          </label>
          <select
            id={filterId}
            className={inputClass}
            value={category}
            onChange={(e) => setCategory(e.target.value as Category | '')}
          >
            <option value="">{t.allCategories}</option>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {ru.analyses.categories[c]}
              </option>
            ))}
          </select>
        </div>
        <label className="flex items-center gap-2 pb-1.5 text-sm">
          <input type="checkbox" checked={showArchive} onChange={(e) => setShowArchive(e.target.checked)} />
          {t.showArchive}
        </label>
        {limits && !showArchive && (
          <p className="pb-1.5 text-sm text-slate-600">
            {t.count}: {query.data?.items.length ?? 0} / {limits.catalog}
          </p>
        )}
      </div>

      {query.isPending && <p>{ru.common.loading}</p>}
      {query.isError && <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />}
      {query.isSuccess && items.length === 0 && (
        <p className="text-sm text-slate-600">{showArchive ? t.emptyArchive : t.empty}</p>
      )}
      {items.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-300 bg-white shadow-sm">
          <table className="w-full text-left text-sm">
            <thead className="bg-slate-50 text-slate-600">
              <tr>
                <th className="px-3 py-2 font-medium">{t.columns.code}</th>
                <th className="px-3 py-2 font-medium">{t.columns.title}</th>
                <th className="px-3 py-2 font-medium">{t.columns.category}</th>
                <th className="px-3 py-2 font-medium">{t.columns.kind}</th>
                <th className="px-3 py-2 font-medium">{t.columns.enabled}</th>
                <th className="px-3 py-2 font-medium">{t.columns.defaultSelected}</th>
                <th className="px-3 py-2 font-medium">{t.columns.actions}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200">
              {items.map((r) => (
                <RuleRow
                  key={r.id}
                  rule={r}
                  onEdit={() => {
                    setMessage(null)
                    setEditing(r)
                  }}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <Modal title={editing === 'new' ? t.create : `${editing.code} · ${t.edit}`} onClose={() => setEditing(null)}>
          <RuleForm
            rule={editing === 'new' ? null : editing}
            textMax={limits?.text_chars ?? 1000}
            onCancel={() => setEditing(null)}
            onDone={() => {
              setMessage(editing === 'new' ? t.created : t.saved)
              setEditing(null)
              void queryClient.invalidateQueries({ queryKey: RISK_RULES_KEY })
            }}
          />
        </Modal>
      )}
    </section>
  )
}
