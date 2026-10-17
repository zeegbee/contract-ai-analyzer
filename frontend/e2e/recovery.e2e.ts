/**
 * Отмена, повтор и восстановление после аварийной остановки worker'а на живом стенде (e2e/run.sh).
 * «Медленная» модель mock-сервера отвечает с задержкой, чтобы действия попадали в середину анализа.
 * Выполняется после flow.e2e.ts (нужны сменённый пароль, провайдер и документ из state.json).
 */
import { execFileSync } from 'node:child_process'

import { expect, test, type Page } from '@playwright/test'

import { apiCall, apiGet, env, loadState, login } from './helpers'

test.describe.configure({ mode: 'serial' })

interface Analysis {
  status: string
  progress: { stage: string | null }
  error_code: string | null
}

function compose(...args: string[]): void {
  // COMPOSE_PROJECT_NAME / COMPOSE_FILE / COMPOSE_ENV_FILES задаёт e2e/run.sh — затрагивается только cai-e2e.
  if (process.env.COMPOSE_PROJECT_NAME !== 'cai-e2e') throw new Error('Запускайте через e2e/run.sh')
  execFileSync('docker', ['compose', ...args], { stdio: 'inherit', cwd: '..' })
}

async function slowModel(page: Page): Promise<string> {
  const providers = await apiGet<{ items: { id: string; name: string }[] }>(page, '/api/admin/providers')
  const provider = providers.items.find((p) => p.name === 'E2E mock Ollama')
  expect(provider).toBeTruthy()
  const created = await apiCall(page, 'POST', '/api/admin/models', {
    provider_config_id: provider?.id,
    external_model_id: 'e2e-slow:latest',
    display_name: 'E2E slow',
    context_window: 32768,
    max_output_tokens: 4096,
  })
  expect(created.status(), await created.text()).toBe(201)
  const id = ((await created.json()) as { id: string }).id
  expect((await apiCall(page, 'POST', `/api/admin/models/${id}/test`, { generate: true })).status()).toBe(200)
  const model = await apiGet<{ version: number }>(page, `/api/admin/models/${id}`)
  expect((await apiCall(page, 'PATCH', `/api/admin/models/${id}`, { version: model.version, enabled: true })).status()).toBe(200)
  return id
}

async function start(page: Page, modelId: string): Promise<string> {
  const { documentId } = loadState()
  const r = await apiCall(page, 'POST', '/api/analyses', {
    document_id: documentId,
    party: 'party_a',
    categories: ['unilateral_changes', 'penalties', 'payment', 'missing_clause', 'acceptance'],
    model_id: modelId,
  })
  expect(r.status(), await r.text()).toBe(201)
  return ((await r.json()) as { id: string }).id
}

const status = (page: Page, id: string) => apiGet<Analysis>(page, `/api/analyses/${id}`)

test('отмена во время вызова модели и повтор', async ({ page }) => {
  await login(page, env.newPassword)
  await expect(page).toHaveURL(/\/$/)
  const modelId = await slowModel(page)
  const id = await start(page, modelId)
  await expect.poll(async () => (await status(page, id)).status, { timeout: 30_000 }).toBe('running')

  const cancel = await apiCall(page, 'POST', `/api/analyses/${id}/cancel`)
  expect(cancel.status(), await cancel.text()).toBe(200)
  await expect.poll(async () => (await status(page, id)).status, { timeout: 30_000 }).toBe('cancelled')

  const retry = await apiCall(page, 'POST', `/api/analyses/${id}/retry`, {})
  expect(retry.status(), await retry.text()).toBe(200)
  await expect.poll(async () => (await status(page, id)).status, { timeout: 120_000 }).toMatch(/^completed/)
})

test('аварийная остановка worker посреди анализа: задача подхвачена после истечения lease', async ({ page }) => {
  test.setTimeout(420_000)
  await login(page, env.newPassword)
  await expect(page).toHaveURL(/\/$/)
  const models = await apiGet<{ items: { id: string; display_name: string }[] }>(page, '/api/models/available')
  const modelId = models.items.find((m) => m.display_name === 'E2E slow')?.id ?? ''
  const id = await start(page, modelId)
  await expect.poll(async () => (await status(page, id)).progress.stage, { timeout: 60_000 }).toBe('chunks')

  // SIGKILL: worker не успевает ни завершить задачу, ни освободить её.
  compose('kill', '-s', 'SIGKILL', 'worker')
  compose('up', '-d', 'worker')
  await expect
    .poll(async () => (await status(page, id)).status, { timeout: 360_000, intervals: [5_000] })
    .toMatch(/^completed/)
  const risks = await apiGet<{ items: { validation_status: string }[] }>(page, `/api/analyses/${id}/risks`)
  expect(risks.items.some((r) => r.validation_status === 'verified_match')).toBe(true)
})
