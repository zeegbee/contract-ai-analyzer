import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import type { AvailableModel, Model, Provider } from '../../api/catalog'
import { makeMe, serveMe } from '../../test/auth'
import { renderRoute } from '../../test/render'
import { server } from '../../test/server'

function makeProvider(overrides: Partial<Provider> = {}): Provider {
  return {
    id: 'p-1',
    name: 'OpenRouter',
    type: 'openrouter',
    base_url: 'https://openrouter.ai/api/v1',
    is_cloud: true,
    enabled: true,
    has_secret: true,
    secret_last4: 'cdef',
    allow_private_network: false,
    timeout_s: 120,
    max_concurrency: 2,
    extra_headers: {},
    last_check_at: null,
    last_check_status: null,
    last_check_error_safe: null,
    url_warnings: [],
    model_count: 1,
    version: 1,
    created_at: '2026-10-16T10:00:00Z',
    updated_at: '2026-10-16T10:00:00Z',
    ...overrides,
  }
}

function makeModel(overrides: Partial<Model> = {}): Model {
  return {
    id: 'm-1',
    provider_config_id: 'p-1',
    provider_name: 'OpenRouter',
    provider_type: 'openrouter',
    provider_enabled: true,
    is_cloud: true,
    display_name: 'GPT Test',
    external_model_id: 'openai/gpt-test',
    enabled: true,
    purpose: ['analysis'],
    priority: 100,
    is_default: false,
    allowed_roles: ['admin', 'analyst', 'superadmin'],
    context_window: 128000,
    max_output_tokens: null,
    generation_params: {},
    structured_output: 'unknown',
    pricing: null,
    notes: null,
    provider_metadata: null,
    last_tested_at: null,
    last_test_status: null,
    last_test_error_safe: null,
    version: 1,
    created_at: '2026-10-16T10:00:00Z',
    updated_at: '2026-10-16T10:00:00Z',
    ...overrides,
  }
}

function serveCatalog({
  providers = [makeProvider()],
  models = [makeModel()],
  canEdit = true,
}: { providers?: Provider[]; models?: Model[]; canEdit?: boolean } = {}) {
  server.use(
    http.get('*/api/admin/providers', () => HttpResponse.json({ items: providers })),
    http.get('*/api/admin/models', () => HttpResponse.json({ items: models })),
    http.get('*/api/admin/llm-settings', () =>
      HttpResponse.json({
        cloud_models_enabled: false,
        cloud_notice: { version: '1', text: 'Текст будет передан внешнему провайдеру.' },
        can_edit: canEdit,
      }),
    ),
  )
}

