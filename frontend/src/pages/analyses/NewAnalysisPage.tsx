import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useId, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'

import {
  ANALYSES_KEY,
  type AnalysisRequest,
  CATEGORIES,
  type Category,
  createAnalysis,
  type Depth,
  type Estimate,
  estimateAnalysis,
  type Party,
} from '../../api/analyses'
import { AVAILABLE_MODELS_KEY, fetchAvailableModels } from '../../api/catalog'
import { DOCUMENTS_KEY, fetchDocument, fetchDocuments } from '../../api/documents'
import { AVAILABLE_RULES_KEY, type AvailableRule, fetchAvailableRules } from '../../api/riskRules'
import { ErrorNotice } from '../../components/ErrorNotice'
import { type ModelChoice, ModelSelect } from '../../components/ModelSelect'
import { Button, inputClass, Notice, groupCardClass, PageTitle } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatDateTime } from '../../lib/format'
import { RedactionPreview } from './RedactionPreview'

const t = ru.analyses.form
const PARTIES: Party[] = ['party_a', 'party_b', 'named', 'unknown']
const DEPTHS: Depth[] = ['quick', 'standard', 'deep']
const number = new Intl.NumberFormat('ru-RU')

// Предупреждение, если правила организации увеличивают оценку объёма больше чем на 25 %.
const RULES_EXTRA_WARNING_SHARE = 0.25

function EstimateView({ estimate }: { estimate: Estimate }) {
  const base = estimate.estimated_tokens - estimate.rules_extra_tokens
  return (
    <div className="space-y-1 rounded-md border border-slate-200 bg-slate-50 p-3 text-sm" aria-live="polite">
      <p className="font-medium">{t.estimateTitle}</p>
      <p>
        {t.chunks}: {estimate.chunks} · {t.tokens}: {number.format(estimate.estimated_tokens)} · {t.limit}:{' '}
        {number.format(estimate.token_limit)}
      </p>
      <p>
        {t.cost}:{' '}
        {estimate.estimated_cost ? `≈ ${estimate.estimated_cost} ${estimate.cost_currency ?? ''}` : t.costUnknown}
      </p>
      {estimate.rules_extra_tokens > 0 && (
        <p>
          {t.rulesExtra}: {number.format(estimate.rules_extra_tokens)}
        </p>
      )}
      {base > 0 && estimate.rules_extra_tokens / base > RULES_EXTRA_WARNING_SHARE && (
        <p className="text-amber-900">{t.rulesExtraWarning}</p>
      )}
      <p className="text-xs text-slate-600">{estimate.note}</p>
      {!estimate.within_limit && <p className="text-red-800">{t.overLimit}</p>}
      {estimate.cloud_transfer && <p className="text-amber-900">{t.cloudTransfer}</p>}
    </div>
  )
}

