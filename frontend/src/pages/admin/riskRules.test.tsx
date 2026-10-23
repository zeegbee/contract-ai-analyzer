import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RiskRule } from '../../api/riskRules'
import { makeMe, serveMe } from '../../test/auth'
import { renderRoute } from '../../test/render'
import { LIMITS, makeRule } from '../../test/riskRules'
import { server } from '../../test/server'

interface Log {
  created: Record<string, unknown>[]
  patched: Record<string, unknown>[]
  archived: Record<string, unknown>[]
}

function serveRules(active: RiskRule[], archived: RiskRule[] = []): Log {
  const log: Log = { created: [], patched: [], archived: [] }
  server.use(
    http.get('*/api/admin/risk-rules', ({ request }) => {
      const showArchive = new URL(request.url).searchParams.get('archived') === 'true'
      return HttpResponse.json({ items: showArchive ? archived : active, limits: LIMITS })
    }),
    http.post('*/api/admin/risk-rules', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>
      log.created.push(body)
      return HttpResponse.json(makeRule({ id: 'rr-new', code: 'RR-007', ...body }), { status: 201 })
    }),
    http.patch('*/api/admin/risk-rules/:id', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>
      log.patched.push(body)
      if (body.version !== 1) {
        return HttpResponse.json(
          {
            error: {
              code: 'version_conflict',
              message: 'Запись изменена другим пользователем. Обновите страницу и повторите.',
              request_id: 'req-9',
              details: {},
            },
          },
          { status: 409 },
        )
      }
      return HttpResponse.json(makeRule({ ...body, version: 2 }))
    }),
    http.post('*/api/admin/risk-rules/:id/:action', async ({ request, params }) => {
      log.archived.push({ action: params.action, ...((await request.json()) as Record<string, unknown>) })
      return HttpResponse.json(makeRule({ archived_at: params.action === 'archive' ? '2026-10-23T11:00:00Z' : null }))
    }),
    http.get('*/api/admin/risk-rules/:id/revisions', () =>
      HttpResponse.json({
        items: [
          {
            id: 'rev-1',
            entity_type: 'risk_rule',
            entity_id: 'rr-1',
            revision: 1,
            diff: { title: { from: null, to: 'Высокая неустойка' } },
            changed_by_id: 'id-admin',
            created_at: '2026-10-23T10:00:00Z',
          },
        ],
      }),
    ),
  )
  return log
}

afterEach(() => vi.restoreAllMocks())

