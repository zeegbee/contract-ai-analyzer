import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { makeMe, serveMe } from '../../test/auth'
import { makeRetention, retentionHandler } from '../../test/fixtures'
import { renderRoute } from '../../test/render'
import { server } from '../../test/server'

const settingsHandlers = [
  http.get('*/api/admin/system-settings', () => HttpResponse.json({ groups: {}, editable: false })),
  http.get('*/api/admin/llm-settings', () =>
    HttpResponse.json({ cloud_models_enabled: false, cloud_notice: { version: '1', text: 'x' }, can_edit: false }),
  ),
]

describe('политика хранения документов', () => {
  it('по умолчанию выключена; Admin видит состояние, но не меняет', async () => {
    serveMe(makeMe('admin'))
    server.use(retentionHandler(), ...settingsHandlers)
    renderRoute('/admin/settings')
    expect(await screen.findByText(/Автоматическое удаление выключено/)).toBeInTheDocument()
    expect(screen.getByText('Изменять политику хранения может только Superadmin.')).toBeInTheDocument()
    expect(screen.queryByLabelText('Удалять документы автоматически')).not.toBeInTheDocument()
  })

  it('включение: предпросмотр числа документов и обязательное подтверждение', async () => {
    serveMe(makeMe('superadmin'))
    let saved: unknown = null
    server.use(
      http.put('*/api/admin/retention', async ({ request }) => {
        saved = await request.json()
        return HttpResponse.json(makeRetention({ retention_days: 30, eligible_now: 2, can_manage: true }))
      }),
      retentionHandler({ can_manage: true }, (days) => (days <= 30 ? 2 : 0)),
      ...settingsHandlers,
    )
    renderRoute('/admin/settings')
    const user = userEvent.setup()
    await user.click(await screen.findByLabelText('Удалять документы автоматически'))
    const days = screen.getByLabelText('Срок хранения без активности, дней')
    await user.clear(days)
    await user.type(days, '30')
    expect(await screen.findByText(/Сразу после сохранения будет удалено документов: 2/)).toBeInTheDocument()
    const save = screen.getByRole('button', { name: 'Сохранить' })
    expect(save).toBeDisabled()
    await user.click(screen.getByLabelText(/Понимаю, что документы будут удалены/))
    expect(save).toBeEnabled()
    await user.click(save)
    await waitFor(() => expect(saved).toEqual({ retention_days: 30, confirm: true }))
    expect(await screen.findByText(/Включено: документы без активности дольше 30 дн\./)).toBeInTheDocument()
    expect(screen.getByText(/Ожидают удаления: 2/)).toBeInTheDocument()
  })

  it('недопустимый срок не отправляется', async () => {
    serveMe(makeMe('superadmin'))
    server.use(retentionHandler({ can_manage: true }), ...settingsHandlers)
    renderRoute('/admin/settings')
    const user = userEvent.setup()
    await user.click(await screen.findByLabelText('Удалять документы автоматически'))
    const days = screen.getByLabelText('Срок хранения без активности, дней')
    await user.clear(days)
    await user.type(days, '0')
    expect(screen.getByText('Укажите целое число от 1 до 3650')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Сохранить' })).toBeDisabled()
  })

  it('выключение не требует подтверждения', async () => {
    serveMe(makeMe('superadmin'))
    let saved: unknown = null
    server.use(
      http.put('*/api/admin/retention', async ({ request }) => {
        saved = await request.json()
        return HttpResponse.json(makeRetention({ can_manage: true }))
      }),
      retentionHandler({ retention_days: 90, eligible_now: 0, can_manage: true }),
      ...settingsHandlers,
    )
    renderRoute('/admin/settings')
    const user = userEvent.setup()
    expect(await screen.findByText(/дольше 90 дн\./)).toBeInTheDocument()
    await user.click(screen.getByLabelText('Удалять документы автоматически'))
    expect(screen.queryByLabelText(/Понимаю, что документы будут удалены/)).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Сохранить' }))
    await waitFor(() => expect(saved).toEqual({ retention_days: null, confirm: false }))
    expect(await screen.findByText(/Автоматическое удаление выключено/)).toBeInTheDocument()
  })
})