export function NewAnalysisPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const ids = { document: useId(), version: useId(), partyName: useId() }
  const [documentId, setDocumentId] = useState(params.get('document') ?? '')
  const [versionId, setVersionId] = useState(params.get('version') ?? '')
  const [party, setParty] = useState<Party>('party_a')
  const [partyName, setPartyName] = useState('')
  const [categories, setCategories] = useState<Set<Category>>(new Set(CATEGORIES))
  // Явный выбор пользователя по правилам; без него действует «отмечено по умолчанию» из правила.
  const [ruleChoice, setRuleChoice] = useState<Record<string, boolean>>({})
  const [depth, setDepth] = useState<Depth>('standard')
  const [model, setModel] = useState<ModelChoice | null>(null)
  const [estimate, setEstimate] = useState<Estimate | null>(null)
  const [errors, setErrors] = useState<string[]>([])
  // Облачная модель: после параметров — обязательный шаг предпросмотра маскировки (этап 10).
  const [redactionFor, setRedactionFor] = useState<AnalysisRequest | null>(null)
  // Один ключ на форму: повторное нажатие или повтор после обрыва связи не создаст второй анализ.
  const idempotencyKey = useRef(crypto.randomUUID().replaceAll('-', ''))

  const documents = useQuery({
    queryKey: [...DOCUMENTS_KEY, 'list', 'for-analysis'],
    queryFn: ({ signal }) => fetchDocuments(null, signal),
  })
  const document = useQuery({
    queryKey: [...DOCUMENTS_KEY, documentId],
    queryFn: ({ signal }) => fetchDocument(documentId, signal),
    enabled: Boolean(documentId),
  })
  const available = useQuery({ queryKey: AVAILABLE_MODELS_KEY, queryFn: ({ signal }) => fetchAvailableModels(signal) })
  const rules = useQuery({ queryKey: AVAILABLE_RULES_KEY, queryFn: ({ signal }) => fetchAvailableRules(signal) })
  const rulesByCategory = useMemo(() => {
    const map = new Map<Category, AvailableRule[]>()
    for (const r of rules.data?.items ?? []) map.set(r.category, [...(map.get(r.category) ?? []), r])
    return map
  }, [rules.data])
  // Правило применяется только вместе со своей категорией.
  const ruleSelected = (r: AvailableRule) => categories.has(r.category) && (ruleChoice[r.id] ?? r.default_selected)
  const selectedRules = (rules.data?.items ?? []).filter(ruleSelected)
  const rulesLimit = rules.data?.limits.per_analysis ?? 0
  const rulesOverLimit = rules.isSuccess && selectedRules.length > rulesLimit
  const selectedModel = available.data?.items.find((m) => m.id === model?.modelId) ?? null

  const candidates = useMemo(
    () =>
      (documents.data?.items ?? []).filter(
        (d) => d.can_analyze && d.current_version?.extraction_status === 'ready',
      ),
    [documents.data],
  )
  const versions = (document.data?.versions ?? []).filter((v) => v.extraction_status === 'ready')
  const effectiveVersionId = versionId || document.data?.current_version_id || ''
  const isCloud = Boolean(selectedModel?.is_cloud)

  const request = (): AnalysisRequest | null => {
    const problems: string[] = []
    if (!documentId) problems.push(t.required.document)
    if (categories.size === 0) problems.push(t.required.categories)
    if (party === 'named' && !partyName.trim()) problems.push(t.required.partyName)
    if (!model) problems.push(t.required.model)
    if (selectedModel?.is_cloud && !model?.cloudConsent) problems.push(t.required.consent)
    if (rulesOverLimit) problems.push(t.rulesOverLimit)
    setErrors(problems)
    if (problems.length > 0 || !model) return null
    return {
      document_id: documentId,
      version_id: versionId || null,
      party,
      party_name: party === 'named' ? partyName.trim() : null,
      categories: CATEGORIES.filter((c) => categories.has(c)),
      depth,
      model_id: model.modelId,
      cloud_consent: model.cloudConsent,
      // Без выбранных правил поле не передаётся (запрос — как до этапа 11).
      rule_ids: selectedRules.length > 0 ? selectedRules.map((r) => r.id) : undefined,
    }
  }

  const estimateMutation = useMutation({ mutationFn: estimateAnalysis, onSuccess: setEstimate })
  const submit = useMutation({
    mutationFn: (body: AnalysisRequest) => createAnalysis(body, idempotencyKey.current),
    onSuccess: (analysis) => {
      void queryClient.invalidateQueries({ queryKey: ANALYSES_KEY })
      void navigate(`/analyses/${encodeURIComponent(analysis.id)}`)
    },
  })

  const resetEstimate = () => setEstimate(null)
  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    const body = request()
    if (!body) return
    if (isCloud) {
      submit.reset()
      setRedactionFor(body)
    } else {
      submit.mutate(body)
    }
  }

  if (redactionFor && selectedModel && effectiveVersionId) {
    return (
      <section aria-labelledby="new-analysis-title" className="space-y-4">
        <PageTitle id="new-analysis-title">{t.title}</PageTitle>
        <RedactionPreview
          documentId={redactionFor.document_id}
          versionId={effectiveVersionId}
          model={selectedModel}
          submitting={submit.isPending}
          submitError={submit.error}
          onBack={() => setRedactionFor(null)}
          onConfirmed={(previewId) =>
            submit.mutate({ ...redactionFor, version_id: effectiveVersionId, redaction_preview_id: previewId })
          }
        />
      </section>
    )
  }

  const toggle = (c: Category) => {
    setCategories((prev) => {
      const next = new Set(prev)
      if (next.has(c)) next.delete(c)
      else next.add(c)
      return next
    })
    resetEstimate()
  }
  const toggleRule = (r: AvailableRule) => {
    setRuleChoice((prev) => ({ ...prev, [r.id]: !ruleSelected(r) }))
    resetEstimate()
  }
  const setAll = (selected: boolean) => {
    setCategories(new Set(selected ? CATEGORIES : []))
    setRuleChoice(Object.fromEntries((rules.data?.items ?? []).map((r) => [r.id, selected])))
    resetEstimate()
  }

  return (
    <section aria-labelledby="new-analysis-title" className="max-w-3xl space-y-4">
      <PageTitle id="new-analysis-title">{t.title}</PageTitle>
      <p className="text-sm text-slate-700">{t.disclaimer}</p>
      <form onSubmit={onSubmit} noValidate className="space-y-5">
        <div className={`space-y-1 ${groupCardClass}`}>
          <label htmlFor={ids.document} className="block text-sm font-medium">
            {t.document}
          </label>
          {documents.isError && <ErrorNotice error={documents.error} onRetry={() => void documents.refetch()} />}
          {documents.isSuccess && candidates.length === 0 && !documentId ? (
            <p className="text-sm text-slate-600">{t.noDocuments}</p>
          ) : (
            <select
              id={ids.document}
              className={inputClass}
              value={documentId}
              onChange={(e) => {
                setDocumentId(e.target.value)
                setVersionId('')
                resetEstimate()
              }}
            >
              <option value="">{t.documentPlaceholder}</option>
              {candidates.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.title}
                </option>
              ))}
              {documentId && !candidates.some((d) => d.id === documentId) && document.data && (
                <option value={documentId}>{document.data.title}</option>
              )}
            </select>
          )}
        </div>
        {documentId && versions.length > 1 && (
          <div className={`space-y-1 ${groupCardClass}`}>
            <label htmlFor={ids.version} className="block text-sm font-medium">
              {t.version}
            </label>
            <select
              id={ids.version}
              className={inputClass}
              value={versionId}
              onChange={(e) => {
                setVersionId(e.target.value)
                resetEstimate()
              }}
            >
              <option value="">—</option>
              {versions.map((v) => (
                <option key={v.id} value={v.id}>
                  {t.versionLabel} {v.version_no} · {v.original_filename} · {formatDateTime(v.created_at)}
                </option>
              ))}
            </select>
          </div>
        )}
        {document.isError && <ErrorNotice error={document.error} />}

        <fieldset className={`space-y-2 ${groupCardClass}`}>
          <legend className="text-sm font-semibold">{t.party}</legend>
          {PARTIES.map((p) => (
            <label key={p} className="flex items-start gap-2 text-sm">
              <input type="radio" name="party" value={p} checked={party === p} onChange={() => setParty(p)} />
              <span>
                {ru.analyses.party[p]} — <span className="text-slate-600">{ru.analyses.partyHint[p]}</span>
              </span>
            </label>
          ))}
          {party === 'named' && (
            <div className="space-y-1 pl-6">
              <label htmlFor={ids.partyName} className="block text-sm">
                {t.partyName}
              </label>
              <input
                id={ids.partyName}
                className={inputClass}
                maxLength={300}
                value={partyName}
                onChange={(e) => setPartyName(e.target.value)}
              />
            </div>
          )}
        </fieldset>

        <fieldset className={`space-y-2 ${groupCardClass}`}>
          <legend className="text-sm font-semibold">{t.categories}</legend>
          <div className="flex gap-2">
            <Button onClick={() => setAll(true)}>{t.selectAll}</Button>
            <Button onClick={() => setAll(false)}>{t.clearAll}</Button>
          </div>
          {rulesByCategory.size > 0 && <p className="text-xs text-slate-600">{t.rulesHint}</p>}
          <div className="grid items-start gap-x-4 gap-y-1 sm:grid-cols-2">
            {CATEGORIES.map((c) => (
              <div key={c}>
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={categories.has(c)} onChange={() => toggle(c)} />
                  {ru.analyses.categories[c]}
                </label>
                {(rulesByCategory.get(c) ?? []).length > 0 && (
                  <ul aria-label={`${t.rulesTitle}: ${ru.analyses.categories[c]}`} className="mt-1 mb-2 space-y-1 pl-6">
                    {(rulesByCategory.get(c) ?? []).map((r) => (
                      <li key={r.id}>
                        <label
                          className={`flex items-start gap-2 text-sm ${categories.has(c) ? '' : 'text-slate-400'}`}
                          title={r.description}
                        >
                          <input
                            type="checkbox"
                            className="mt-1"
                            checked={ruleSelected(r)}
                            disabled={!categories.has(c)}
                            onChange={() => toggleRule(r)}
                          />
                          <span>
                            <span className="font-mono text-xs">{r.code}</span> {r.title}
                            <span className="block text-xs text-slate-500">
                              {t.ruleKinds[r.kind]} · <span className="line-clamp-2 inline">{r.description}</span>
                            </span>
                          </span>
                        </label>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
          {rules.isError && <p className="text-sm text-amber-900">{t.rulesLoadError}</p>}
          {rulesByCategory.size > 0 && (
            <p className={`text-sm ${rulesOverLimit ? 'text-red-800' : 'text-slate-700'}`} aria-live="polite">
              {t.rulesSelected}: {selectedRules.length} {t.rulesOf} {rulesLimit}
              {rulesOverLimit && ` — ${t.rulesOverLimit}`}
            </p>
          )}
        </fieldset>

        <fieldset className={`space-y-2 ${groupCardClass}`}>
          <legend className="text-sm font-semibold">{t.depth}</legend>
          {DEPTHS.map((d) => (
            <label key={d} className="flex items-start gap-2 text-sm">
              <input
                type="radio"
                name="depth"
                value={d}
                checked={depth === d}
                onChange={() => {
                  setDepth(d)
                  resetEstimate()
                }}
              />
              <span>
                {ru.analyses.depth[d]} — <span className="text-slate-600">{ru.analyses.depthHint[d]}</span>
              </span>
            </label>
          ))}
        </fieldset>

        <fieldset className={`space-y-2 ${groupCardClass}`}>
          <legend className="text-sm font-semibold">{t.model}</legend>
          <ModelSelect
            value={model}
            onChange={(v) => {
              setModel(v)
              if (v?.modelId !== model?.modelId) resetEstimate()
            }}
          />
        </fieldset>

        {errors.length > 0 && (
          <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900">
            <ul className="list-disc pl-5">
              {errors.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          </div>
        )}
        {estimate && <EstimateView estimate={estimate} />}
        {estimateMutation.isError && <ErrorNotice error={estimateMutation.error} />}
        {submit.isError && <ErrorNotice error={submit.error} />}
        {selectedModel?.is_cloud && model?.cloudConsent && <Notice tone="warning">{t.cloudTransfer}</Notice>}
        {isCloud && <p className="text-sm text-slate-700">{t.redactionHint}</p>}

        <div className="flex flex-wrap gap-2">
          <Button
            disabled={estimateMutation.isPending}
            onClick={() => {
              const body = request()
              if (body) estimateMutation.mutate(body)
            }}
          >
            {estimateMutation.isPending ? t.estimating : t.estimate}
          </Button>
          <Button
            type="submit"
            variant="primary"
            disabled={submit.isPending || rulesOverLimit || (estimate !== null && !estimate.within_limit)}
          >
            {submit.isPending ? t.submitting : isCloud ? t.checkRedaction : t.submit}
          </Button>
        </div>
      </form>
    </section>
  )
}
