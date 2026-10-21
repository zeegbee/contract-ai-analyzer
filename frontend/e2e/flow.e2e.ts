/**
 * Сквозной сценарий на чистом стенде (e2e/run.sh):
 * bootstrap → вход → обязательная смена пароля → провайдер и модель (mock-сервер с API Ollama) →
 * загрузка PDF → извлечение текста → анализ → серверная проверка доказательств → экспорт.
 * Попутно: заголовки безопасности, аудит доступности (axe), отсутствие текста договора в аудите.
 */
import { readFileSync } from 'node:fs'

import { expect, test, type Page } from '@playwright/test'

import { apiCall, apiGet, env, expectAccessible, login, saveState, sha256 } from './helpers'

test.describe.configure({ mode: 'serial' })

const UNILATERAL = 'Одностороннее изменение цены'
const PENALTY = 'Высокая неустойка'
const FABRICATED = 'Удержание оплаты'
const MISSING = 'Не определён порядок приёмки'

interface Evidence {
  quote: string
  fragment_key: string | null
  match_status: string
  relevance_status: string
  page_start: number | null
  clause_ref: string | null
  superseded: boolean
}
interface Risk {
  title: string
  kind: string
  validation_status: string
  evidence: Evidence[]
}
interface Fragment {
  fragment_key: string
  text: string
}

test('health, заголовки безопасности и закрытый API без сессии', async ({ request }) => {
  const ready = await request.get('/api/health/ready')
  expect(ready.status()).toBe(200)

  const spa = await request.get('/login')
  const csp = spa.headers()['content-security-policy'] ?? ''
  expect(csp).toContain("script-src 'self'")
  expect(csp).toContain("frame-ancestors 'none'")
  expect(csp).not.toContain('unsafe-inline')
  expect(spa.headers()['x-frame-options']).toBe('DENY')
  expect(spa.headers()['x-content-type-options']).toBe('nosniff')

  const api = await request.get('/api/documents')
  expect(api.status()).toBe(401)
  expect(api.headers()['cache-control']).toBe('no-store')
  expect(api.headers()['content-security-policy']).toContain("default-src 'none'")

  // Без Origin и CSRF-токена вход отклоняется (защита login CSRF).
  const forged = await request.post('/api/auth/login', {
    data: { email: env.email, password: env.bootstrapPassword },
    headers: { Origin: 'https://evil.example' },
  })
  expect(forged.status()).toBe(403)
})

