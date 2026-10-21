import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ReactNode, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'

import type { AvailableModel } from '../../api/catalog'
import { type Fragment, fetchFragments } from '../../api/documents'
import {
  type ConfirmBody,
  confirmPreview,
  createPreview,
  type EditOp,
  editPreview,
  type Entity,
  fetchPreview,
  fetchRedactionConfig,
  type Occurrence,
  type Preview,
  previewKey,
  REDACTION_CONFIG_KEY,
  type RedactionCategory,
} from '../../api/redaction'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Modal } from '../../components/Modal'
import { Button, inputClass, Notice } from '../../components/ui'
import { ru } from '../../i18n/ru'

const t = ru.redaction.preview
const MAX_FRAGMENTS = 20_000

const GROUP_COLORS: Record<string, string> = {
  personal: 'bg-amber-200',
  organization: 'bg-sky-200',
  commercial: 'bg-violet-200',
  technical: 'bg-slate-300',
  custom: 'bg-emerald-200',
}

async function fetchAllFragments(documentId: string, versionId: string, signal: AbortSignal): Promise<Fragment[]> {
  const items: Fragment[] = []
  let after: number | null = null
  do {
    const page = await fetchFragments(documentId, versionId, { afterSeq: after }, signal)
    items.push(...page.items)
    after = page.next_after_seq
  } while (after !== null && items.length < MAX_FRAGMENTS)
  return items
}

interface Placed {
  occurrence: Occurrence
  entity: Entity
}

interface Selection {
  fragment: string
  start: number
  end: number
  text: string
}

/** Смещения выделения в тексте фрагмента: элементы текста помечены data-start (начало в тексте фрагмента). */
function selectionInFragment(): Selection | null {
  const selection = window.getSelection()
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null
  const range = selection.getRangeAt(0)
  const point = (node: Node, offset: number): { fragment: string; pos: number } | null => {
    const element = node.nodeType === Node.TEXT_NODE ? node.parentElement : (node as Element)
    const segment = element?.closest<HTMLElement>('[data-start]')
    const fragment = segment?.closest<HTMLElement>('[data-fragment]')
    if (!segment || !fragment || segment.dataset.masked === 'label') return null
    return { fragment: fragment.dataset.fragment ?? '', pos: Number(segment.dataset.start) + offset }
  }
  const a = point(range.startContainer, range.startOffset)
  const b = point(range.endContainer, range.endOffset)
  if (!a || !b || a.fragment !== b.fragment || b.pos <= a.pos) return null
  return { fragment: a.fragment, start: a.pos, end: b.pos, text: selection.toString() }
}

function FragmentText({
  fragment,
  placed,
  modelView,
  colorOf,
  filter,
}: {
  fragment: Fragment
  placed: Placed[]
  modelView: boolean
  colorOf: (category: string) => string
  filter: string
}) {
  const parts: ReactNode[] = []
  let pos = 0
  for (const { occurrence: o, entity } of placed) {
    if (o.start < pos) continue
    if (o.start > pos) {
      parts.push(
        <span key={`t${pos}`} data-start={pos}>
          {fragment.text.slice(pos, o.start)}
        </span>,
      )
    }
    const masked = entity.masked && o.masked
    const dimmed = filter !== '' && entity.category !== filter
    const showLabel = modelView && masked && entity.label
    parts.push(
      <mark
        key={o.id}
        id={`occ-${o.id}`}
        data-start={o.start}
        data-masked={showLabel ? 'label' : undefined}
        title={`${entity.category} · ${t.source[o.source] ?? o.source}${masked ? '' : ` · ${t.unmarkedOccurrence}`}`}
        className={
          masked
            ? `${dimmed ? 'bg-slate-100' : colorOf(entity.category)} rounded px-0.5 text-slate-900`
            : 'bg-transparent text-slate-900 underline decoration-dashed decoration-red-500'
        }
      >
        {showLabel ? entity.label : fragment.text.slice(o.start, o.end)}
      </mark>,
    )
    pos = o.end
  }
  if (pos < fragment.text.length) {
    parts.push(
      <span key={`t${pos}`} data-start={pos}>
        {fragment.text.slice(pos)}
      </span>,
    )
  }
  return (
    <p
      data-fragment={fragment.fragment_key}
      className={`whitespace-pre-wrap [content-visibility:auto] [contain-intrinsic-size:auto_3rem] ${fragment.kind === 'heading' ? 'font-semibold' : ''}`}
    >
      {parts}
    </p>
  )
}

