import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'

import {
  CATALOG_KEY,
  deleteModel,
  fetchModel,
  fetchRevisions,
  type Model,
  type ModelInput,
  type ModelTest,
  PURPOSES,
  type Purpose,
  type StructuredOutput,
  testModel,
  updateModel,
} from '../../api/catalog'
import { ApiError } from '../../api/client'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Button, Field, inputClass, Notice, PageTitle } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatDateTime } from '../../lib/format'
import { pricingText } from '../../lib/pricing'

const t = ru.llm.models
const ROLES = ['superadmin', 'admin', 'analyst'] as const

type PricingMode = 'keep' | 'unknown' | 'manual'

interface Draft {
  display_name: string
  enabled: boolean
  is_default: boolean
  purpose: Purpose[]
  priority: string
  allowed_roles: string[]
  context_window: string
  max_output_tokens: string
  temperature: string
  top_p: string
  seed: string
  num_ctx: string
  reasoning: 'on' | 'off'
  structured_output: StructuredOutput
  pricing_mode: PricingMode
  prompt_price: string
  completion_price: string
  currency: string
  notes: string
}

const str = (v: unknown) => (typeof v === 'number' ? String(v) : '')
const num = (v: string) => (v.trim() === '' ? undefined : Number(v))

function draftFrom(m: Model): Draft {
  const p = m.generation_params
  return {
    display_name: m.display_name,
    enabled: m.enabled,
    is_default: m.is_default,
    purpose: m.purpose.filter((x): x is Purpose => (PURPOSES as readonly string[]).includes(x)),
    priority: String(m.priority),
    allowed_roles: m.allowed_roles,
    context_window: str(m.context_window),
    max_output_tokens: str(m.max_output_tokens),
    temperature: str(p.temperature),
    top_p: str(p.top_p),
    seed: str(p.seed),
    num_ctx: str(p.num_ctx),
    reasoning: p.reasoning === 'off' ? 'off' : 'on',
    structured_output: m.structured_output,
    pricing_mode: 'keep',
    prompt_price: m.pricing?.prompt_per_mtok ?? '',
    completion_price: m.pricing?.completion_per_mtok ?? '',
    currency: m.pricing?.currency ?? 'USD',
    notes: m.notes ?? '',
  }
}

function toInput(d: Draft, m: Model): ModelInput & { version: number } {
  const params: ModelInput['generation_params'] = {}
  const temperature = num(d.temperature)
  const topP = num(d.top_p)
  const seed = num(d.seed)
  const numCtx = num(d.num_ctx)
  if (temperature !== undefined) params.temperature = temperature
  if (topP !== undefined) params.top_p = topP
  if (seed !== undefined) params.seed = seed
  if (numCtx !== undefined) params.num_ctx = numCtx
  if (d.reasoning === 'off') params.reasoning = 'off'
  return {
    version: m.version,
    display_name: d.display_name.trim(),
    enabled: d.enabled,
    is_default: d.is_default,
    purpose: d.purpose,
    priority: Number(d.priority),
    allowed_roles: d.allowed_roles,
    context_window: num(d.context_window) ?? null,
    max_output_tokens: num(d.max_output_tokens) ?? null,
    generation_params: params,
    // json_schema выставляет только проверка; вручную его не отправляем.
    ...(d.structured_output !== m.structured_output && d.structured_output !== 'json_schema'
      ? { structured_output: d.structured_output }
      : {}),
    ...(d.pricing_mode === 'unknown' ? { pricing: null } : {}),
    ...(d.pricing_mode === 'manual'
      ? { pricing: { prompt_per_mtok: d.prompt_price, completion_per_mtok: d.completion_price, currency: d.currency } }
      : {}),
    notes: d.notes.trim() || null,
  }
}

function toggle<T>(list: T[], value: T, on: boolean): T[] {
  return on ? [...new Set([...list, value])] : list.filter((x) => x !== value)
}

