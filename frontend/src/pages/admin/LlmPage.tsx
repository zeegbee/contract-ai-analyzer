import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'
import { Link } from 'react-router'

import {
  CATALOG_KEY,
  LLM_SETTINGS_KEY,
  checkProvider,
  createModel,
  createProvider,
  deleteProvider,
  fetchLlmSettings,
  fetchModels,
  fetchProviders,
  fetchRemoteModels,
  type Health,
  importModels,
  type Model,
  MODELS_KEY,
  type Provider,
  type ProviderInput,
  PROVIDERS_KEY,
  type ProviderType,
  updateLlmSettings,
  updateProvider,
} from '../../api/catalog'
import { useCurrentUser } from '../../app/session'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Button, Field, inputClass, Notice, PageTitle } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatDateTime } from '../../lib/format'
import { pricingText } from '../../lib/pricing'

const t = ru.llm
const SETTINGS_KEY = LLM_SETTINGS_KEY

function Badge({ children, tone = 'slate' }: { children: string; tone?: 'slate' | 'amber' | 'green' | 'red' }) {
  const tones = {
    slate: 'bg-slate-100 text-slate-800',
    amber: 'bg-amber-100 text-amber-900',
    green: 'bg-green-100 text-green-900',
    red: 'bg-red-100 text-red-900',
  }
  return <span className={`rounded px-1.5 py-0.5 text-xs ${tones[tone]}`}>{children}</span>
}

// ------------------------------------------------------------------ облако

function CloudSettingsPanel() {
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: SETTINGS_KEY, queryFn: ({ signal }) => fetchLlmSettings(signal) })
  const [text, setText] = useState<string | null>(null)
  const mutation = useMutation({
    mutationFn: updateLlmSettings,
    onSuccess: (data) => {
      queryClient.setQueryData(SETTINGS_KEY, data)
      setText(null)
    },
  })
  if (query.isPending) return <p>{ru.common.loading}</p>
  if (query.isError) return <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />
  const s = query.data
  const noticeText = text ?? s.cloud_notice.text
  return (
    <section aria-labelledby="cloud-title" className="space-y-3 rounded-lg border border-slate-300 bg-white shadow-sm p-4">
      <h2 id="cloud-title" className="text-lg font-semibold">
        {t.cloud.title}
      </h2>
      {mutation.isError && <ErrorNotice error={mutation.error} />}
      <Notice tone={s.cloud_models_enabled ? 'warning' : 'info'}>
        {s.cloud_models_enabled ? t.cloud.enabled : t.cloud.disabled}
      </Notice>
      {!s.can_edit && <p className="text-sm text-slate-600">{t.cloud.readOnly}</p>}
      {s.can_edit && (
        <Button
          variant={s.cloud_models_enabled ? 'danger' : 'secondary'}
          disabled={mutation.isPending}
          onClick={() => mutation.mutate({ cloud_models_enabled: !s.cloud_models_enabled })}
        >
          {s.cloud_models_enabled ? t.cloud.disable : t.cloud.enable}
        </Button>
      )}
      <Field id="cloud-notice" label={t.cloud.noticeTitle} hint={`${t.cloud.noticeVersion(s.cloud_notice.version)}. ${t.cloud.noticeHint}`}>
        <textarea
          id="cloud-notice"
          className={`${inputClass} min-h-28`}
          value={noticeText}
          readOnly={!s.can_edit}
          maxLength={4000}
          onChange={(e) => setText(e.target.value)}
        />
      </Field>
      {s.can_edit && (
        <Button
          disabled={mutation.isPending || text === null || text.trim().length < 20 || text === s.cloud_notice.text}
          onClick={() => mutation.mutate({ cloud_notice_text: noticeText })}
        >
          {t.cloud.saveNotice}
        </Button>
      )}
    </section>
  )
}

// ------------------------------------------------------------------ провайдеры

interface ProviderDraft {
  name: string
  type: ProviderType
  base_url: string
  api_key: string
  clear_api_key: boolean
  allow_private_network: boolean
  is_cloud: boolean
  enabled: boolean
  timeout_s: string
  max_concurrency: string
}

function draftFrom(p?: Provider): ProviderDraft {
  return {
    name: p?.name ?? '',
    type: p?.type ?? 'openrouter',
    base_url: p?.base_url ?? '',
    api_key: '',
    clear_api_key: false,
    allow_private_network: p?.allow_private_network ?? false,
    is_cloud: p?.is_cloud ?? true,
    enabled: p?.enabled ?? false,
    timeout_s: String(p?.timeout_s ?? 120),
    max_concurrency: String(p?.max_concurrency ?? 2),
  }
}