describe('модели и провайдеры', () => {
  it('показывает провайдеров без секрета и цену «неизвестна»', async () => {
    serveMe(makeMe('superadmin'))
    serveCatalog()
    renderRoute('/admin/models')
    expect(await screen.findByText(/ключ задан \(…cdef\)/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'GPT Test' })).toHaveAttribute('href', '/admin/models/m-1')
    expect(screen.getByText('неизвестна')).toBeInTheDocument()
    expect(screen.getByText(/Облачные модели отключены/)).toBeInTheDocument()
  })

  it('администратор не может менять провайдеров и настройки облака', async () => {
    serveMe(makeMe('admin'))
    serveCatalog({ canEdit: false })
    renderRoute('/admin/models')
    expect(await screen.findByText('Изменять эти настройки может только Superadmin.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Добавить провайдера' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Изменить' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Проверить соединение' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Разрешить облачные модели' })).not.toBeInTheDocument()
  })

  it('аналитику раздел недоступен', async () => {
    serveMe(makeMe('analyst'))
    renderRoute('/admin/models')
    expect(await screen.findByText('Недостаточно прав для просмотра этой страницы')).toBeInTheDocument()
  })

  it('отправляет ключ только при создании и не показывает его после сохранения', async () => {
    serveMe(makeMe('superadmin'))
    serveCatalog({ providers: [], models: [] })
    let body: Record<string, unknown> | null = null
    server.use(
      http.post('*/api/admin/providers', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(makeProvider({ id: 'p-2', name: 'Мой OpenRouter' }), { status: 201 })
      }),
    )
    renderRoute('/admin/models')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Добавить провайдера' }))
    const form = screen.getByRole('form', { name: 'Добавить провайдера' })
    await user.type(within(form).getByLabelText('Название'), 'Мой OpenRouter')
    const key = within(form).getByLabelText('API-ключ')
    expect(key).toHaveAttribute('type', 'password')
    await user.type(key, 'sk-or-v1-secret-value-123')
    await user.click(within(form).getByRole('button', { name: 'Сохранить' }))
    await waitFor(() => expect(body).not.toBeNull())
    expect(body).toMatchObject({ name: 'Мой OpenRouter', type: 'openrouter', api_key: 'sk-or-v1-secret-value-123' })
    await waitFor(() => expect(screen.queryByRole('form', { name: 'Добавить провайдера' })).not.toBeInTheDocument())
    expect(screen.queryByDisplayValue('sk-or-v1-secret-value-123')).not.toBeInTheDocument()
  })

  it('показывает ошибку SSRF-проверки адреса', async () => {
    serveMe(makeMe('superadmin'))
    serveCatalog({ providers: [], models: [] })
    server.use(
      http.post('*/api/admin/providers', () =>
        HttpResponse.json(
          {
            error: {
              code: 'metadata_blocked',
              message: 'Адреса metadata-сервисов запрещены',
              request_id: 'req-9',
              details: {},
            },
          },
          { status: 422 },
        ),
      ),
    )
    renderRoute('/admin/models')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Добавить провайдера' }))
    const form = screen.getByRole('form', { name: 'Добавить провайдера' })
    await user.type(within(form).getByLabelText('Название'), 'X')
    await user.type(within(form).getByLabelText('Адрес API'), 'https://169.254.169.254')
    await user.click(within(form).getByRole('button', { name: 'Сохранить' }))
    expect(await screen.findByText('Адреса metadata-сервисов запрещены')).toBeInTheDocument()
    expect(screen.getByText('req-9')).toBeInTheDocument()
  })

  it('проверка соединения показывает результат', async () => {
    serveMe(makeMe('admin'))
    serveCatalog({ canEdit: false })
    server.use(
      http.post('*/api/admin/providers/p-1/check', () =>
        HttpResponse.json({
          provider: makeProvider({ last_check_status: 'provider_auth_failed' }),
          health: {
            ok: false,
            status: 'provider_auth_failed',
            message: 'Провайдер отклонил ключ доступа (проверьте API-ключ)',
            details: {},
          },
        }),
      ),
    )
    renderRoute('/admin/models')
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Проверить соединение' }))
    expect(await screen.findByText(/Провайдер отклонил ключ доступа/)).toBeInTheDocument()
  })
})