function ModelForm({ model, onSaved }: { model: Model; onSaved: (m: Model) => void }) {
  const [d, setD] = useState<Draft>(() => draftFrom(model))
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setD((prev) => ({ ...prev, [key]: value }))
  const mutation = useMutation({ mutationFn: () => updateModel(model.id, toInput(d, model)), onSuccess: onSaved })
  const submit = (e: FormEvent) => {
    e.preventDefault()
    mutation.mutate()
  }
  const numberField = (key: keyof Draft, label: string, props: { min?: number; max?: number; step?: string } = {}) => (
    <Field id={`model-${key}`} label={label}>
      <input
        id={`model-${key}`}
        className={inputClass}
        type="number"
        value={d[key] as string}
        onChange={(e) => set(key, e.target.value as never)}
        {...props}
      />
    </Field>
  )
  return (
    <form className="space-y-4 rounded-lg border border-slate-300 bg-white shadow-sm p-4" onSubmit={submit}>
      {mutation.isError && <ErrorNotice error={mutation.error} />}
      {mutation.isSuccess && <Notice tone="success">{ru.users.detail.saved}</Notice>}
      <div className="grid gap-3 sm:grid-cols-2">
        <Field id="model-display_name" label={t.displayName}>
          <input
            id="model-display_name"
            className={inputClass}
            value={d.display_name}
            maxLength={200}
            required
            onChange={(e) => set('display_name', e.target.value)}
          />
        </Field>
        {numberField('priority', t.priority, { min: 0, max: 10000 })}
        {numberField('context_window', t.contextWindow, { min: 512 })}
        {numberField('max_output_tokens', t.maxOutput, { min: 16 })}
        {numberField('temperature', t.temperature, { min: 0, max: 2, step: '0.01' })}
        {numberField('top_p', t.topP, { min: 0, max: 1, step: '0.01' })}
        {numberField('seed', t.seed, { min: 0 })}
        {model.provider_type === 'ollama' && numberField('num_ctx', t.numCtx, { min: 512 })}
        <Field id="model-reasoning" label={t.reasoning} hint={t.reasoningHint}>
          <select
            id="model-reasoning"
            className={inputClass}
            value={d.reasoning}
            onChange={(e) => set('reasoning', e.target.value as Draft['reasoning'])}
          >
            <option value="on">{t.reasoningOn}</option>
            <option value="off">{t.reasoningOff}</option>
          </select>
        </Field>
        <Field id="model-structured" label={t.structured} hint={t.structuredHint}>
          <select
            id="model-structured"
            className={inputClass}
            value={d.structured_output}
            onChange={(e) => set('structured_output', e.target.value as StructuredOutput)}
          >
            {(['json_schema', 'json_mode', 'none', 'unknown'] as const).map((v) => (
              <option key={v} value={v} disabled={v === 'json_schema' && model.structured_output !== 'json_schema'}>
                {t.structuredValues[v]}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <fieldset className="space-y-1 text-sm">
        <legend className="font-medium">{t.purpose}</legend>
        {PURPOSES.map((p) => (
          <label key={p} className="mr-4 inline-flex items-center gap-2">
            <input type="checkbox" checked={d.purpose.includes(p)} onChange={(e) => set('purpose', toggle(d.purpose, p, e.target.checked))} />
            {t.purposes[p]}
          </label>
        ))}
      </fieldset>
      <fieldset className="space-y-1 text-sm">
        <legend className="font-medium">{t.roles}</legend>
        {ROLES.map((r) => (
          <label key={r} className="mr-4 inline-flex items-center gap-2">
            <input
              type="checkbox"
              checked={d.allowed_roles.includes(r)}
              onChange={(e) => set('allowed_roles', toggle(d.allowed_roles, r, e.target.checked))}
            />
            {ru.roles[r]}
          </label>
        ))}
      </fieldset>
      <div className="space-y-1 text-sm">
        <label className="mr-4 inline-flex items-center gap-2">
          <input type="checkbox" checked={d.enabled} onChange={(e) => set('enabled', e.target.checked)} />
          {t.enabled}
        </label>
        <label className="inline-flex items-center gap-2">
          <input type="checkbox" checked={d.is_default} onChange={(e) => set('is_default', e.target.checked)} />
          {t.makeDefault}
        </label>
      </div>
      <fieldset className="space-y-2 text-sm">
        <legend className="font-medium">
          {t.pricing}: {pricingText(model.pricing)}
        </legend>
        <label className="mr-4 inline-flex items-center gap-2">
          <input type="radio" name="pricing" checked={d.pricing_mode === 'keep'} onChange={() => set('pricing_mode', 'keep')} />
          {t.keepPricing}
        </label>
        <label className="mr-4 inline-flex items-center gap-2">
          <input type="radio" name="pricing" checked={d.pricing_mode === 'unknown'} onChange={() => set('pricing_mode', 'unknown')} />
          {t.clearPricing}
        </label>
        <label className="inline-flex items-center gap-2">
          <input type="radio" name="pricing" checked={d.pricing_mode === 'manual'} onChange={() => set('pricing_mode', 'manual')} />
          {t.pricingManual}
        </label>
        {d.pricing_mode === 'manual' && (
          <div className="grid gap-3 sm:grid-cols-3">
            {numberField('prompt_price', t.promptPrice, { min: 0, step: '0.000001' })}
            {numberField('completion_price', t.completionPrice, { min: 0, step: '0.000001' })}
            <Field id="model-currency" label={t.currency}>
              <select id="model-currency" className={inputClass} value={d.currency} onChange={(e) => set('currency', e.target.value)}>
                <option value="USD">USD</option>
                <option value="RUB">RUB</option>
                <option value="EUR">EUR</option>
              </select>
            </Field>
          </div>
        )}
      </fieldset>
      <Field id="model-notes" label={t.notes}>
        <textarea id="model-notes" className={inputClass} maxLength={2000} value={d.notes} onChange={(e) => set('notes', e.target.value)} />
      </Field>
      <Button type="submit" variant="primary" disabled={mutation.isPending || !d.display_name.trim() || d.purpose.length === 0}>
        {ru.common.save}
      </Button>
    </form>
  )
}

function TestResult({ result }: { result: ModelTest }) {
  return (
    <div className="space-y-2">
      <Notice tone={result.ok ? 'success' : 'warning'}>{result.ok ? t.testOk : t.testFailed}</Notice>
      <ul className="list-disc pl-5 text-sm">
        {result.steps.map((s) => (
          <li key={s.name}>
            {s.ok ? '✓' : '✗'} {s.message} <span className="text-xs text-slate-600">({s.code})</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function Revisions({ modelId }: { modelId: string }) {
  const query = useQuery({
    queryKey: [...CATALOG_KEY, 'revisions', modelId],
    queryFn: ({ signal }) => fetchRevisions('model', modelId, signal),
  })
  return (
    <section aria-labelledby="revisions-title" className="space-y-2">
      <h2 id="revisions-title" className="text-lg font-semibold">
        {t.history}
      </h2>
      {query.isError && <ErrorNotice error={query.error} />}
      {query.data?.length === 0 && <p className="text-sm text-slate-600">{t.noHistory}</p>}
      <ul className="space-y-2 text-sm">
        {query.data?.map((r) => (
          <li key={r.id} className="rounded border border-slate-200 bg-white p-2">
            <p className="text-xs text-slate-600">
              #{r.revision} · {formatDateTime(r.created_at)}
            </p>
            <p className="break-all font-mono text-xs">{Object.keys(r.diff).join(', ')}</p>
          </li>
        ))}
      </ul>
    </section>
  )
}

export function ModelPage() {
  const { modelId = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const key = [...CATALOG_KEY, 'model', modelId]
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => fetchModel(modelId, signal) })
  const [result, setResult] = useState<ModelTest | null>(null)
  const [formKey, setFormKey] = useState(0)
  const saved = (m: Model) => {
    queryClient.setQueryData(key, m)
    setFormKey((k) => k + 1)
    void queryClient.invalidateQueries({ queryKey: CATALOG_KEY })
  }
  const test = useMutation({
    mutationFn: (generate: boolean) => testModel(modelId, generate),
    onSuccess: (r) => {
      setResult(r)
      saved(r.model)
    },
  })
  const remove = useMutation({
    mutationFn: () => deleteModel(modelId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: CATALOG_KEY })
      void navigate('/admin/models')
    },
  })

  if (query.isPending) return <p>{ru.common.loading}</p>
  if (query.isError) {
    if (query.error instanceof ApiError && query.error.status === 404) return <p role="alert">{t.notFound}</p>
    return <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />
  }
  const model = query.data
  return (
    <section aria-labelledby="model-title" className="space-y-6">
      <Link to="/admin/models" className="text-sm text-blue-800 hover:underline">
        ← {ru.llm.title}
      </Link>
      <PageTitle id="model-title">{model.display_name}</PageTitle>
      <dl className="grid max-w-2xl grid-cols-[max-content_1fr] gap-x-6 gap-y-1 rounded-lg border border-slate-300 bg-white shadow-sm p-4 text-sm">
        <dt className="text-slate-600">{t.provider}</dt>
        <dd>
          {model.provider_name} {model.is_cloud && `(${ru.llm.providers.cloudBadge})`}
        </dd>
        <dt className="text-slate-600">{t.externalId}</dt>
        <dd>
          <code>{model.external_model_id}</code>
        </dd>
        <dt className="text-slate-600">{t.structured}</dt>
        <dd>{t.structuredValues[model.structured_output]}</dd>
        <dt className="text-slate-600">{t.lastTest}</dt>
        <dd>
          {model.last_tested_at
            ? `${formatDateTime(model.last_tested_at)} — ${model.last_test_status ?? ''}`
            : ru.llm.providers.neverChecked}
          {model.last_test_error_safe && <span className="block text-red-800">{model.last_test_error_safe}</span>}
        </dd>
      </dl>
      <div className="space-y-2">
        <div className="flex flex-wrap gap-2">
          <Button disabled={test.isPending} onClick={() => test.mutate(false)}>
            {t.test}
          </Button>
          <Button disabled={test.isPending} onClick={() => test.mutate(true)}>
            {t.testGenerate}
          </Button>
        </div>
        <p className="text-xs text-slate-600">{t.testGenerateHint}</p>
        {test.isError && <ErrorNotice error={test.error} />}
        {result && <TestResult result={result} />}
      </div>
      <ModelForm key={formKey} model={model} onSaved={saved} />
      <Revisions modelId={model.id} />
      {remove.isError && <ErrorNotice error={remove.error} />}
      <Button
        variant="danger"
        disabled={remove.isPending}
        onClick={() => {
          if (window.confirm(t.confirmDelete)) remove.mutate()
        }}
      >
        {t.delete}
      </Button>
    </section>
  )
}
