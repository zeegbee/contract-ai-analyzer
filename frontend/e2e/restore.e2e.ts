/**
 * После deploy/restore.sh на пересозданном пустом стенде (e2e/run.sh backup-restore):
 * вход с паролем, установленным до бэкапа, документ, оригинал (SHA-256), анализ и риски на месте.
 */
import { expect, test } from '@playwright/test'

import { apiGet, env, loadState, login, sha256 } from './helpers'

test('данные и вход восстановлены из бэкапа', async ({ page }) => {
  const state = loadState()
  // Пароль из bootstrap не действует: БД восстановлена, а в ней пароль уже сменён.
  await login(page, env.bootstrapPassword)
  await expect(page.getByText('Неверный e-mail или пароль')).toBeVisible()
  await login(page, env.newPassword)
  await expect(page).toHaveURL(/\/$/)

  const file = await page.request.get(`/api/documents/${state.documentId}/versions/${state.versionId}/file`)
  expect(file.status()).toBe(200)
  expect(sha256(await file.body())).toBe(state.originalSha256)

  const analysis = await apiGet<{ status: string }>(page, `/api/analyses/${state.analysisId}`)
  expect(analysis.status).toMatch(/^completed/)
  await page.goto(`/analyses/${state.analysisId}`)
  for (const title of state.verifiedTitles) {
    await expect(page.getByRole('article', { name: title })).toContainText('Подтверждён')
  }
})
