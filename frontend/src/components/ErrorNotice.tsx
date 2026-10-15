import { ApiError } from '../api/client'
import { ru } from '../i18n/ru'

interface Props {
  error: unknown
  onRetry?: () => void
}

/** Безопасное сообщение об ошибке: только текст от API и request ID, без технических деталей. */
export function ErrorNotice({ error, onRetry }: Props) {
  const message = error instanceof ApiError ? error.message : 'Произошла непредвиденная ошибка'
  const requestId = error instanceof ApiError ? error.requestId : null
  return (
    <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-900">
      <p>{message}</p>
      {requestId && (
        <p className="mt-1 text-xs text-red-800">
          {ru.errors.requestId}: <code>{requestId}</code>
        </p>
      )}
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 rounded-md border border-red-300 bg-white px-3 py-1.5 text-red-900 hover:bg-red-100"
        >
          {ru.errors.retry}
        </button>
      )}
    </div>
  )
}
