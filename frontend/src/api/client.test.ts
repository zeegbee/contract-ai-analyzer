import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { server } from '../test/server'
import { ApiError, apiRequest } from './client'

describe('apiRequest', () => {
  it('разбирает единый формат ошибки', async () => {
    server.use(
      http.get('*/api/documents/1', () =>
        HttpResponse.json(
          { error: { code: 'document_not_found', message: 'Документ не найден', request_id: 'req-123', details: {} } },
          { status: 404 },
        ),
      ),
    )
    const error = await apiRequest('/documents/1').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 404, code: 'document_not_found', message: 'Документ не найден', requestId: 'req-123' })
  })

  it('не показывает сырое тело не-JSON ошибки', async () => {
    server.use(
      http.get('*/api/x', () => new HttpResponse('<html>502 Bad Gateway nginx</html>', {
        status: 502,
        headers: { 'X-Request-ID': 'rid-1' },
      })),
    )
    const error = (await apiRequest('/x').catch((e: unknown) => e)) as ApiError
    expect(error.code).toBe('http_502')
    expect(error.message).not.toContain('nginx')
    expect(error.requestId).toBe('rid-1')
  })

  it('сетевую ошибку превращает в понятное сообщение', async () => {
    server.use(http.get('*/api/x', () => HttpResponse.error()))
    const error = (await apiRequest('/x').catch((e: unknown) => e)) as ApiError
    expect(error.code).toBe('network_error')
  })

  it('добавляет CSRF-токен только к изменяющим запросам', async () => {
    document.cookie = 'cai_csrf=token-abc; path=/'
    const seen: Record<string, string | null> = {}
    server.use(
      http.get('*/api/x', ({ request }) => {
        seen.get = request.headers.get('X-CSRF-Token')
        return HttpResponse.json({})
      }),
      http.post('*/api/x', ({ request }) => {
        seen.post = request.headers.get('X-CSRF-Token')
        return HttpResponse.json({})
      }),
    )
    await apiRequest('/x')
    await apiRequest('/x', { method: 'POST', body: { a: 1 } })
    expect(seen.get).toBeNull()
    expect(seen.post).toBe('token-abc')
  })
})