function EntityRow({
  entity,
  categories,
  disabled,
  onEdit,
}: {
  entity: Entity
  categories: RedactionCategory[]
  disabled: boolean
  onEdit: (ops: EditOp[]) => void
}) {
  const ids = { mask: useId(), category: useId() }
  const first = entity.occurrences[0]
  return (
    <li className="space-y-1 border-b border-slate-200 py-2 last:border-b-0">
      <div className="flex items-start gap-2">
        <input
          id={ids.mask}
          type="checkbox"
          checked={entity.masked}
          disabled={disabled}
          onChange={(e) => onEdit([{ op: 'set_entity', entity_id: entity.id, masked: e.target.checked }])}
        />
        <label htmlFor={ids.mask} className="min-w-0 flex-1 break-words">
          <span className="font-medium">{entity.canonical}</span>
          {entity.masked && entity.label && <span className="ml-1 text-xs text-slate-600">→ {entity.label}</span>}
          <span className="block text-xs text-slate-600">
            {t.occurrences(entity.occurrences.length)} · {t.source[entity.source] ?? entity.source} ·{' '}
            {t.confidence[entity.confidence]}
          </span>
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-2 pl-6">
        <label htmlFor={ids.category} className="sr-only">
          {t.category}
        </label>
        <select
          id={ids.category}
          className={`${inputClass} max-w-56 py-0.5 text-xs`}
          value={entity.category}
          disabled={disabled}
          onChange={(e) => onEdit([{ op: 'set_category', entity_id: entity.id, category: e.target.value }])}
        >
          {categories.map((c) => (
            <option key={c.code} value={c.code}>
              {c.short}
            </option>
          ))}
        </select>
        {first && (
          <button
            type="button"
            className="text-xs text-blue-800 underline"
            onClick={() => document.getElementById(`occ-${first.id}`)?.scrollIntoView({ block: 'center' })}
          >
            {t.show}
          </button>
        )}
      </div>
      {entity.occurrences.length > 1 && entity.masked && (
        <details className="pl-6 text-xs">
          <summary className="cursor-pointer text-slate-700">{t.occurrences(entity.occurrences.length)}</summary>
          <ul className="mt-1 space-y-0.5">
            {entity.occurrences.map((o) => (
              <li key={o.id}>
                <label className="flex items-start gap-1">
                  <input
                    type="checkbox"
                    checked={o.masked}
                    disabled={disabled}
                    onChange={(e) => onEdit([{ op: 'set_occurrence', occurrence_id: o.id, masked: e.target.checked }])}
                  />
                  <span className="break-words">
                    {o.fragment}: «{o.text}»
                  </span>
                </label>
              </li>
            ))}
          </ul>
        </details>
      )}
    </li>
  )
}

interface Props {
  documentId: string
  versionId: string
  model: AvailableModel
  /** Анализ запускается родителем после подтверждения предпросмотра. */
  onConfirmed: (previewId: string) => void
  onBack: () => void
  submitting: boolean
  submitError: unknown
}

/**
 * Экран предпросмотра маскировки (обязателен перед облачным анализом). Пометки и подтверждение
 * проверяет и хранит сервер; найденные значения видит только автор предпросмотра.
 */
