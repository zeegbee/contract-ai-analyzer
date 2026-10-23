import { useQueryClient } from '@tanstack/react-query'
import { type DragEvent, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'

import { ME_QUERY_KEY } from '../../api/auth'
import { ApiError } from '../../api/client'
import { DOCUMENTS_KEY, uploadDocument, type UploadLimits } from '../../api/documents'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Button } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatBytes } from '../../lib/format'
import { validateFile } from '../../lib/upload'

type ItemState = 'uploading' | 'done' | 'error'

interface UploadItem {
  key: string
  file: File
  idempotencyKey: string
  progress: number
  state: ItemState
  /** Ошибка проверки в браузере (до отправки) — текст; ошибка сервера — объект ApiError. */
  error?: string | unknown
  documentId?: string
}

const t = ru.documents.upload

function newKey(): string {
  return typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

export function UploadPanel({ limits }: { limits: UploadLimits }) {
  const queryClient = useQueryClient()
  const [items, setItems] = useState<UploadItem[]>([])
  const [dragging, setDragging] = useState(false)
  const controllers = useRef(new Map<string, AbortController>())

  useEffect(() => {
    const active = controllers.current
    return () => active.forEach((c) => c.abort())
  }, [])

  const update = (key: string, patch: Partial<UploadItem>) =>
    setItems((list) => list.map((item) => (item.key === key ? { ...item, ...patch } : item)))

  const start = (item: UploadItem) => {
    const controller = new AbortController()
    controllers.current.set(item.key, controller)
    update(item.key, { state: 'uploading', progress: 0, error: undefined })
    uploadDocument(item.file, item.idempotencyKey, (progress) => update(item.key, { progress }), controller.signal)
      .then((doc) => {
        update(item.key, { state: 'done', progress: 1, documentId: doc.id })
        void queryClient.invalidateQueries({ queryKey: DOCUMENTS_KEY })
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        if (error instanceof ApiError && error.status === 401) queryClient.setQueryData(ME_QUERY_KEY, null)
        update(item.key, { state: 'error', error })
      })
      .finally(() => controllers.current.delete(item.key))
  }

  const addFiles = (files: FileList | File[]) => {
    const added = Array.from(files).map<UploadItem>((file) => {
      const problem = validateFile(file, limits)
      return {
        key: newKey(),
        file,
        idempotencyKey: newKey(),
        progress: 0,
        state: problem ? 'error' : 'uploading',
        error: problem ?? undefined,
      }
    })
    setItems((list) => [...added, ...list])
    added.filter((item) => item.state === 'uploading').forEach(start)
  }

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    setDragging(false)
    if (event.dataTransfer.files.length) addFiles(event.dataTransfer.files)
  }

  return (
    <section aria-labelledby="upload-title" className="space-y-3 rounded-lg border border-slate-300 bg-white shadow-sm p-4">
      <h2 id="upload-title" className="font-semibold">
        {t.title}
      </h2>
      <div
        data-testid="dropzone"
        onDragEnter={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragOver={(e) => e.preventDefault()}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={`flex flex-col items-center gap-2 rounded-lg border-2 border-dashed p-6 text-center text-sm ${
          dragging ? 'border-blue-600 bg-blue-50' : 'border-slate-300 bg-slate-50'
        }`}
      >
        {dragging ? (
          <p className="font-medium text-blue-900">{t.dropActive}</p>
        ) : (
          <p>
            <label
              htmlFor="document-files"
              className="cursor-pointer rounded-md border border-blue-700 bg-blue-700 px-3 py-1.5 text-white hover:bg-blue-800 focus-within:ring-2"
            >
              {t.choose}
            </label>{' '}
            <span className="text-slate-700">{t.dropHint}</span>
          </p>
        )}
        <input
          id="document-files"
          type="file"
          multiple
          accept={limits.extensions.map((e) => `.${e}`).join(',')}
          className="sr-only"
          onChange={(e) => {
            if (e.target.files?.length) addFiles(e.target.files)
            e.target.value = ''
          }}
        />
        <p className="text-xs text-slate-600">{t.limits(limits.max_upload_mb, limits.max_pages)}</p>
      </div>

      {items.length > 0 && (
        <ul className="space-y-2" aria-live="polite">
          {items.map((item) => (
            <li key={item.key} className="rounded-md border border-slate-200 p-3 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium break-all">{item.file.name}</span>
                <span className="text-xs text-slate-600">{formatBytes(item.file.size)}</span>
              </div>
              {item.state === 'uploading' && (
                <div className="mt-2 flex items-center gap-2">
                  <progress
                    className="h-2 w-full"
                    max={100}
                    value={Math.round(item.progress * 100)}
                    aria-label={t.progress(item.file.name)}
                  />
                  <span className="w-10 text-right text-xs">{Math.round(item.progress * 100)}%</span>
                </div>
              )}
              {item.state === 'done' && item.documentId && (
                <p className="mt-1 text-green-800">
                  {t.done} ·{' '}
                  <Link className="text-blue-800 underline" to={`/documents/${item.documentId}`}>
                    {t.open}
                  </Link>
                </p>
              )}
              {item.state === 'error' && (
                <div className="mt-2 space-y-2">
                  {typeof item.error === 'string' ? (
                    <p role="alert" className="text-red-800">
                      {item.error}
                    </p>
                  ) : (
                    <ErrorNotice error={item.error} />
                  )}
                  <div className="flex gap-2">
                    {typeof item.error !== 'string' && (
                      <Button onClick={() => start(item)}>{t.retry}</Button>
                    )}
                    <Button onClick={() => setItems((list) => list.filter((i) => i.key !== item.key))}>
                      {t.remove}
                    </Button>
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
