import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import type { RedactionSettings } from '../../api/redaction'
import { makeMe, serveMe } from '../../test/auth'
import { backupHandler, retentionHandler } from '../../test/fixtures'
import { makeRedactionSettings, redactionSettingsHandler } from '../../test/redaction'
import { renderRoute } from '../../test/render'
import { server } from '../../test/server'

const settingsHandlers = [
  http.get('*/api/admin/system-settings', () => HttpResponse.json({ groups: {}, editable: false })),
  http.get('*/api/admin/llm-settings', () =>
    HttpResponse.json({ cloud_models_enabled: false, cloud_notice: { version: '1', text: 'x' }, can_edit: false }),
  ),
  backupHandler(),
  retentionHandler(),
]

function section() {
  return screen.getByRole('heading', { name: 'Маскировка данных' }).closest('section') as HTMLElement
}

describe('настройки маскировки', () => {
  it('Admin видит категории и надёжность, но не может менять', async () => {
    serveMe(makeMe('admin'))
    server.use(redactionSettingsHandler(), ...settingsHandlers)
    renderRoute('/admin/settings')
    expect(await screen.findByText('Изменять настройки маскировки может только Superadmin.')).toBeInTheDocument()
    const s = section()
    expect(within(s).getByLabelText(/ФИО/)).toBeDisabled()
    expect(within(s).getByLabelText(/ФИО/)).toBeChecked()
    expect(within(s).getByLabelText(/^сумма/)).not.toBeChecked()
    expect(within(s).getByText(/не поддерживается/)).toBeInTheDocument()
    expect(within(s).queryByRole('button', { name: 'Сохранить' })).not.toBeInTheDocument()
  })

  it('включение отправки без маскировки: предупреждение и подтверждение, отмена ничего не сохраняет', async () => {
    serveMe(makeMe('superadmin'))
    const saved: unknown[] = []
    server.use(
      redactionSettingsHandler({ can_manage: true }),
      http.put('*/api/admin/redaction', async ({ request }) => {
        const body = (await request.json()) as RedactionSettings & { confirm_allow_unmasked: boolean }
        saved.push(body)
        return HttpResponse.json(
          makeRedactionSettings({ can_manage: true, revision: 1, settings: { ...body, allow_unmasked: body.allow_unmasked } }),
        )
      }),
      ...settingsHandlers,
    )
    renderRoute('/admin/settings')
    const user = userEvent.setup()
    await user.click(await screen.findByLabelText('Разрешить пользователям отправлять текст в облако без маскировки'))
    await user.click(within(section()).getByRole('button', { name: 'Сохранить' }))
    const dialog = await screen.findByRole('dialog', { name: 'Разрешить отправку без маскировки?' })
    const allow = within(dialog).getByRole('button', { name: 'Разрешить' })
    expect(allow).toBeDisabled()
    await user.click(within(dialog).getByRole('button', { name: 'Отмена' }))
    expect(saved).toEqual([])

    await user.click(within(section()).getByRole('button', { name: 'Сохранить' }))
    const again = await screen.findByRole('dialog')
    await user.click(within(again).getByLabelText(/Понимаю последствия/))
    await user.click(within(again).getByRole('button', { name: 'Разрешить' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(saved[0]).toMatchObject({ allow_unmasked: true, confirm_allow_unmasked: true })
    expect(await screen.findByText('Настройки маскировки сохранены.')).toBeInTheDocument()
  })

  it('изменение категорий и словаря сохраняется без дополнительного подтверждения', async () => {
    serveMe(makeMe('superadmin'))
    const saved: Record<string, unknown>[] = []
    server.use(
      redactionSettingsHandler({ can_manage: true }),
      http.put('*/api/admin/redaction', async ({ request }) => {
        saved.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(makeRedactionSettings({ can_manage: true, revision: 1 }))
      }),
      ...settingsHandlers,
    )
    renderRoute('/admin/settings')
    const user = userEvent.setup()
    const s = await waitFor(() => section())
    await user.click(await within(s).findByLabelText(/^сумма/))
    expect(within(s).getByText('Маскировка ухудшает анализ')).toBeInTheDocument()
    await user.click(within(s).getAllByRole('button', { name: 'Добавить' })[0] as HTMLElement)
    await user.type(within(s).getByLabelText('Фраза'), 'Проект Полярная звезда')
    await user.type(within(s).getByLabelText(/Исключения/), 'Заказчик')
    await user.click(within(s).getByRole('button', { name: 'Сохранить' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(saved[0]).toMatchObject({
      confirm_allow_unmasked: false,
      custom_dictionary: [{ text: 'Проект Полярная звезда', category: 'goods_and_projects' }],
      allow_list: ['Заказчик'],
    })
    const categories = (saved[0] ?? {}).categories as Record<string, boolean>
    expect(categories.money_amount).toBe(true)
  })
})