test('полный сценарий: от bootstrap до экспорта', async ({ page }) => {
  // --- вход Superadmin из bootstrap и обязательная смена пароля
  await page.goto('/login')
  await expectAccessible(page, 'вход')
  await login(page, env.bootstrapPassword)
  await expect(page).toHaveURL(/\/change-password$/)
  await page.getByLabel('Текущий пароль').fill(env.bootstrapPassword)
  await page.getByLabel('Новый пароль', { exact: true }).fill(env.newPassword)
  await page.getByLabel('Повторите новый пароль').fill(env.newPassword)
  await page.getByRole('button', { name: 'Сменить пароль' }).click()
  await expect(page).toHaveURL(/\/$/)
  await expectAccessible(page, 'обзор')

  // --- провайдер Ollama (mock) в частной сети и модель; настройка — через админ-API, как в UI
  const provider = await apiCall(page, 'POST', '/api/admin/providers', {
    name: 'E2E mock Ollama',
    type: 'ollama',
    base_url: 'http://mock-llm:11434',
    allow_private_network: true,
    enabled: true,
    timeout_s: 60,
  })
  expect(provider.status(), await provider.text()).toBe(201)
  const providerId = ((await provider.json()) as { id: string }).id
  const check = await apiCall(page, 'POST', `/api/admin/providers/${providerId}/check`)
  expect(check.status(), await check.text()).toBe(200)

  const created = await apiCall(page, 'POST', '/api/admin/models', {
    provider_config_id: providerId,
    external_model_id: 'e2e-mock:latest',
    display_name: 'E2E mock',
    context_window: 32768,
    max_output_tokens: 4096,
  })
  expect(created.status(), await created.text()).toBe(201)
  const modelId = ((await created.json()) as { id: string }).id
  const probe = await apiCall(page, 'POST', `/api/admin/models/${modelId}/test`, { generate: true })
  expect(probe.status(), await probe.text()).toBe(200)
  const model = await apiGet<{ version: number; structured_output: string }>(page, `/api/admin/models/${modelId}`)
  expect(model.structured_output).toBe('json_schema')
  const enabled = await apiCall(page, 'PATCH', `/api/admin/models/${modelId}`, {
    version: model.version,
    enabled: true,
    is_default: true,
  })
  expect(enabled.status(), await enabled.text()).toBe(200)

  // --- загрузка PDF через UI и извлечение текста
  await page.getByRole('link', { name: 'Договоры' }).first().click()
  await expectAccessible(page, 'договоры')
  await page.locator('#document-files').setInputFiles(env.fixture('contract-e2e.pdf'))
  const uploaded = page.getByRole('listitem').filter({ hasText: 'contract-e2e.pdf' })
  await expect(uploaded).toContainText('Загружен')
  await uploaded.getByRole('link', { name: 'Открыть' }).click()
  await expect(page).toHaveURL(/\/documents\/[0-9a-f-]+$/)
  const documentId = page.url().split('/').pop() ?? ''
  await expect
    .poll(async () => {
      const d = await apiGet<{ current_version_detail: { extraction_status: string } }>(page, `/api/documents/${documentId}`)
      return d.current_version_detail.extraction_status
    }, { timeout: 90_000 })
    .toBe('ready')
  await page.reload()
  await expect(page.getByText('2.2. Поставщик вправе изменить цену').first()).toBeVisible()
  await expectAccessible(page, 'карточка договора')
  const doc = await apiGet<{ current_version_detail: { id: string; sha256: string; page_count: number } }>(
    page,
    `/api/documents/${documentId}`,
  )
  const version = doc.current_version_detail
  expect(version.page_count).toBe(2)
  expect(version.sha256).toBe(sha256(readFileSync(env.fixture('contract-e2e.pdf'))))

  // --- маскировка (этап 10): поиск данных в собранном образе — модели NER в образе, дочерний процесс
  // под RLIMIT_AS. Сам облачный анализ на стенде не проверяется: mock-LLM в частной сети не может быть
  // облачным провайдером (netguard), он покрыт integration-тестами.
  const previewResponse = await apiCall(
    page,
    'POST',
    `/api/documents/${documentId}/versions/${version.id}/redaction-previews`,
  )
  expect(previewResponse.status(), await previewResponse.text()).toBe(201)
  const previewId = ((await previewResponse.json()) as { id: string }).id
  await expect
    .poll(async () => (await apiGet<{ status: string }>(page, `/api/redaction-previews/${previewId}`)).status, {
      timeout: 120_000,
    })
    .toBe('ready')
  const preview = await apiGet<{ ner_available: boolean; entities: { canonical: string; category: string }[] }>(
    page,
    `/api/redaction-previews/${previewId}`,
  )
  expect(preview.ner_available).toBe(true)
  expect(preview.entities.map((e) => e.canonical)).toEqual(expect.arrayContaining(['Альфа Трейд', 'Бета Ритейл']))

  // --- новый анализ через UI
  await page.getByRole('link', { name: 'Запустить анализ' }).click()
  await expect(page.getByRole('heading', { name: 'Новый анализ' })).toBeVisible()
  await page.getByLabel('Модель', { exact: true }).selectOption(modelId)
  await page.getByRole('button', { name: 'Проверить модель и объём' }).click()
  await expect(page.getByText('Оценка объёма')).toBeVisible()
  await expectAccessible(page, 'новый анализ')
  await page.getByRole('button', { name: 'Запустить анализ' }).click()
  await expect(page).toHaveURL(/\/analyses\/[0-9a-f-]+$/)
  const analysisId = page.url().split('/').pop() ?? ''

  await expect
    .poll(async () => (await apiGet<{ status: string }>(page, `/api/analyses/${analysisId}`)).status, {
      timeout: 120_000,
    })
    .toMatch(/^completed/)

  // --- отчёт: три состояния доказательств различимы
  await page.reload()
  const unilateral = page.getByRole('article', { name: UNILATERAL })
  await expect(unilateral).toContainText('Подтверждён')
  await expect(unilateral).toContainText('Цитата совпадает')
  await expect(unilateral).toContainText('стр. 1')
  await expect(page.getByRole('article', { name: PENALTY })).toContainText('Подтверждён')
  const missing = page.getByRole('article', { name: MISSING })
  await expect(missing).toContainText('Требует проверки')
  await expect(missing).toContainText('Область поиска')
  // Риск с выдуманной цитатой скрыт по умолчанию и виден как «Не подтверждён»
  await expect(page.getByRole('article', { name: FABRICATED })).toHaveCount(0)
  await page.getByLabel('Показывать отклонённые').check()
  const fabricated = page.getByRole('article', { name: FABRICATED })
  await expect(fabricated).toContainText('Не подтверждён')
  await expect(page.getByRole('note').filter({ hasText: 'prompt injection' })).toBeVisible()
  await expect(page.getByText(/не означает, что договор безопасен|не заменяет|не является юридическ/).first()).toBeVisible()
  await expectAccessible(page, 'отчёт анализа')

  // --- проверка доказательств на уровне данных: цитата — дословный фрагмент сохранённого текста
  const risks = (
    await apiGet<{ items: Risk[] }>(page, `/api/analyses/${analysisId}/risks?include_rejected=true`)
  ).items
  const byTitle = new Map(risks.map((r) => [r.title, r]))
  const fragments = (
    await apiGet<{ items: Fragment[] }>(page, `/api/documents/${documentId}/versions/${version.id}/fragments?limit=500`)
  ).items
  const textByKey = new Map(fragments.map((f) => [f.fragment_key, f.text.replace(/\s+/g, ' ')]))
  for (const title of [UNILATERAL, PENALTY]) {
    const risk = byTitle.get(title)
    expect(risk?.validation_status, title).toBe('verified_match')
    const evidence = risk?.evidence.filter((e) => !e.superseded) ?? []
    expect(evidence.length).toBeGreaterThan(0)
    for (const e of evidence) {
      expect(['exact', 'normalized']).toContain(e.match_status)
      expect(e.relevance_status).toBe('supports')
      expect(textByKey.get(e.fragment_key ?? '')).toContain(e.quote.replace(/\s+/g, ' '))
      expect(e.page_start).not.toBeNull()
    }
  }
  expect(byTitle.get(UNILATERAL)?.evidence[0]?.clause_ref).toBe('2.2')
  expect(byTitle.get(FABRICATED)?.validation_status).toBe('rejected')
  expect(byTitle.get(MISSING)?.validation_status).toBe('needs_review')
  expect(byTitle.get(MISSING)?.evidence).toEqual([])

  // --- экспорт PDF и XLSX через UI
  await exportAndDownload(page, 'Отчёт PDF', '%PDF')
  await exportAndDownload(page, 'Риски XLSX', 'PK')

  // --- аудит без текста договора
  const audit = await page.request.get('/api/admin/audit?limit=100')
  expect(audit.status()).toBe(200)
  const auditText = await audit.text()
  for (const action of ['document.uploaded', 'analysis.started', 'export.created', 'export.downloaded']) {
    expect(auditText).toContain(action)
  }
  expect(auditText).not.toContain('вправе изменить цену')
  expect(auditText).not.toContain('contract-e2e.pdf')
  expect(auditText).not.toContain('Альфа Трейд')

  // --- настройки маскировки (Superadmin) доступны и проходят аудит доступности
  await page.goto('/admin/settings')
  await expect(page.getByRole('heading', { name: 'Маскировка данных' })).toBeVisible()
  await expect(page.getByLabel('Разрешить пользователям отправлять текст в облако без маскировки')).not.toBeChecked()
  await expectAccessible(page, 'настройки')

  saveState({
    documentId,
    versionId: version.id,
    analysisId,
    originalSha256: version.sha256,
    verifiedTitles: [UNILATERAL, PENALTY],
  })
})

async function exportAndDownload(page: Page, button: string, magic: string): Promise<void> {
  await page.getByRole('button', { name: button }).click()
  const row = page.getByRole('listitem').filter({ hasText: button }).first()
  const link = row.getByRole('link', { name: 'Скачать' })
  await expect(link).toBeVisible({ timeout: 60_000 })
  const [download] = await Promise.all([page.waitForEvent('download'), link.click()])
  const path = await download.path()
  const head = readFileSync(path).subarray(0, magic.length).toString('latin1')
  expect(head, button).toBe(magic)
  expect(download.suggestedFilename()).toMatch(/^contract-analysis-/)
}