export function RedactionPreview({ documentId, versionId, model, onConfirmed, onBack, submitting, submitError }: Props) {
  const ids = { reviewed: useId(), filter: useId(), markCategory: useId(), markAll: useId(), unmasked: useId() }
  const queryClient = useQueryClient()
  const [previewId, setPreviewId] = useState<string | null>(null)
  const [modelView, setModelView] = useState(false)
  const [filter, setFilter] = useState('')
  const [reviewed, setReviewed] = useState(false)
  const [selection, setSelection] = useState<Selection | null>(null)
  const [markCategory, setMarkCategory] = useState('person_name')
  const [markAll, setMarkAll] = useState(false)
  const [unmaskedOpen, setUnmaskedOpen] = useState(false)
  const [unmaskedConfirmed, setUnmaskedConfirmed] = useState(false)

  const config = useQuery({ queryKey: REDACTION_CONFIG_KEY, queryFn: ({ signal }) => fetchRedactionConfig(signal) })
  const create = useMutation({
    mutationFn: () => createPreview(documentId, versionId),
    onSuccess: (preview) => {
      queryClient.setQueryData(previewKey(preview.id), preview)
      setPreviewId(preview.id)
      setReviewed(false)
    },
  })
  const started = useRef(false)
  const { mutate: startPreview } = create
  useEffect(() => {
    if (started.current) return
    started.current = true
    startPreview()
  }, [startPreview])

  const preview = useQuery({
    queryKey: previewKey(previewId ?? ''),
    queryFn: ({ signal }) => fetchPreview(previewId ?? '', signal),
    enabled: previewId !== null,
    refetchInterval: (query) => (query.state.data?.status === 'scanning' ? 1500 : false),
  })
  const fragments = useQuery({
    queryKey: ['redaction', 'fragments', documentId, versionId],
    queryFn: ({ signal }) => fetchAllFragments(documentId, versionId, signal),
  })
  const setPreview = (data: Preview) => queryClient.setQueryData(previewKey(data.id), data)
  const edit = useMutation({
    mutationFn: (ops: EditOp[]) => editPreview(previewId ?? '', ops),
    onSuccess: setPreview,
  })
  const confirm = useMutation({
    mutationFn: (body: ConfirmBody) => confirmPreview(previewId ?? '', body),
    onSuccess: (data) => {
      setPreview(data)
      setUnmaskedOpen(false)
      onConfirmed(data.id)
    },
  })

  const categories = useMemo(() => (config.data?.categories ?? []).filter((c) => c.supported), [config.data])
  const byCode = useMemo(() => new Map(categories.map((c) => [c.code, c])), [categories])
  const colorOf = useCallback(
    (code: string) => GROUP_COLORS[byCode.get(code)?.group ?? 'custom'] ?? 'bg-emerald-200',
    [byCode],
  )
  const data = preview.data
  const entities = useMemo(() => data?.entities ?? [], [data])
  const placed = useMemo(() => {
    const map = new Map<string, Placed[]>()
    for (const entity of entities) {
      for (const occurrence of entity.occurrences) {
        const list = map.get(occurrence.fragment) ?? []
        list.push({ occurrence, entity })
        map.set(occurrence.fragment, list)
      }
    }
    for (const list of map.values()) list.sort((a, b) => a.occurrence.start - b.occurrence.start)
    return map
  }, [entities])
  const present = useMemo(() => {
    const counts = new Map<string, number>()
    for (const e of entities) if (e.masked) counts.set(e.category, (counts.get(e.category) ?? 0) + 1)
    return [...counts.entries()]
  }, [entities])

  const ready = data?.status === 'ready' && !data.stale
  const busy = edit.isPending || confirm.isPending || submitting
  const review = entities.filter((e) => e.confidence === 'low')
  const regular = entities.filter((e) => e.confidence !== 'low')
  const counts = data?.counts ?? {}
  const recheck = () => {
    setPreviewId(null)
    create.mutate()
  }

  // Выделение текста мышью или клавиатурой (Shift+стрелки) → форма ручной пометки.
  const textRef = useRef<HTMLDivElement>(null)
  const markable = ready && !modelView
  useEffect(() => {
    if (!markable) return
    const onSelect = () => {
      const anchor = window.getSelection()?.anchorNode
      if (!anchor || !textRef.current?.contains(anchor)) return
      const found = selectionInFragment()
      if (found) setSelection(found)
    }
    document.addEventListener('mouseup', onSelect)
    document.addEventListener('keyup', onSelect)
    return () => {
      document.removeEventListener('mouseup', onSelect)
      document.removeEventListener('keyup', onSelect)
    }
  }, [markable])

  const unmaskedCategories = Object.entries(counts.categories ?? {})

  return (
    <section aria-labelledby="redaction-title" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="redaction-title" className="text-lg font-semibold">
          {t.title}
        </h2>
        <Button onClick={onBack} disabled={busy}>
          {t.back}
        </Button>
      </div>
      <p className="text-sm text-slate-700">{t.intro}</p>

      {create.isError && <ErrorNotice error={create.error} onRetry={recheck} />}
      {(create.isPending || data?.status === 'scanning') && (
        <output className="block text-sm">{t.scanning}</output>
      )}
      {preview.isError && <ErrorNotice error={preview.error} onRetry={() => void preview.refetch()} />}
      {data?.status === 'failed' && (
        <div role="alert" className="space-y-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900">
          <p>{data.error_message ?? t.failed}</p>
          <Button onClick={recheck}>{t.recheck}</Button>
        </div>
      )}
      {data?.stale && (
        <div role="alert" className="space-y-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          <p>
            {t.stale[data.stale_reason ?? ''] ?? ''} {t.staleHint}
          </p>
          <Button onClick={recheck}>{t.recheck}</Button>
        </div>
      )}

      {data && data.status !== 'scanning' && data.status !== 'failed' && (
        <>
          {data.ner_available === false && <Notice tone="warning">{t.nerUnavailable}</Notice>}
          <p aria-live="polite" className="text-sm font-medium">
            {entities.length === 0 ? t.noneFound : t.summary(counts.values ?? 0, counts.occurrences ?? 0)}
          </p>
          <div className="flex flex-wrap items-center gap-4 text-sm">
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={modelView} onChange={(e) => setModelView(e.target.checked)} />
              {t.modelView}
            </label>
            <label htmlFor={ids.filter} className="flex items-center gap-2">
              {t.filter}
              <select
                id={ids.filter}
                className={`${inputClass} w-auto py-0.5`}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              >
                <option value="">{t.allCategories}</option>
                {present.map(([code, n]) => (
                  <option key={code} value={code}>
                    {byCode.get(code)?.short ?? code} ({n})
                  </option>
                ))}
              </select>
            </label>
            <ul className="flex flex-wrap gap-2 text-xs" aria-label={t.filter}>
              {present.map(([code]) => (
                <li key={code} className={`${colorOf(code)} rounded px-1.5`}>
                  {byCode.get(code)?.short ?? code}
                </li>
              ))}
            </ul>
          </div>

          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
            <section aria-label={t.text} className="space-y-2">
              {ready && !modelView && <p className="text-xs text-slate-600">{t.markHint}</p>}
              {selection && (
                <form
                  aria-label={t.markTitle}
                  className="space-y-2 rounded-md border border-blue-200 bg-blue-50 p-3 text-sm"
                  onSubmit={(e) => {
                    e.preventDefault()
                    edit.mutate(
                      [
                        {
                          op: 'add',
                          fragment: selection.fragment,
                          start: selection.start,
                          end: selection.end,
                          category: markCategory,
                          all_occurrences: markAll,
                        },
                      ],
                      { onSuccess: () => setSelection(null) },
                    )
                  }}
                >
                  <p className="font-medium">
                    {t.markTitle}: «{selection.text}»
                  </p>
                  <label htmlFor={ids.markCategory} className="flex items-center gap-2">
                    {t.markCategory}
                    <select
                      id={ids.markCategory}
                      className={`${inputClass} w-auto py-0.5`}
                      value={markCategory}
                      onChange={(e) => setMarkCategory(e.target.value)}
                    >
                      {categories.map((c) => (
                        <option key={c.code} value={c.code}>
                          {c.short}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label htmlFor={ids.markAll} className="flex items-center gap-2">
                    <input
                      id={ids.markAll}
                      type="checkbox"
                      checked={markAll}
                      onChange={(e) => setMarkAll(e.target.checked)}
                    />
                    {t.markAll}
                  </label>
                  <div className="flex gap-2">
                    <Button type="submit" variant="primary" disabled={busy}>
                      {t.mark}
                    </Button>
                    <Button onClick={() => setSelection(null)}>{t.cancelMark}</Button>
                  </div>
                </form>
              )}
              {edit.isError && <ErrorNotice error={edit.error} />}
              {fragments.isPending && <p className="text-sm">{t.loadingText}</p>}
              {fragments.isError && <ErrorNotice error={fragments.error} onRetry={() => void fragments.refetch()} />}
              {fragments.data && (
                <div
                  ref={textRef}
                  className="max-h-[70vh] space-y-2 overflow-y-auto rounded-md border border-slate-200 bg-white p-3 text-sm leading-relaxed"
                >
                  {fragments.data.map((f) => (
                    <FragmentText
                      key={f.id}
                      fragment={f}
                      placed={placed.get(f.fragment_key) ?? []}
                      modelView={modelView}
                      colorOf={colorOf}
                      filter={filter}
                    />
                  ))}
                </div>
              )}
            </section>

            <aside aria-label={t.entities} className="space-y-3 text-sm">
              {review.length > 0 && (
                <section aria-labelledby="redaction-review" className="rounded-md border border-amber-200 bg-amber-50 p-3">
                  <h3 id="redaction-review" className="font-semibold">
                    {t.review}
                  </h3>
                  <ul>
                    {review.map((e) => (
                      <EntityRow key={e.id} entity={e} categories={categories} disabled={!ready || busy} onEdit={edit.mutate} />
                    ))}
                  </ul>
                </section>
              )}
              <section aria-labelledby="redaction-entities" className="rounded-md border border-slate-200 bg-white p-3">
                <h3 id="redaction-entities" className="font-semibold">
                  {t.entities}
                </h3>
                <ul className="max-h-[60vh] overflow-y-auto">
                  {regular.map((e) => (
                    <EntityRow key={e.id} entity={e} categories={categories} disabled={!ready || busy} onEdit={edit.mutate} />
                  ))}
                </ul>
              </section>
            </aside>
          </div>

          <div className="space-y-3 rounded-md border border-slate-200 bg-slate-50 p-3 text-sm">
            <label htmlFor={ids.reviewed} className="flex items-center gap-2 font-medium">
              <input
                id={ids.reviewed}
                type="checkbox"
                checked={reviewed}
                disabled={!ready}
                onChange={(e) => setReviewed(e.target.checked)}
              />
              {t.reviewed}
            </label>
            {confirm.isError && <ErrorNotice error={confirm.error} />}
            {submitError ? <ErrorNotice error={submitError} /> : null}
            <div className="flex flex-wrap gap-2">
              <Button
                variant="primary"
                disabled={!ready || !reviewed || busy}
                onClick={() => confirm.mutate({ mode: 'masked', reviewed: true })}
              >
                {busy ? t.confirming : t.confirm}
              </Button>
              {data.allow_unmasked && (
                <Button
                  variant="danger"
                  disabled={!ready || !reviewed || busy}
                  onClick={() => {
                    setUnmaskedConfirmed(false)
                    setUnmaskedOpen(true)
                  }}
                >
                  {t.unmasked}
                </Button>
              )}
            </div>
          </div>
        </>
      )}

      {unmaskedOpen && data && (
        <Modal title={t.unmaskedTitle} onClose={() => setUnmaskedOpen(false)}>
          <p className="text-red-900">{t.unmaskedWarning(model.provider_name, model.display_name)}</p>
          {unmaskedCategories.length > 0 ? (
            <div>
              <p>{t.unmaskedCounts}</p>
              <ul className="list-disc pl-5">
                {unmaskedCategories.map(([code, n]) => (
                  <li key={code}>
                    {byCode.get(code)?.short ?? code}: {n}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <p>{t.unmaskedNone}</p>
          )}
          <label htmlFor={ids.unmasked} className="flex items-start gap-2 font-medium">
            <input
              id={ids.unmasked}
              type="checkbox"
              checked={unmaskedConfirmed}
              onChange={(e) => setUnmaskedConfirmed(e.target.checked)}
            />
            {t.unmaskedConfirm}
          </label>
          {confirm.isError && <ErrorNotice error={confirm.error} />}
          <div className="flex flex-wrap gap-2">
            <Button
              variant="danger"
              disabled={!unmaskedConfirmed || busy || !config.data}
              onClick={() =>
                confirm.mutate({
                  mode: 'unmasked',
                  reviewed: true,
                  confirm_unmasked: true,
                  unmasked_notice_version: config.data?.unmasked_notice_version,
                })
              }
            >
              {t.unmaskedSubmit}
            </Button>
            <Button onClick={() => setUnmaskedOpen(false)}>{t.cancel}</Button>
          </div>
        </Modal>
      )}
    </section>
  )
}