function ProviderForm({ provider, onDone }: { provider?: Provider; onDone: (p: Provider, secretReset: boolean) => void }) {
  const [d, setD] = useState<ProviderDraft>(() => draftFrom(provider))
  const set = <K extends keyof ProviderDraft>(key: K, value: ProviderDraft[K]) => setD((prev) => ({ ...prev, [key]: value }))
  const mutation = useMutation({
    mutationFn: async () => {
      const body: ProviderInput = {
        name: d.name.trim(),
        enabled: d.enabled,
        allow_private_network: d.type === 'ollama' && d.allow_private_network,
        timeout_s: Number(d.timeout_s),
        max_concurrency: Number(d.max_concurrency),
        ...(d.base_url.trim() ? { base_url: d.base_url.trim() } : {}),
        ...(d.api_key.trim() ? { api_key: d.api_key.trim() } : {}),
        ...(d.type === 'ollama' && d.allow_private_network ? { is_cloud: d.is_cloud } : {}),
      }
      if (provider) {
        const updated = await updateProvider(provider.id, {
          ...body,
          version: provider.version,
          ...(d.clear_api_key ? { clear_api_key: true } : {}),
        })
        return { provider: updated, secretReset: provider.has_secret && !updated.has_secret && !d.clear_api_key }
      }
      return { provider: await createProvider({ ...body, type: d.type }), secretReset: false }
    },
    onSuccess: ({ provider: saved, secretReset }) => onDone(saved, secretReset),
  })
  const submit = (e: FormEvent) => {
    e.preventDefault()
    mutation.mutate()
  }
  const prefix = provider ? `provider-${provider.id}` : 'provider-new'
  return (
    <form className="space-y-3" onSubmit={submit} aria-label={provider ? `${t.providers.edit}: ${provider.name}` : t.providers.add}>
      {mutation.isError && <ErrorNotice error={mutation.error} />}
      <div className="grid gap-3 sm:grid-cols-2">
        <Field id={`${prefix}-name`} label={t.providers.name}>
          <input id={`${prefix}-name`} className={inputClass} value={d.name} maxLength={100} required onChange={(e) => set('name', e.target.value)} />
        </Field>
        <Field id={`${prefix}-type`} label={t.providers.type}>
          <select
            id={`${prefix}-type`}
            className={inputClass}
            value={d.type}
            disabled={Boolean(provider)}
            onChange={(e) => set('type', e.target.value as ProviderType)}
          >
            <option value="openrouter">OpenRouter</option>
            <option value="ollama">Ollama</option>
          </select>
        </Field>
        <Field
          id={`${prefix}-url`}
          label={t.providers.baseUrl}
          hint={d.type === 'ollama' ? t.providers.baseUrlHintOllama : t.providers.baseUrlHintOpenRouter}
        >
          <input
            id={`${prefix}-url`}
            className={inputClass}
            value={d.base_url}
            maxLength={500}
            placeholder={d.type === 'ollama' ? 'http://ollama:11434' : 'https://openrouter.ai/api/v1'}
            onChange={(e) => set('base_url', e.target.value)}
          />
        </Field>
        <Field id={`${prefix}-key`} label={t.providers.apiKey} hint={t.providers.apiKeyHint}>
          <input
            id={`${prefix}-key`}
            className={inputClass}
            type="password"
            autoComplete="off"
            value={d.api_key}
            maxLength={500}
            onChange={(e) => set('api_key', e.target.value)}
          />
        </Field>
        <Field id={`${prefix}-timeout`} label={t.providers.timeout}>
          <input id={`${prefix}-timeout`} className={inputClass} type="number" min={5} max={900} value={d.timeout_s} onChange={(e) => set('timeout_s', e.target.value)} />
        </Field>
        <Field id={`${prefix}-concurrency`} label={t.providers.concurrency}>
          <input
            id={`${prefix}-concurrency`}
            className={inputClass}
            type="number"
            min={1}
            max={32}
            value={d.max_concurrency}
            onChange={(e) => set('max_concurrency', e.target.value)}
          />
        </Field>
      </div>
      <div className="space-y-2 text-sm">
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={d.enabled} onChange={(e) => set('enabled', e.target.checked)} />
          {t.providers.enabled}
        </label>
        {provider?.has_secret && (
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={d.clear_api_key} onChange={(e) => set('clear_api_key', e.target.checked)} />
            {t.providers.clearKey}
          </label>
        )}
        {d.type === 'ollama' && (
          <>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={d.allow_private_network}
                onChange={(e) => {
                  set('allow_private_network', e.target.checked)
                  if (e.target.checked && !provider?.allow_private_network) set('is_cloud', false)
                }}
              />
              {t.providers.privateNetwork}
            </label>
            <p className="text-xs text-slate-600">{t.providers.privateNetworkHint}</p>
            {d.allow_private_network && (
              <>
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={d.is_cloud} onChange={(e) => set('is_cloud', e.target.checked)} />
                  {t.providers.isCloud}
                </label>
                <p className="text-xs text-slate-600">{t.providers.isCloudHint}</p>
              </>
            )}
          </>
        )}
      </div>
      <Button type="submit" variant="primary" disabled={mutation.isPending || !d.name.trim()}>
        {ru.common.save}
      </Button>
    </form>
  )
}

