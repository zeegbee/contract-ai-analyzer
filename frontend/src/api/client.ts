/**
 * HTTP-клиент API.
 *
 * - Запросы идут на тот же origin (`/api`), cookie сессии отправляются автоматически.
 * - Для изменяющих запросов добавляется `X-CSRF-Token` из cookie `cai_csrf`.
 * - Ошибки разбираются из единого формата backend: {error: {code, message, request_id, details}}.
 */

const CSRF_COOKIE = 'cai_csrf'
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string | null
  readonly details: Record<string, unknown>

  constructor(
    status: number,
    code: string,
    message: string,
    requestId: string | null,
    details: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
    this.details = details
  }
}

const NETWORK_ERROR_MESSAGE = 'Сервер недоступен. Проверьте подключение и повторите попытку.'
const UNKNOWN_ERROR_MESSAGE = 'Не удалось выполнить запрос'

export function readCookie(name: string): string | null {
  for (const part of document.cookie.split(';')) {
    const [key, ...rest] = part.trim().split('=')
    if (key === name) return decodeURIComponent(rest.join('='))
  }
  return null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Ошибка из тела ответа в едином формате backend (используется и для XHR-загрузки файлов). */
export function apiErrorFromBody(status: number, body: unknown, headerRequestId: string | null): ApiError {
  const err = isRecord(body) && isRecord(body.error) ? body.error : null
  const code = err && typeof err.code === 'string' ? err.code : `http_${status}`
  const message = err && typeof err.message === 'string' ? err.message : UNKNOWN_ERROR_MESSAGE
  const requestId = err && typeof err.request_id === 'string' ? err.request_id : headerRequestId
  const details = err && isRecord(err.details) ? err.details : {}
  return new ApiError(status, code, message, requestId, details)
}

export const networkError = () => new ApiError(0, 'network_error', NETWORK_ERROR_MESSAGE, null)

async function toApiError(response: Response): Promise<ApiError> {
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    // тело не JSON (например, ответ прокси) — используем общий текст
  }
  return apiErrorFromBody(response.status, body, response.headers.get('X-Request-ID'))
}

export interface RequestOptions {
  method?: string
  body?: unknown
  signal?: AbortSignal
  /** Разрешить статусы, отличные от 2xx, и вернуть тело как есть (для readiness 503). */
  acceptStatuses?: number[]
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method ?? 'GET').toUpperCase()
  const headers: Record<string, string> = { Accept: 'application/json' }
  let body: BodyInit | undefined
  if (options.body instanceof FormData) {
    body = options.body
  } else if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }
  if (!SAFE_METHODS.has(method)) {
    const csrf = readCookie(CSRF_COOKIE)
    if (csrf) headers['X-CSRF-Token'] = csrf
  }

  let response: Response
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers,
      body,
      credentials: 'same-origin',
      signal: options.signal,
    })
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw networkError()
  }

  if (!response.ok && !options.acceptStatuses?.includes(response.status)) {
    throw await toApiError(response)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}
