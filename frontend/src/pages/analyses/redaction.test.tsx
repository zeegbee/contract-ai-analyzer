import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import { createQueryClient } from '../../app/queryClient'
import type { AvailableModel } from '../../api/catalog'
import { makeMe, serveMe } from '../../test/auth'
import { analysisHandlers, makeAnalysis } from '../../test/fixtures'
import { type RedactionLog, redactionHandlers } from '../../test/redaction'
import { renderRoute } from '../../test/render'
import { server } from '../../test/server'
import { RedactionPreview } from './RedactionPreview'

const MODEL: AvailableModel = {
  id: 'm-cloud',
  display_name: 'Облачная модель',
  provider_name: 'OpenRouter',
  provider_type: 'openrouter',
  is_cloud: true,
  is_default: false,
  purpose: ['analysis'],
  context_window: 128000,
  pricing: null,
  consent_required: false,
}

function renderPreview(onConfirmed = vi.fn()) {
  const client = createQueryClient()
  client.setDefaultOptions({ queries: { ...client.getDefaultOptions().queries, retry: false } })
  render(
    <QueryClientProvider client={client}>
      <RedactionPreview
        documentId="d-1"
        versionId="v-1"
        model={MODEL}
        onConfirmed={onConfirmed}
        onBack={vi.fn()}
        submitting={false}
        submitError={null}
      />
    </QueryClientProvider>,
  )
  return onConfirmed
}

const newLog = (): RedactionLog => ({ created: 0, edits: [], confirms: [] })

describe('предпросмотр маскировки', () => {
  it('подсвечивает найденное, показывает текст «как увидит модель» и снимает пометку', async () => {
    const log = newLog()
    server.use(...redactionHandlers(log))
    renderPreview()
    const user = userEvent.setup()
    expect(await screen.findByText(/Будет скрыто значений: 3/)).toBeInTheDocument()
    const text = await screen.findByText(/заключили договор/)
    const paragraph = text.closest('p') as HTMLElement
    expect(within(paragraph).getByText('Иванова Ивана Петровича').tagName).toBe('MARK')

    // Как увидит модель: значения заменены метками.
    await user.click(screen.getByLabelText('Как увидит модель'))
    expect(within(paragraph).getByText('⟦ЛИЦО_1⟧')).toBeInTheDocument()
    expect(within(paragraph).queryByText('Иванова Ивана Петровича')).not.toBeInTheDocument()
    await user.click(screen.getByLabelText('Как увидит модель'))

    // Низкая уверенность — отдельный блок «Проверьте».
    const review = screen.getByRole('heading', { name: /Проверьте/ }).closest('section') as HTMLElement
    expect(within(review).getByText('7707083893')).toBeInTheDocument()

    // Снять пометку с сущности.
    await user.click(screen.getByRole('checkbox', { name: /Иванов Иван Петрович/ }))
    await waitFor(() => expect(log.edits).toEqual([{ ops: [{ op: 'set_entity', entity_id: 'E0002', masked: false }] }]))
    await waitFor(() =>
      expect(within(paragraph).getByText('Иванова Ивана Петровича')).toHaveClass('decoration-dashed'),
    )
  })

  it('подтверждение — только после отметки «проверено»; кнопки «без маскировки» нет, если она запрещена', async () => {
    const log = newLog()
    server.use(...redactionHandlers(log))
    const onConfirmed = renderPreview()
    const user = userEvent.setup()
    const confirm = await screen.findByRole('button', { name: 'Подтвердить и запустить анализ' })
    expect(confirm).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Отправить без маскировки' })).not.toBeInTheDocument()
    await user.click(screen.getByLabelText('Подсвеченные данные проверены'))
    await user.click(confirm)
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledWith('rp-1'))
    expect(log.confirms).toEqual([{ mode: 'masked', reviewed: true }])
  })

  it('отправка без маскировки: отдельное предупреждение, без отметки кнопка неактивна, отмена не отправляет запрос', async () => {
    const log = newLog()
    server.use(...redactionHandlers(log, { config: { allow_unmasked: true } }))
    const onConfirmed = renderPreview()
    const user = userEvent.setup()
    await user.click(await screen.findByLabelText('Подсвеченные данные проверены'))
    await user.click(screen.getByRole('button', { name: 'Отправить без маскировки' }))
    const dialog = await screen.findByRole('dialog', { name: 'Отправка без маскировки' })
    expect(dialog).toHaveTextContent('OpenRouter')
    expect(dialog).toHaveTextContent('ФИО: 1')
    const send = within(dialog).getByRole('button', { name: 'Отправить без маскировки' })
    expect(send).toBeDisabled()
    await user.click(within(dialog).getByRole('button', { name: 'Отмена' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(log.confirms).toEqual([])

    await user.click(screen.getByRole('button', { name: 'Отправить без маскировки' }))
    const again = await screen.findByRole('dialog')
    await user.click(within(again).getByLabelText('Понимаю, что данные будут переданы без маскировки'))
    await user.click(within(again).getByRole('button', { name: 'Отправить без маскировки' }))
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledWith('rp-1'))
    expect(log.confirms).toEqual([
      { mode: 'unmasked', reviewed: true, confirm_unmasked: true, unmasked_notice_version: 'unmasked-1' },
    ])
  })

  it('устаревший предпросмотр: подтверждение недоступно, «Проверить заново» создаёт новый', async () => {
    const log = newLog()
    server.use(...redactionHandlers(log, { preview: { stale: true, stale_reason: 'settings_changed' } }))
    renderPreview()
    const user = userEvent.setup()
    expect(await screen.findByText(/Настройки маскировки изменились/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Подтвердить и запустить анализ' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Проверить заново' }))
    await waitFor(() => expect(log.created).toBe(2))
    await waitFor(() => expect(screen.queryByText(/Настройки маскировки изменились/)).not.toBeInTheDocument())
  })

  it('ошибка поиска показывается с кнопкой повтора', async () => {
    const log = newLog()
    server.use(
      ...redactionHandlers(log, {
        preview: { status: 'failed', error_code: 'redaction_scan_timeout', error_message: 'Поиск не уложился в лимит' },
      }),
    )
    renderPreview()
    expect(await screen.findByText('Поиск не уложился в лимит')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Проверить заново' })).toBeInTheDocument()
  })
})

describe('отчёт', () => {
  it('показывает, сколько значений скрыто, или что текст отправлен без маскировки', async () => {
    serveMe(makeMe('analyst'))
    const log = newLog()
    const masked = makeAnalysis({
      redaction: {
        mode: 'masked',
        values: 3,
        occurrences: 5,
        categories: { person_name: 2, inn_org: 1 },
        unmasked_confirmed_at: null,
      },
    })
    server.use(...redactionHandlers(log), ...analysisHandlers(masked))
    renderRoute('/analyses/a-1')
    expect(await screen.findByText(/При анализе скрыто значений: 3/)).toBeInTheDocument()
    expect(await screen.findByText(/ФИО — 2, ИНН юрлица — 1/)).toBeInTheDocument()
  })

  it('без маскировки — отдельная пометка', async () => {
    serveMe(makeMe('analyst'))
    const unmasked = makeAnalysis({
      redaction: {
        mode: 'unmasked_by_user',
        values: 2,
        occurrences: 2,
        categories: {},
        unmasked_confirmed_at: '2026-10-21T10:00:00Z',
      },
    })
    server.use(...analysisHandlers(unmasked), http.get('*/api/redaction/config', () => HttpResponse.error()))
    renderRoute('/analyses/a-1')
    expect(await screen.findByText(/отправлен облачной модели без маскировки/)).toBeInTheDocument()
  })
})
