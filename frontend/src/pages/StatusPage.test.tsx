import { screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { renderRoute } from '../test/render'
import { server } from '../test/server'

describe('StatusPage', () => {
  it('показывает готовность сервисов из API', async () => {
    server.use(
      http.get('*/api/health/ready', () =>
        HttpResponse.json({ status: 'ready', checks: { database: 'ok', migrations: 'ok', storage: 'ok' }, version: '0.1.0' }),
      ),
    )
    renderRoute('/')
    expect(await screen.findByText('Все сервисы готовы к работе')).toBeInTheDocument()
    expect(screen.getByText('База данных')).toBeInTheDocument()
    expect(screen.getAllByText('в порядке')).toHaveLength(3)
  })

  it('показывает неготовность (503) как состояние, а не как ошибку', async () => {
    server.use(
      http.get('*/api/health/ready', () =>
        HttpResponse.json(
          { status: 'not_ready', checks: { database: 'unavailable', migrations: 'unknown', storage: 'ok' }, version: '0.1.0' },
          { status: 503 },
        ),
      ),
    )
    renderRoute('/')
    expect(await screen.findByText('Часть сервисов недоступна')).toBeInTheDocument()
    expect(screen.getByText('недоступно')).toBeInTheDocument()
  })

  it('показывает ошибку с кодом запроса', async () => {
    server.use(
      http.get('*/api/health/ready', () =>
        HttpResponse.json(
          { error: { code: 'internal_error', message: 'Внутренняя ошибка сервера', request_id: 'rid-42', details: {} } },
          { status: 500 },
        ),
      ),
    )
    renderRoute('/')
    expect(await screen.findByRole('alert')).toHaveTextContent('Внутренняя ошибка сервера')
    expect(screen.getByText('rid-42')).toBeInTheDocument()
  })

  it('всегда показывает правовую оговорку', async () => {
    server.use(http.get('*/api/health/ready', () => HttpResponse.json({ status: 'ready', checks: {}, version: '0.1.0' })))
    renderRoute('/')
    expect(await screen.findByText(/не заменяют консультацию юриста/)).toBeInTheDocument()
  })
})

describe('NotFound', () => {
  it('отображает страницу 404 для неизвестного пути', async () => {
    renderRoute('/unknown/path')
    expect(await screen.findByText('Страница не найдена')).toBeInTheDocument()
  })
})