describe('страница правил рисков', () => {
  it('администратор видит правила, создаёт новое и получает предупреждение о данных', async () => {
    serveMe(makeMe('admin'))
    const log = serveRules([makeRule(), makeRule({ id: 'rr-2', code: 'RR-002', title: 'Нет приёмки', category: 'acceptance', kind: 'missing_clause' })])
    renderRoute('/admin/risk-rules')
    const user = userEvent.setup()
    expect(await screen.findByRole('heading', { name: 'Правила рисков организации' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Правила' })).toHaveAttribute('aria-current', 'page')
    const table = await screen.findByRole('table')
    expect(within(table).getByText('RR-001')).toBeInTheDocument()
    expect(within(table).getByText('Нет приёмки')).toBeInTheDocument()
    expect(screen.getByText(/Правил в каталоге: 2 \/ 100/)).toBeInTheDocument()

    // Фильтр по категории.
    await user.selectOptions(screen.getByLabelText('Категория'), 'acceptance')
    expect(within(screen.getByRole('table')).queryByText('RR-001')).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Категория'), '')

    await user.click(screen.getByRole('button', { name: 'Новое правило' }))
    const dialog = await screen.findByRole('dialog', { name: 'Новое правило' })
    expect(within(dialog).getByText(/Не указывайте в правилах персональные данные/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Сохранить' }))
    expect(within(dialog).getByRole('alert')).toHaveTextContent('Заполните название и описание')
    expect(log.created).toEqual([])

    await user.type(within(dialog).getByLabelText('Название'), 'Долгая оплата')
    await user.selectOptions(within(dialog).getByLabelText('Категория'), 'payment')
    await user.selectOptions(within(dialog).getByLabelText('Где искать'), 'any')
    await user.type(within(dialog).getByLabelText('Что считать риском'), 'Срок оплаты больше 30 дней')
    expect(within(dialog).getByText('26 / 1000 символов')).toBeInTheDocument()
    await user.click(within(dialog).getByLabelText('Отмечено по умолчанию в форме анализа'))
    await user.click(within(dialog).getByRole('button', { name: 'Сохранить' }))
    await waitFor(() => expect(log.created).toHaveLength(1))
    expect(log.created[0]).toEqual({
      title: 'Долгая оплата',
      category: 'payment',
      kind: 'any',
      description: 'Срок оплаты больше 30 дней',
      signs: null,
      severity: null,
      recommendation: null,
      enabled: true,
      default_selected: false,
    })
    expect(await screen.findByText('Правило создано')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('переключатели в таблице, конфликт версии, архив и восстановление', async () => {
    serveMe(makeMe('superadmin'))
    const log = serveRules([makeRule()], [makeRule({ id: 'rr-9', code: 'RR-009', title: 'Старое правило', archived_at: '2026-10-20T10:00:00Z', version: 1 })])
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderRoute('/admin/risk-rules')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('checkbox', { name: 'Включено: RR-001 Высокая неустойка' }))
    await waitFor(() => expect(log.patched).toEqual([{ enabled: false, version: 1 }]))
    await user.click(screen.getByRole('checkbox', { name: 'По умолчанию: RR-001 Высокая неустойка' }))
    await waitFor(() => expect(log.patched).toHaveLength(2))

    // Изменение в форме устаревшей версии — ошибка сервера показывается.
    await user.click(screen.getByRole('button', { name: 'Изменить: RR-001 Высокая неустойка' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByLabelText('Название')).toHaveValue('Высокая неустойка')
    expect(within(dialog).getByLabelText('Рекомендуемый уровень')).toHaveValue('high')

    await user.click(within(dialog).getByRole('button', { name: 'Отмена' }))
    await user.click(screen.getByRole('button', { name: 'В архив: RR-001 Высокая неустойка' }))
    await waitFor(() => expect(log.archived).toEqual([{ action: 'archive', version: 1 }]))
    expect(window.confirm).toHaveBeenCalled()

    await user.click(screen.getByLabelText('Показать архив'))
    expect(await screen.findByText('Старое правило')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Изменить: RR-009/ })).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Включено: RR-009 Старое правило' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Восстановить: RR-009 Старое правило' }))
    await waitFor(() => expect(log.archived).toHaveLength(2))
    expect(log.archived[1]).toEqual({ action: 'restore', version: 1 })

    await user.click(screen.getByText('История изменений'))
    expect(await screen.findByText(/ред\. 1 ·/)).toBeInTheDocument()
  })

  it('ошибка сервера при сохранении показывается в форме', async () => {
    serveMe(makeMe('admin'))
    serveRules([makeRule({ version: 5 })])
    renderRoute('/admin/risk-rules')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Изменить: RR-001 Высокая неустойка' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Название'), ' 2')
    await user.click(within(dialog).getByRole('button', { name: 'Сохранить' }))
    expect(await within(dialog).findByText(/изменена другим пользователем/)).toBeInTheDocument()
    expect(within(dialog).getByText(/req-9/)).toBeInTheDocument()
  })

  it('аналитику и наблюдателю страница и пункт меню недоступны', async () => {
    for (const role of ['analyst', 'viewer'] as const) {
      serveMe(makeMe(role))
      server.use(http.get('*/api/admin/risk-rules', () => HttpResponse.json({ items: [], limits: LIMITS })))
      const { unmount } = renderRoute('/admin/risk-rules')
      expect(await screen.findByRole('alert')).toHaveTextContent(/недостаточно прав/i)
      expect(screen.queryByRole('link', { name: 'Правила' })).not.toBeInTheDocument()
      unmount()
    }
  })
})