function RemoteModelsPanel({ provider, onImported }: { provider: Provider; onImported: (n: number) => void }) {
  const [q, setQ] = useState('')
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const query = useQuery({
    queryKey: [...CATALOG_KEY, 'remote', provider.id, search],
    queryFn: ({ signal }) => fetchRemoteModels(provider.id, search, signal),
  })
  const mutation = useMutation({
    mutationFn: () => importModels(provider.id, [...selected]),
    onSuccess: (items) => {
      setSelected(new Set())
      onImported(items.length)
    },
  })
  return (
    <div className="space-y-3 border-t border-slate-200 pt-3">
      <h4 className="font-medium">{t.remote.title}</h4>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          setSearch(q.trim())
        }}
      >
        <input aria-label={t.remote.search} className={inputClass} value={q} maxLength={100} onChange={(e) => setQ(e.target.value)} />
        <Button type="submit">{t.remote.search}</Button>
      </form>
      {query.isPending && <p className="text-sm">{t.remote.loading}</p>}
      {query.isError && <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />}
      {mutation.isError && <ErrorNotice error={mutation.error} />}
      {query.data && (
        <ul className="max-h-80 divide-y divide-slate-100 overflow-y-auto rounded border border-slate-200 text-sm">
          {query.data.map((m) => (
            <li key={m.external_model_id} className="flex items-start gap-2 p-2">
              <input
                type="checkbox"
                aria-label={m.external_model_id}
                disabled={m.already_added}
                checked={selected.has(m.external_model_id)}
                onChange={(e) =>
                  setSelected((prev) => {
                    const next = new Set(prev)
                    if (e.target.checked) next.add(m.external_model_id)
                    else next.delete(m.external_model_id)
                    return next
                  })
                }
              />
              <div>
                <p>
                  <span className="font-medium">{m.display_name}</span> <code className="text-xs">{m.external_model_id}</code>
                  {m.already_added && <span className="text-slate-600"> · {t.remote.added}</span>}
                </p>
                <p className="text-xs text-slate-600">
                  {t.remote.context}: {m.context_window ?? ru.common.unknown} ·{' '}
                  {m.prompt_price_per_mtok !== null && m.completion_price_per_mtok !== null
                    ? `${t.remote.price}: ${m.prompt_price_per_mtok} / ${m.completion_price_per_mtok} USD`
                    : t.remote.priceUnknown}
                  {m.advertises_structured_output && ` · ${t.remote.structured}`}
                </p>
              </div>
            </li>
          ))}
        </ul>
      )}
      <Button variant="primary" disabled={selected.size === 0 || mutation.isPending} onClick={() => mutation.mutate()}>
        {t.remote.add}
      </Button>
    </div>
  )
}

function HealthNotice({ health }: { health: Health }) {
  return (
    <Notice tone={health.ok ? 'success' : 'warning'}>
      {health.message}
      {!health.ok && <span className="text-xs"> ({health.status})</span>}
    </Notice>
  )
}

