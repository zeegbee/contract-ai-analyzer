import { useInfiniteQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { DOCUMENTS_KEY, fetchFragments, type Fragment, type PageSource } from '../../api/documents'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Button, inputClass } from '../../components/ui'
import { ru } from '../../i18n/ru'

const t = ru.documents.text

function pageLabel(f: Fragment): string | null {
  if (f.page_start === null) return null
  return f.page_end !== null && f.page_end !== f.page_start
    ? `${t.pageShort} ${f.page_start}–${f.page_end}`
    : `${t.pageShort} ${f.page_start}`
}

function FragmentText({ fragment }: { fragment: Fragment }) {
  const cells = fragment.meta.cells
  if (fragment.kind === 'table_row' && Array.isArray(cells)) {
    return (
      <div className="flex flex-wrap gap-px bg-slate-200">
        {cells.map((cell, i) => (
          // Порядок ячеек фиксирован, индекс — стабильный ключ.
          <span key={i} className="min-w-24 flex-1 bg-white px-2 py-1 whitespace-pre-wrap">
            {typeof cell === 'string' ? cell : ''}
          </span>
        ))}
      </div>
    )
  }
  const label = typeof fragment.meta.list_label === 'string' ? fragment.meta.list_label : null
  return (
    <p className={`whitespace-pre-wrap ${fragment.kind === 'heading' ? 'font-semibold' : ''}`}>
      {label && <span className="mr-1 text-slate-500">{label}</span>}
      {fragment.text}
    </p>
  )
}

interface Props {
  documentId: string
  versionId: string
  pageCount: number | null
  pageSource: PageSource | null
  /** Фрагмент, к которому нужно перейти (из карточки риска), и его страница. */
  focusKey?: string | null
  focusPage?: number | null
}

export function TextViewer({ documentId, versionId, pageCount, pageSource, focusKey = null, focusPage = null }: Props) {
  const hasPages = pageSource !== null && pageCount !== null && pageCount > 0
  const initialPage = focusKey ? (focusPage ?? null) : 1
  const [page, setPage] = useState<number | null>(hasPages ? initialPage : null)

  const query = useInfiniteQuery({
    queryKey: [...DOCUMENTS_KEY, documentId, 'fragments', versionId, page],
    queryFn: ({ pageParam, signal }) => fetchFragments(documentId, versionId, { page, afterSeq: pageParam }, signal),
    initialPageParam: null as number | null,
    getNextPageParam: (last) => last.next_after_seq,
  })
  const fragments = query.data?.pages.flatMap((p) => p.items) ?? []
  const total = query.data?.pages[0]?.total ?? 0
  const focusLoaded = Boolean(focusKey && fragments.some((f) => f.fragment_key === focusKey))
  useEffect(() => {
    if (!focusKey || !focusLoaded) return
    const el = document.getElementById(focusKey)
    el?.scrollIntoView?.({ block: 'center' })
    el?.focus({ preventScroll: true })
  }, [focusKey, focusLoaded])

  return (
    <section aria-labelledby="text-title" className="space-y-3 rounded-lg border border-slate-200 bg-white p-4">
      <h2 id="text-title" className="font-semibold">
        {t.title}
      </h2>
      <p className="text-xs text-slate-600">
        {ru.documents.pageSource[pageSource ?? 'none']} {t.offsetsNote}
      </p>
      {hasPages && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Button disabled={page === null || page <= 1} onClick={() => setPage((p) => (p ? p - 1 : 1))}>
            {t.prev}
          </Button>
          <label htmlFor="page-select" className="sr-only">
            {t.page}
          </label>
          <select
            id="page-select"
            className={`${inputClass} w-auto`}
            value={page ?? ''}
            onChange={(e) => setPage(e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">{t.allPages}</option>
            {Array.from({ length: pageCount }, (_, i) => i + 1).map((n) => (
              <option key={n} value={n}>
                {t.page} {n}
              </option>
            ))}
          </select>
          <Button
            disabled={page === null || page >= pageCount}
            onClick={() => setPage((p) => (p ? Math.min(p + 1, pageCount) : 1))}
          >
            {t.next}
          </Button>
        </div>
      )}
      {query.isPending && <p aria-live="polite">{ru.common.loading}</p>}
      {query.isError && <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />}
      {query.isSuccess && fragments.length === 0 && <p className="text-sm text-slate-600">{t.empty}</p>}
      {fragments.length > 0 && (
        <ol className="space-y-2 text-sm" aria-label={t.title}>
          {fragments.map((f) => {
            const where = pageLabel(f)
            return (
              <li
                key={f.id}
                id={f.fragment_key}
                tabIndex={f.fragment_key === focusKey ? -1 : undefined}
                aria-current={f.fragment_key === focusKey ? 'true' : undefined}
                className={`rounded-md border p-2 ${f.fragment_key === focusKey ? 'border-amber-400 bg-amber-50 ring-2 ring-amber-300' : f.kind === 'header_footer' ? 'border-dashed border-slate-200 text-slate-500' : 'border-slate-100'}`}
              >
                <div className="mb-1 flex flex-wrap gap-x-2 text-xs text-slate-500">
                  <code>{f.fragment_key}</code>
                  <span>{ru.documents.kinds[f.kind] ?? f.kind}</span>
                  {f.clause_ref && (
                    <span>
                      {t.clause} {f.clause_ref}
                    </span>
                  )}
                  {where && <span>{where}</span>}
                  {f.heading_path.length > 0 && f.kind !== 'heading' && (
                    <span className="truncate" title={f.heading_path.join(' › ')}>
                      {f.heading_path.join(' › ')}
                    </span>
                  )}
                </div>
                <FragmentText fragment={f} />
              </li>
            )
          })}
        </ol>
      )}
      {fragments.length > 0 && <p className="text-xs text-slate-600">{t.shown(fragments.length, total)}</p>}
      {query.hasNextPage && (
        <Button onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}>
          {t.loadMore}
        </Button>
      )}
    </section>
  )
}
