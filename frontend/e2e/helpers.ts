import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import AxeBuilder from '@axe-core/playwright'
import { expect, type APIResponse, type Page } from '@playwright/test'

function required(name: string): string {
  const value = process.env[name]
  if (!value) throw new Error(`Не задана переменная окружения ${name} (запускайте через e2e/run.sh)`)
  return value
}

export const env = {
  get baseUrl() {
    return process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8091'
  },
  get email() {
    return required('E2E_ADMIN_EMAIL')
  },
  get bootstrapPassword() {
    return required('E2E_BOOTSTRAP_PASSWORD')
  },
  get newPassword() {
    return required('E2E_NEW_PASSWORD')
  },
  fixture(name: string) {
    return join(required('E2E_FIXTURES'), name)
  },
}

export interface RunState {
  documentId: string
  versionId: string
  analysisId: string
  originalSha256: string
  verifiedTitles: string[]
}

export function saveState(state: RunState): void {
  writeFileSync(required('E2E_STATE_FILE'), JSON.stringify(state, null, 2), 'utf-8')
}

export function loadState(): RunState {
  return JSON.parse(readFileSync(required('E2E_STATE_FILE'), 'utf-8')) as RunState
}

export function sha256(data: Buffer): string {
  return createHash('sha256').update(data).digest('hex')
}

export async function login(page: Page, password: string): Promise<void> {
  await page.goto('/login')
  await page.getByLabel('E-mail').fill(env.email)
  await page.getByLabel('Пароль', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Войти' }).click()
}

/** Изменяющий запрос API от имени вошедшего пользователя: cookie сессии, CSRF-токен и Origin — как у SPA. */
export async function apiCall(
  page: Page,
  method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
  path: string,
  data?: unknown,
): Promise<APIResponse> {
  const cookies = await page.context().cookies()
  const csrf = cookies.find((c) => c.name === 'cai_csrf')?.value ?? ''
  return page.request.fetch(path, {
    method,
    data,
    headers: { 'X-CSRF-Token': csrf, Origin: env.baseUrl },
  })
}

export async function apiGet<T>(page: Page, path: string): Promise<T> {
  const r = await page.request.get(path)
  expect(r.status(), `${path}: ${await r.text()}`).toBe(200)
  return (await r.json()) as T
}

/** Автоматический аудит доступности (axe-core, WCAG 2.1 A/AA): серьёзные и критические нарушения недопустимы. */
export async function expectAccessible(page: Page, label: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  const serious = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical')
  const summary = serious.map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(' ')).join('; ')}`)
  expect(summary, `axe: ${label}`).toEqual([])
}
