import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { makeMe, serveMe } from '../../test/auth'
import {
  analysisHandlers,
  availableModels,
  cloudModel,
  localModel,
  makeAnalysis,
  makeDocument,
  makeEvidence,
  makeRisk,
  backupHandler,
  retentionHandler,
  RISKS,
} from '../../test/fixtures'
import { renderRoute } from '../../test/render'
import { server } from '../../test/server'

const apiError = (status: number, code: string, message: string, requestId = 'req-42') =>
  HttpResponse.json({ error: { code, message, request_id: requestId, details: {} } }, { status })

function serveForm(models: Record<string, unknown>[] = [localModel, cloudModel]) {
  const doc = makeDocument()
  server.use(
    http.get('*/api/documents', () => HttpResponse.json({ items: [doc], next_cursor: null })),
    http.get('*/api/documents/d-1', () => HttpResponse.json(doc)),
    http.get('*/api/models/available', () => availableModels(models)),
  )
}

describe('запуск анализа', () => {
  it('проверяет поля, требует облачное согласие, показывает оценку и запускает с Idempotency-Key', async () => {
    serveMe(makeMe('analyst'))
    serveForm()
    document.cookie = 'cai_csrf=csrf-1; path=/'
    let consent: unknown = null
    let estimateBody: Record<string, unknown> | null = null
    let createBody: Record<string, unknown> | null = null
    let headers: Headers | null = null
    server.use(
      http.post('*/api/models/m-cloud/cloud-consent', async ({ request }) => {
        consent = await request.json()
        return HttpResponse.json({ provider_config_id: 'p', notice_version: '3', accepted_at: '2026-10-17T10:00:00Z' })
      }),
      http.post('*/api/analyses/estimate', async ({ request }) => {
        estimateBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({
          chunks: 3,
          estimated_input_tokens: 20000,
          estimated_output_tokens: 30000,
          estimated_tokens: 50000,
          token_limit: 1500000,
          within_limit: true,
          estimated_cost: '0.0210',
          cost_currency: 'USD',
          pricing_source: 'provider_api',
          cloud_transfer: true,
          note: 'Оценка по длине текста',
        })
      }),
      http.post('*/api/analyses', async ({ request }) => {
        createBody = (await request.json()) as Record<string, unknown>
        headers = request.headers
        return HttpResponse.json(makeAnalysis({ status: 'queued', cloud_transfer: true }), { status: 201 })
      }),
      ...analysisHandlers(makeAnalysis({ status: 'queued', summary: null, coverage: null })),
    )
    renderRoute('/analyses/new?document=d-1')
    const user = userEvent.setup()
    expect(await screen.findByRole('heading', { name: 'Новый анализ' })).toBeInTheDocument()
    expect(await screen.findByRole('option', { name: 'Договор поставки' })).toBeInTheDocument()

    // Без модели — ошибки формы, запрос не отправляется.
    await user.click(screen.getByRole('button', { name: 'Запустить анализ' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Выберите модель')
    expect(createBody).toBeNull()

    await user.click(screen.getByRole('radio', { name: /Указанная сторона/ }))
    await user.type(screen.getByLabelText('Наименование стороны'), 'ООО «Бета»')
    await user.click(screen.getByRole('button', { name: 'Снять все' }))
    await user.click(screen.getByRole('checkbox', { name: 'Финансы и оплата' }))
    await user.click(screen.getByRole('checkbox', { name: 'Противоречия' }))
    await user.click(screen.getByRole('radio', { name: /Углублённый/ }))
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Модель' }), 'm-cloud')
    expect(screen.getByText('Фрагменты договора будут переданы внешнему провайдеру.')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Проверить модель и объём' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Подтвердите передачу текста')
    expect(estimateBody).toBeNull()

    await user.click(screen.getByRole('checkbox', { name: /согласен на передачу/ }))
    await waitFor(() => expect(consent).toEqual({ notice_version: '3' }))
    await user.click(screen.getByRole('button', { name: 'Проверить модель и объём' }))
    expect(await screen.findByText(/Частей текста: 3/)).toBeInTheDocument()
    expect(screen.getByText(/≈ 0.0210 USD/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Запустить анализ' }))
    await waitFor(() => expect(createBody).not.toBeNull())
    expect(createBody).toEqual({
      document_id: 'd-1',
      version_id: null,
      party: 'named',
      party_name: 'ООО «Бета»',
      categories: ['payment', 'contradiction'],
      depth: 'deep',
      model_id: 'm-cloud',
      cloud_consent: true,
    })
    expect(estimateBody).toEqual(createBody)
    const sent = headers as Headers | null
    expect(sent?.get('Idempotency-Key')).toMatch(/^[0-9a-f]{32}$/)
    expect(sent?.get('X-CSRF-Token')).toBe('csrf-1')
    // После запуска — страница анализа с прогрессом.
    expect(await screen.findByRole('heading', { name: 'Договор поставки' })).toBeInTheDocument()
    expect(screen.getByText(/Ход анализа/)).toBeInTheDocument()
  })

  it('оценка выше лимита блокирует запуск; ошибка сервера показывается с кодом запроса', async () => {
    serveMe(makeMe('analyst'))
    serveForm([localModel])
    server.use(
      http.post('*/api/analyses/estimate', () =>
        HttpResponse.json({
          chunks: 40,
          estimated_input_tokens: 1_000_000,
          estimated_output_tokens: 900_000,
          estimated_tokens: 1_900_000,
          token_limit: 1_500_000,
          within_limit: false,
          estimated_cost: null,
          cost_currency: null,
          pricing_source: null,
          cloud_transfer: false,
          note: 'Оценка',
        }),
      ),
    )
    renderRoute('/analyses/new?document=d-1')
    const user = userEvent.setup()
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Модель' }), 'm-local')
    await user.click(screen.getByRole('button', { name: 'Проверить модель и объём' }))
    expect(await screen.findByText(/превышает лимит токенов/)).toBeInTheDocument()
    expect(screen.getByText(/неизвестна \(цена модели не задана\)/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Запустить анализ' })).toBeDisabled()

    server.use(
      http.post('*/api/analyses/estimate', () =>
        apiError(409, 'cloud_models_disabled', 'Облачные модели отключены администратором'),
      ),
    )
    await user.click(screen.getByRole('radio', { name: /Быстрый/ }))
    await user.click(screen.getByRole('button', { name: 'Проверить модель и объём' }))
    const alert = await screen.findByText('Облачные модели отключены администратором')
    expect(alert.closest('[role="alert"]')).toHaveTextContent('req-42')
  })

  it('наблюдатель не может запускать анализ', async () => {
    serveMe(makeMe('viewer'))
    renderRoute('/analyses/new')
    expect(await screen.findByRole('alert')).toHaveTextContent('Недостаточно прав')
  })
})

describe('страница анализа', () => {
  it('показывает прогресс через polling и затем результаты', async () => {
    serveMe(makeMe('analyst'))
    let calls = 0
    server.use(
      http.get('*/api/analyses/a-1', () => {
        calls += 1
        return HttpResponse.json(
          calls === 1
            ? makeAnalysis({
                status: 'running',
                progress: { stage: 'chunks', chunks_total: 4, chunks_done: 1, risks: null, retry_scheduled: null },
                summary: null,
                coverage: null,
                can_cancel: true,
              })
            : makeAnalysis(),
        )
      }),
      http.get('*/api/analyses/a-1/risks', () => HttpResponse.json({ items: RISKS, total: 4, hidden_rejected: 0 })),
      http.get('*/api/analyses/a-1/exports', () => HttpResponse.json({ items: [] })),
    )
    renderRoute('/analyses/a-1')
    expect(await screen.findByText(/Анализ частей текста/)).toBeInTheDocument()
    const bar = screen.getByRole('progressbar', { name: 'Части текста' })
    expect(bar).toHaveAttribute('value', '1')
    expect(bar).toHaveAttribute('max', '4')
    expect(screen.getByRole('button', { name: 'Отменить анализ' })).toBeInTheDocument()
    expect(await screen.findByText('Одностороннее изменение цены', {}, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.getByText('Рисков с подтверждёнными цитатами: 1; требуют проверки: 2.')).toBeInTheDocument()
  }, 10_000)

  it('различает статусы доказательств и показывает область поиска отсутствующего условия', async () => {
    serveMe(makeMe('analyst'))
    server.use(...analysisHandlers(makeAnalysis()))
    renderRoute('/analyses/a-1')
    const verified = (await screen.findByRole('heading', { name: 'Одностороннее изменение цены' })).closest('article')!
    expect(within(verified).getByText('Подтверждён')).toBeInTheDocument()
    expect(within(verified).getByText('Цитата совпадает дословно')).toBeInTheDocument()
    expect(within(verified).getByText('Подтверждает риск')).toBeInTheDocument()
    expect(within(verified).getByText('стр. 1, п. 2.2')).toBeInTheDocument()
    expect(within(verified).getByText(/Проект редакции/)).toHaveTextContent('не готовый юридический текст')
    expect(within(verified).getByText(/0,80 \(субъективная оценка модели/)).toBeInTheDocument()
    expect(within(verified).getByRole('link', { name: 'Открыть в тексте' })).toHaveAttribute(
      'href',
      '/documents/d-1?fragment=F000006&page=1',
    )

    const partial = screen.getByRole('heading', { name: 'Ограничение ответственности поставщика' }).closest('article')!
    expect(within(partial).getByText('Требует проверки')).toBeInTheDocument()
    expect(within(partial).getByText('Подтверждает частично — требует проверки')).toBeInTheDocument()
    expect(within(partial).getByText(/цитата подтверждает риск лишь частично/)).toBeInTheDocument()
    expect(within(partial).getByText(/требует проверки юристом/)).toBeInTheDocument()

    const missing = screen.getByRole('heading', { name: 'Нет условия о форс-мажоре' }).closest('article')!
    expect(within(missing).getByText('Отсутствующее условие')).toBeInTheDocument()
    expect(within(missing).getByText(/Отсутствие условия не может быть доказано цитатой/)).toBeInTheDocument()
    expect(within(missing).getByText('Просмотрены разделы 1–4')).toBeInTheDocument()
    expect(within(missing).getByText(/включая приложения/)).toBeInTheDocument()
    expect(within(missing).queryByText('Цитаты не приведены')).not.toBeInTheDocument()

    // Отклонённый риск скрыт по умолчанию и помечен как не подтверждённый.
    expect(screen.queryByText('Выдуманное условие о штрафе')).not.toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('checkbox', { name: /Показывать отклонённые/ }))
    const rejected = screen.getByRole('heading', { name: 'Выдуманное условие о штрафе' }).closest('article')!
    expect(within(rejected).getByText('Не подтверждён')).toBeInTheDocument()
    expect(within(rejected).getByText(/Не является установленным фактом/)).toBeInTheDocument()
    expect(within(rejected).getByText('Цитата не найдена в тексте')).toBeInTheDocument()
  })

  it('фильтрует риски по уровню, категории, стороне и статусу проверки', async () => {
    serveMe(makeMe('analyst'))
    server.use(...analysisHandlers(makeAnalysis()))
    renderRoute('/analyses/a-1')
    const user = userEvent.setup()
    await screen.findByRole('heading', { name: 'Одностороннее изменение цены' })
    const articles = () => screen.queryAllByRole('article')
    expect(articles()).toHaveLength(3)
    expect(screen.getByText('Показано: 3 / 4')).toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText('Уровень'), 'medium')
    expect(articles().map((a) => within(a).getByRole('heading', { level: 3 }).textContent)).toEqual([
      'Ограничение ответственности поставщика',
    ])
    await user.selectOptions(screen.getByLabelText('Уровень'), '')
    await user.selectOptions(screen.getByLabelText('Затрагивает сторону'), 'party_a')
    expect(articles()).toHaveLength(2)
    await user.selectOptions(screen.getByLabelText('Категория'), 'force_majeure')
    expect(articles().map((a) => within(a).getByRole('heading', { level: 3 }).textContent)).toEqual(['Нет условия о форс-мажоре'])
    await user.click(screen.getByRole('button', { name: 'Сбросить фильтры' }))
    await user.selectOptions(screen.getByLabelText('Статус проверки'), 'rejected')
    expect(articles().map((a) => within(a).getByRole('heading', { level: 3 }).textContent)).toEqual(['Выдуманное условие о штрафе'])
    await user.selectOptions(screen.getByLabelText('Статус проверки'), 'verified_match')
    await user.selectOptions(screen.getByLabelText('Уровень'), 'low')
    expect(screen.getByText('Нет рисков, подходящих под фильтры.')).toBeInTheDocument()
  })

  it('длинная цитата сворачивается, текст модели не интерпретируется как разметка', async () => {
    serveMe(makeMe('analyst'))
    const long = `${'Очень длинное условие договора. '.repeat(40)}конец цитаты`
    const risk = makeRisk({
      title: '<img src=x onerror=alert(1)>',
      evidence: [makeEvidence({ quote: long })],
    })
    server.use(...analysisHandlers(makeAnalysis(), [risk]))
    const { container } = renderRoute('/analyses/a-1')
    const heading = await screen.findByRole('heading', { name: '<img src=x onerror=alert(1)>' })
    expect(container.querySelector('img')).toBeNull()
    const article = heading.closest('article')!
    expect(within(article).queryByText(/конец цитаты/)).not.toBeInTheDocument()
    const toggle = within(article).getByRole('button', { name: 'Показать полностью' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await userEvent.setup().click(toggle)
    expect(within(article).getByText(/конец цитаты/)).toBeInTheDocument()
    expect(within(article).getByRole('button', { name: 'Свернуть' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('пустой результат не утверждает, что договор безопасен', async () => {
    serveMe(makeMe('analyst'))
    server.use(...analysisHandlers(makeAnalysis(), []))
    renderRoute('/analyses/a-1')
    expect(await screen.findByText(/Это не означает, что договор безопасен/)).toBeInTheDocument()
  })

  it('ошибка загрузки показывает безопасное сообщение и код запроса', async () => {
    serveMe(makeMe('analyst'))
    server.use(http.get('*/api/analyses/a-1', () => apiError(404, 'analysis_not_found', 'Анализ не найден', 'req-404')))
    renderRoute('/analyses/a-1')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Анализ не найден')
    expect(alert).toHaveTextContent('req-404')
  })

  it('неудачный облачный анализ: причина и повтор с повторным согласием', async () => {
    serveMe(makeMe('analyst'))
    const failed = makeAnalysis({
      status: 'failed',
      error_code: 'provider_unavailable',
      error_message: 'Провайдер модели временно недоступен. Повторите анализ позже.',
      cloud_transfer: true,
      can_retry: true,
      summary: null,
      coverage: null,
    })
    let retryBody: unknown = null
    const queued = { ...failed, status: 'queued' as const, can_retry: false, error_code: null, error_message: null }
    server.use(
      http.get('*/api/analyses/a-1', () => HttpResponse.json(retryBody ? queued : failed)),
      http.post('*/api/analyses/a-1/retry', async ({ request }) => {
        retryBody = await request.json()
        return HttpResponse.json(queued)
      }),
      ...analysisHandlers(failed),
    )
    renderRoute('/analyses/a-1')
    expect(await screen.findByText(/Провайдер модели временно недоступен/)).toBeInTheDocument()
    const user = userEvent.setup()
    const retry = screen.getByRole('button', { name: 'Повторить анализ' })
    expect(retry).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: /повторную передачу/ }))
    await user.click(retry)
    await waitFor(() => expect(retryBody).toEqual({ cloud_consent: true }))
    expect(await screen.findByText('В очереди', { selector: 'span' })).toBeInTheDocument()
  })

  it('наблюдатель видит отчёт, но без отмены и повтора', async () => {
    serveMe(makeMe('viewer'))
    server.use(...analysisHandlers(makeAnalysis({ status: 'running', can_cancel: false, summary: null, coverage: null })))
    renderRoute('/analyses/a-1')
    expect(await screen.findByText(/Ход анализа/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Отменить анализ' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Повторить анализ' })).not.toBeInTheDocument()
  })
})

describe('экспорт', () => {
  it('создаёт экспорт, ждёт готовности и даёт ссылку на скачивание', async () => {
    serveMe(makeMe('viewer'))
    let created: unknown = null
    let listCalls = 0
    const queued = {
      id: 'e1e1e1e1-0000-4000-8000-000000000001',
      analysis_id: 'a-1',
      format: 'pdf',
      status: 'queued',
      created_at: '2026-10-17T11:00:00Z',
      size_bytes: null,
      expires_at: null,
      error_code: null,
      download_url: null,
    }
    server.use(
      http.post('*/api/analyses/a-1/exports', async ({ request }) => {
        created = await request.json()
        return HttpResponse.json(queued, { status: 202 })
      }),
      http.get('*/api/analyses/a-1/exports', () => {
        listCalls += 1
        if (!created) return HttpResponse.json({ items: [] })
        return HttpResponse.json({
          items: [
            listCalls < 3
              ? queued
              : {
                  ...queued,
                  status: 'ready',
                  size_bytes: 20480,
                  expires_at: '2026-10-24T11:00:00Z',
                  download_url: '/api/exports/e1e1e1e1-0000-4000-8000-000000000001/download',
                },
          ],
        })
      }),
      ...analysisHandlers(makeAnalysis()),
    )
    renderRoute('/analyses/a-1')
    expect(await screen.findByText('Экспортов ещё нет.')).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Отчёт PDF' }))
    await waitFor(() => expect(created).toEqual({ format: 'pdf' }))
    expect(await screen.findByText(/· формируется…/)).toBeInTheDocument()
    const link = await screen.findByRole('link', { name: 'Скачать' }, { timeout: 6000 })
    expect(link).toHaveAttribute('href', '/api/exports/e1e1e1e1-0000-4000-8000-000000000001/download')
    expect(screen.getByText(/20 КБ/)).toBeInTheDocument()
  }, 10_000)

  it('не превращает подменённую ссылку скачивания в href', async () => {
    serveMe(makeMe('viewer'))
    server.use(
      http.get('*/api/analyses/a-1/exports', () =>
        HttpResponse.json({
          items: [
            {
              id: 'e1e1e1e1-0000-4000-8000-000000000002',
              analysis_id: 'a-1',
              format: 'pdf',
              status: 'ready',
              created_at: '2026-10-17T11:00:00Z',
              size_bytes: 10,
              expires_at: null,
              error_code: null,
              download_url: 'javascript:alert(1)',
            },
          ],
        }),
      ),
      ...analysisHandlers(makeAnalysis()),
    )
    renderRoute('/analyses/a-1')
    expect(await screen.findByRole('heading', { name: 'Экспорт' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByRole('alert').length).toBeGreaterThan(0))
    expect(screen.queryByRole('link', { name: 'Скачать' })).not.toBeInTheDocument()
  })

  it('экспорт недоступен до завершения анализа', async () => {
    serveMe(makeMe('analyst'))
    server.use(...analysisHandlers(makeAnalysis({ status: 'running', summary: null, coverage: null })))
    renderRoute('/analyses/a-1')
    expect(await screen.findByText('Экспорт доступен после завершения анализа.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Отчёт PDF' })).not.toBeInTheDocument()
  })
})

describe('списки и обзор', () => {
  it('обзор показывает выполняющиеся анализы и ссылку на запуск', async () => {
    serveMe(makeMe('analyst'))
    server.use(
      http.get('*/api/analyses', () =>
        HttpResponse.json({
          items: [
            makeAnalysis({ id: 'a-2', status: 'running', progress: { stage: 'chunks', chunks_total: 3, chunks_done: 1, risks: null, retry_scheduled: null } }),
            makeAnalysis(),
          ],
          next_cursor: null,
        }),
      ),
      http.get('*/api/documents', () => HttpResponse.json({ items: [], next_cursor: null })),
    )
    renderRoute('/')
    expect(await screen.findByRole('heading', { name: 'Выполняющиеся анализы' })).toBeInTheDocument()
    expect(screen.getAllByText('Анализ частей текста · 1/3').length).toBeGreaterThan(0)
    expect(screen.getByRole('link', { name: 'Новый анализ' })).toHaveAttribute('href', '/analyses/new')
    expect(screen.getByText(/Договоров пока нет/)).toBeInTheDocument()
  })

  it('список анализов фильтрует по статусу; наблюдатель не видит запуска', async () => {
    serveMe(makeMe('viewer'))
    const statuses: (string | null)[] = []
    server.use(
      http.get('*/api/analyses', ({ request }) => {
        statuses.push(new URL(request.url).searchParams.get('status'))
        return HttpResponse.json({ items: [makeAnalysis()], next_cursor: null })
      }),
    )
    renderRoute('/analyses')
    expect(await screen.findByRole('link', { name: 'Договор поставки' })).toHaveAttribute('href', '/analyses/a-1')
    expect(screen.queryByRole('link', { name: 'Новый анализ' })).not.toBeInTheDocument()
    await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'Статус' }), 'failed')
    await waitFor(() => expect(statuses).toContain('failed'))
  })
})

describe('карточка договора', () => {
  const fragmentsHandler = http.get('*/api/documents/d-1/versions/v-1/fragments', () =>
    HttpResponse.json({
      items: [
        {
          id: 'f-6',
          seq: 6,
          fragment_key: 'F000006',
          kind: 'list_item',
          text: 'Поставщик вправе изменить цену',
          char_start: 0,
          char_end: 30,
          page_start: 1,
          page_end: 1,
          clause_ref: '2.2',
          heading_path: [],
          table_ref: null,
          meta: {},
        },
      ],
      next_after_seq: null,
      total: 1,
    }),
  )

  it('показывает анализы договора, запуск и переход к фрагменту из карточки риска', async () => {
    serveMe(makeMe('analyst'))
    let documentFilter: string | null = null
    server.use(
      http.get('*/api/documents/d-1', () => HttpResponse.json(makeDocument({ can_share: false }))),
      http.get('*/api/documents/d-1/versions/v-1/pages', () =>
        HttpResponse.json({ items: [], page_source: 'pdf_native', page_count: 2 }),
      ),
      fragmentsHandler,
      http.get('*/api/analyses', ({ request }) => {
        documentFilter = new URL(request.url).searchParams.get('document_id')
        return HttpResponse.json({ items: [makeAnalysis()], next_cursor: null })
      }),
    )
    renderRoute('/documents/d-1?fragment=F000006&page=1')
    expect(await screen.findByRole('link', { name: 'Запустить анализ' })).toHaveAttribute(
      'href',
      '/analyses/new?document=d-1',
    )
    expect(await screen.findByRole('link', { name: 'Завершён' })).toHaveAttribute('href', '/analyses/a-1')
    expect(documentFilter).toBe('d-1')
    const fragment = (await screen.findByText('Поставщик вправе изменить цену')).closest('li')!
    expect(fragment).toHaveAttribute('aria-current', 'true')
    await waitFor(() => expect(fragment).toHaveFocus())
  })

  it('без права анализа кнопки запуска нет', async () => {
    serveMe(makeMe('viewer'))
    server.use(
      http.get('*/api/documents/d-1', () =>
        HttpResponse.json(makeDocument({ can_analyze: false, can_manage: false, can_share: false, access: 'view' })),
      ),
      http.get('*/api/documents/d-1/versions/v-1/pages', () =>
        HttpResponse.json({ items: [], page_source: 'pdf_native', page_count: 2 }),
      ),
      fragmentsHandler,
      http.get('*/api/analyses', () => HttpResponse.json({ items: [], next_cursor: null })),
    )
    renderRoute('/documents/d-1')
    expect(await screen.findByText('Анализов этого договора пока нет.')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Запустить анализ' })).not.toBeInTheDocument()
  })
})

describe('настройки', () => {
  it('показывает системные параметры только для чтения', async () => {
    serveMe(makeMe('admin'))
    server.use(
      http.get('*/api/admin/system-settings', () =>
        HttpResponse.json({
          groups: { analysis: [{ key: 'analysis_max_tokens', value: 1500000, env: 'ANALYSIS_MAX_TOKENS' }] },
          editable: false,
        }),
      ),
      http.get('*/api/admin/llm-settings', () =>
        HttpResponse.json({ cloud_models_enabled: false, cloud_notice: { version: '3', text: 'x' }, can_edit: false }),
      ),
      retentionHandler(),
      backupHandler(),
    )
    renderRoute('/admin/settings')
    expect(await screen.findByText('Лимит токенов на анализ')).toBeInTheDocument()
    expect(screen.getByText('1 500 000')).toBeInTheDocument()
    expect(screen.getByText('Облачные модели выключены')).toBeInTheDocument()
    // Admin ничего не редактирует: поля резервного копирования только для чтения.
    expect(await screen.findByText('Изменять параметры может только Superadmin.')).toBeInTheDocument()
    for (const field of screen.queryAllByRole('textbox')) expect(field).toBeDisabled()
  })

  it('аналитику настройки недоступны', async () => {
    serveMe(makeMe('analyst'))
    renderRoute('/admin/settings')
    expect(await screen.findByRole('alert')).toHaveTextContent('Недостаточно прав')
  })
})
