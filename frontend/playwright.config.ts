import { defineConfig, devices } from '@playwright/test'

// E2E на отдельном стенде Docker Compose (e2e/run.sh): адрес и учётные данные — из окружения.
// Vitest эти файлы не запускает (*.e2e.ts вне src/).
export default defineConfig({
  testDir: './e2e',
  testMatch: '*.e2e.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8091',
    locale: 'ru-RU',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    acceptDownloads: true,
    ...devices['Desktop Chrome'],
  },
  projects: [
    { name: 'flow', testMatch: 'flow.e2e.ts' },
    { name: 'recovery', testMatch: 'recovery.e2e.ts' },
    { name: 'restore', testMatch: 'restore.e2e.ts' },
  ],
})