describe('страница модели', () => {
  it('проверка с генерацией и запрет ручного json_schema', async () => {
    serveMe(makeMe('admin'))
    let current = makeModel()
    server.use(
      http.get('*/api/admin/models/m-1', () => HttpResponse.json(current)),
      http.get('*/api/admin/config-revisions', () => HttpResponse.json({ items: [] })),
      http.post('*/api/admin/models/m-1/test', async ({ request }) => {
        const { generate } = (await request.json()) as { generate: boolean }
        current = makeModel({ structured_output: 'json_schema', version: 2 })
        return HttpResponse.json({
          ok: true,
          model: current,
          steps: [
            { name: 'describe', ok: true, code: 'ok', message: 'Модель найдена у провайдера' },
            ...(generate
              ? [{ name: 'generate', ok: true, code: 'json_schema', message: 'Генерация по JSON Schema подтверждена' }]
              : []),
          ],
        })
      }),
    )
    renderRoute('/admin/models/m-1')
    const option = await screen.findByRole('option', { name: 'JSON Schema (подтверждено проверкой)' })
    expect(option).toBeDisabled()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Проверить с генерацией' }))
    expect(await screen.findByText(/Генерация по JSON Schema подтверждена/)).toBeInTheDocument()
    expect(await screen.findByRole('option', { name: 'JSON Schema (подтверждено проверкой)' })).toBeEnabled()
  })

  it('сохраняет изменения с версией и отключает модель', async () => {
    serveMe(makeMe('admin'))
    let body: Record<string, unknown> | null = null
    server.use(
      http.get('*/api/admin/models/m-1', () => HttpResponse.json(makeModel())),
      http.get('*/api/admin/config-revisions', () => HttpResponse.json({ items: [] })),
      http.patch('*/api/admin/models/m-1', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(makeModel({ enabled: false, version: 2 }))
      }),
    )
    renderRoute('/admin/models/m-1')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('checkbox', { name: 'Включена' }))
    await user.click(screen.getByRole('button', { name: 'Сохранить' }))
    await waitFor(() => expect(body).not.toBeNull())
    expect(body).toMatchObject({ version: 1, enabled: false })
    expect(body).not.toHaveProperty('structured_output')
    expect(body).not.toHaveProperty('pricing')
  })

  it.each([
    { saved: {}, shown: 'on', choose: 'выключен', sent: { temperature: 0.2, reasoning: 'off' } },
    { saved: { reasoning: 'off' }, shown: 'off', choose: 'включён', sent: { temperature: 0.2 } },
  ])('режим рассуждений: $shown → $choose', async ({ saved, shown, choose, sent }) => {
    serveMe(makeMe('admin'))
    let body: Record<string, unknown> | null = null
    server.use(
      http.get('*/api/admin/models/m-1', () =>
        HttpResponse.json(makeModel({ generation_params: { temperature: 0.2, ...saved } })),
      ),
      http.get('*/api/admin/config-revisions', () => HttpResponse.json({ items: [] })),
      http.patch('*/api/admin/models/m-1', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(makeModel({ version: 2 }))
      }),
    )
    renderRoute('/admin/models/m-1')
    const user = userEvent.setup()
    const select = await screen.findByLabelText('Режим рассуждений')
    expect(select).toHaveValue(shown)
    expect(screen.getAllByRole('option', { name: /^(включён|выключен)$/ })).toHaveLength(2)
    await user.selectOptions(select, choose)
    await user.click(screen.getByRole('button', { name: 'Сохранить' }))
    await waitFor(() => expect(body).not.toBeNull())
    expect((body as unknown as { generation_params: unknown }).generation_params).toEqual(sent)
  })
})

function available(items: AvailableModel[], cloudEnabled = true) {
  return HttpResponse.json({
    items,
    cloud_models_enabled: cloudEnabled,
    cloud_notice: { version: '3', text: 'Фрагменты договора будут переданы внешнему провайдеру.' },
  })
}

const cloudModel: AvailableModel = {
  id: 'm-cloud',
  display_name: 'Облачная',
  provider_name: 'OpenRouter',
  provider_type: 'openrouter',
  is_cloud: true,
  is_default: false,
  purpose: ['analysis'],
  context_window: null,
  pricing: null,
  consent_required: true,
}

describe('модели в профиле', () => {
  it('показывает списком модели с сервера, без выбора и подтверждения', async () => {
    serveMe(makeMe('analyst'))
    server.use(
      http.get('*/api/auth/sessions', () => HttpResponse.json([])),
      http.get('*/api/models/available', () =>
        available([
          { ...cloudModel, id: 'm-local', display_name: 'Локальная', provider_name: 'Ollama', provider_type: 'ollama', is_cloud: false, consent_required: false },
          cloudModel,
        ]),
      ),
    )
    renderRoute('/account')
    const section = (await screen.findByRole('heading', { name: 'Модели, доступные мне для анализа' })).closest(
      'section',
    ) as HTMLElement
    const items = await within(section).findAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('Локальная')
    expect(items[0]).toHaveTextContent('локальная')
    expect(items[1]).toHaveTextContent('Облачная')
    expect(items[1]).toHaveTextContent('облачная')
    expect(items[1]).toHaveTextContent('OpenRouter')
    expect(within(section).queryByRole('combobox')).not.toBeInTheDocument()
    expect(within(section).queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('пустой список и отключённое облако', async () => {
    serveMe(makeMe('analyst'))
    server.use(
      http.get('*/api/auth/sessions', () => HttpResponse.json([])),
      http.get('*/api/models/available', () => available([], false)),
    )
    renderRoute('/account')
    expect(await screen.findByText('Нет доступных моделей. Обратитесь к администратору.')).toBeInTheDocument()
    expect(screen.getByText('Облачные модели отключены администратором.')).toBeInTheDocument()
  })
})