function ProviderCard({ provider }: { provider: Provider }) {
  const me = useCurrentUser()
  const queryClient = useQueryClient()
  const [mode, setMode] = useState<'view' | 'edit' | 'import'>('view')
  const [message, setMessage] = useState<string | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const refresh = () => void queryClient.invalidateQueries({ queryKey: CATALOG_KEY })
  const check = useMutation({
    mutationFn: () => checkProvider(provider.id),
    onSuccess: (result) => {
      setHealth(result.health)
      refresh()
    },
  })
  const remove = useMutation({ mutationFn: () => deleteProvider(provider.id), onSuccess: refresh })
  return (
    <li className="space-y-3 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <p className="font-medium">
            {provider.name}{' '}
            <Badge tone={provider.is_cloud ? 'amber' : 'green'}>
              {provider.is_cloud ? t.providers.cloudBadge : t.providers.localBadge}
            </Badge>{' '}
            {!provider.enabled && <Badge tone="red">{t.providers.offBadge}</Badge>}
          </p>
          <p className="text-sm text-slate-700">
            {provider.type === 'openrouter' ? 'OpenRouter' : 'Ollama'} · <code>{provider.base_url}</code>
          </p>
          <p className="text-xs text-slate-600">
            {provider.has_secret ? t.providers.apiKeySet(provider.secret_last4) : t.providers.apiKeyMissing} ·{' '}
            {t.providers.modelsCount(provider.model_count)} · {t.providers.lastCheck}:{' '}
            {provider.last_check_at
              ? `${formatDateTime(provider.last_check_at)} — ${provider.last_check_status ?? ''}`
              : t.providers.neverChecked}
          </p>
          {provider.last_check_error_safe && <p className="text-xs text-red-800">{provider.last_check_error_safe}</p>}
          {provider.url_warnings.includes('loopback_in_container') && (
            <p className="text-xs text-amber-900">{t.providers.loopbackWarning}</p>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button disabled={check.isPending} onClick={() => check.mutate()}>
            {check.isPending ? t.providers.checking : t.providers.check}
          </Button>
          <Button onClick={() => setMode(mode === 'import' ? 'view' : 'import')}>{t.providers.importModels}</Button>
          {me.capabilities.manage_providers && (
            <>
              <Button onClick={() => setMode(mode === 'edit' ? 'view' : 'edit')}>{t.providers.edit}</Button>
              <Button
                variant="danger"
                disabled={remove.isPending}
                onClick={() => {
                  if (window.confirm(t.providers.confirmDelete)) remove.mutate()
                }}
              >
                {t.providers.delete}
              </Button>
            </>
          )}
        </div>
      </div>
      {check.isError && <ErrorNotice error={check.error} />}
      {remove.isError && <ErrorNotice error={remove.error} />}
      {health && <HealthNotice health={health} />}
      {message && <Notice tone="success">{message}</Notice>}
      {mode === 'edit' && (
        <ProviderForm
          provider={provider}
          onDone={(_saved, secretReset) => {
            setMode('view')
            setMessage(secretReset ? t.providers.secretReset : ru.users.detail.saved)
            refresh()
          }}
        />
      )}
      {mode === 'import' && (
        <RemoteModelsPanel
          provider={provider}
          onImported={(n) => {
            setMessage(t.remote.imported(n))
            refresh()
          }}
        />
      )}
    </li>
  )
}

function ProvidersPanel() {
  const me = useCurrentUser()
  const queryClient = useQueryClient()
  const [adding, setAdding] = useState(false)
  const query = useQuery({ queryKey: PROVIDERS_KEY, queryFn: ({ signal }) => fetchProviders(signal) })
  return (
    <section aria-labelledby="providers-title" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="providers-title" className="text-lg font-semibold">
          {t.providers.title}
        </h2>
        {me.capabilities.manage_providers && (
          <Button variant="primary" onClick={() => setAdding(!adding)}>
            {t.providers.add}
          </Button>
        )}
      </div>
      {adding && (
        <div className="rounded-lg border border-slate-300 bg-white shadow-sm p-4">
          <ProviderForm
            onDone={() => {
              setAdding(false)
              void queryClient.invalidateQueries({ queryKey: CATALOG_KEY })
            }}
          />
        </div>
      )}
      {query.isPending && <p>{ru.common.loading}</p>}
      {query.isError && <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />}
      {query.data?.length === 0 && <p className="text-sm text-slate-600">{t.providers.empty}</p>}
      {query.data && query.data.length > 0 && (
        <ul className="divide-y divide-slate-200 rounded-lg border border-slate-300 bg-white shadow-sm">
          {query.data.map((p) => (
            <ProviderCard key={p.id} provider={p} />
          ))}
        </ul>
      )}
    </section>
  )
}

// ------------------------------------------------------------------ модели

function AddModelForm({ providers, onDone }: { providers: Provider[]; onDone: () => void }) {
  const [providerId, setProviderId] = useState(providers[0]?.id ?? '')
  const [externalId, setExternalId] = useState('')
  const [name, setName] = useState('')
  const mutation = useMutation({
    mutationFn: () =>
      createModel({
        provider_config_id: providerId,
        external_model_id: externalId.trim(),
        display_name: name.trim() || externalId.trim(),
      }),
    onSuccess: onDone,
  })
  return (
    <form
      className="space-y-3 rounded-lg border border-slate-300 bg-white shadow-sm p-4"
      aria-label={t.models.add}
      onSubmit={(e) => {
        e.preventDefault()
        mutation.mutate()
      }}
    >
      {mutation.isError && <ErrorNotice error={mutation.error} />}
      <div className="grid gap-3 sm:grid-cols-3">
        <Field id="model-new-provider" label={t.models.provider}>
          <select id="model-new-provider" className={inputClass} value={providerId} onChange={(e) => setProviderId(e.target.value)}>
            {providers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </Field>
        <Field id="model-new-external" label={t.models.externalId}>
          <input id="model-new-external" className={inputClass} required maxLength={200} value={externalId} onChange={(e) => setExternalId(e.target.value)} />
        </Field>
        <Field id="model-new-name" label={t.models.displayName}>
          <input id="model-new-name" className={inputClass} maxLength={200} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
      </div>
      <Button type="submit" variant="primary" disabled={mutation.isPending || !providerId || !externalId.trim()}>
        {ru.common.save}
      </Button>
    </form>
  )
}

function ModelRow({ model }: { model: Model }) {
  return (
    <tr className="border-t border-slate-100 align-top">
      <td className="p-2">
        <Link to={`/admin/models/${model.id}`} className="font-medium text-blue-800 hover:underline">
          {model.display_name}
        </Link>
        {model.is_default && (
          <>
            {' '}
            <Badge tone="green">{t.models.default}</Badge>
          </>
        )}
        <p className="text-xs text-slate-600">
          <code>{model.external_model_id}</code>
        </p>
      </td>
      <td className="p-2">
        {model.provider_name}{' '}
        {model.is_cloud && <Badge tone="amber">{t.providers.cloudBadge}</Badge>}
      </td>
      <td className="p-2">{model.enabled && model.provider_enabled ? ru.common.yes : ru.common.no}</td>
      <td className="p-2">{model.purpose.map((p) => t.models.purposes[p] ?? p).join(', ')}</td>
      <td className="p-2">{t.models.structuredValues[model.structured_output]}</td>
      <td className="p-2">{pricingText(model.pricing)}</td>
      <td className="p-2 text-xs">
        {model.last_tested_at ? `${formatDateTime(model.last_tested_at)} — ${model.last_test_status ?? ''}` : t.providers.neverChecked}
      </td>
    </tr>
  )
}

function ModelsPanel() {
  const queryClient = useQueryClient()
  const [adding, setAdding] = useState(false)
  const models = useQuery({ queryKey: MODELS_KEY, queryFn: ({ signal }) => fetchModels(signal) })
  const providers = useQuery({ queryKey: PROVIDERS_KEY, queryFn: ({ signal }) => fetchProviders(signal) })
  return (
    <section aria-labelledby="models-title" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="models-title" className="text-lg font-semibold">
          {t.models.title}
        </h2>
        <Button variant="primary" disabled={!providers.data?.length} onClick={() => setAdding(!adding)}>
          {t.models.add}
        </Button>
      </div>
      {adding && providers.data && (
        <AddModelForm
          providers={providers.data}
          onDone={() => {
            setAdding(false)
            void queryClient.invalidateQueries({ queryKey: CATALOG_KEY })
          }}
        />
      )}
      {models.isPending && <p>{ru.common.loading}</p>}
      {models.isError && <ErrorNotice error={models.error} onRetry={() => void models.refetch()} />}
      {models.data?.length === 0 && <p className="text-sm text-slate-600">{t.models.empty}</p>}
      {models.data && models.data.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-300 bg-white shadow-sm">
          <table className="w-full text-left text-sm">
            <thead className="bg-slate-50 text-slate-700">
              <tr>
                <th className="p-2">{t.models.displayName}</th>
                <th className="p-2">{t.models.provider}</th>
                <th className="p-2">{t.models.enabled}</th>
                <th className="p-2">{t.models.purpose}</th>
                <th className="p-2">{t.models.structured}</th>
                <th className="p-2">{t.models.pricing}</th>
                <th className="p-2">{t.models.lastTest}</th>
              </tr>
            </thead>
            <tbody>
              {models.data.map((m) => (
                <ModelRow key={m.id} model={m} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

export function LlmPage() {
  return (
    <section aria-labelledby="llm-title" className="space-y-6">
      <PageTitle id="llm-title">{t.title}</PageTitle>
      <CloudSettingsPanel />
      <ProvidersPanel />
      <ModelsPanel />
    </section>
  )
}
