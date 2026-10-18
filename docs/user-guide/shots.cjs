// Скриншоты интерфейса для руководства пользователя (docs/user-guide/screenshots/*.png).
//
// Запуск (из каталога docs/user-guide, нужен `npm ci` в frontend/ и `npx playwright install chromium`):
//   CAI_PASSWORD='…' node shots.cjs
// Переменные: CAI_URL (по умолчанию http://127.0.0.1:8080; должен быть в ALLOWED_ORIGINS),
// CAI_EMAIL (по умолчанию root@example.org; нужен Superadmin), CAI_PASSWORD (обязательно),
// CAI_TEMP_ENABLE_MODELS=1 — на время съёмки включить все модели, их провайдеров и облачные модели,
// чтобы показать форму анализа с оценкой объёма и облачным предупреждением; исходное состояние
// восстанавливается в конце (в том числе при ошибке). Запросов к моделям при съёмке нет.
//
// Данные берутся со стенда: первый договор с завершённым анализом, договор без текстового слоя,
// первый пользователь кроме себя, первая модель каталога. Чего на стенде нет — тот снимок
// пропускается, и в каталоге остаётся прежний файл.
const path = require('path')
const { chromium } = require(path.join(__dirname, '..', '..', 'frontend', 'node_modules', '@playwright/test'))

const BASE = (process.env.CAI_URL || 'http://127.0.0.1:8080').replace(/\/$/, '')
const EMAIL = process.env.CAI_EMAIL || 'root@example.org'
const PASSWORD = process.env.CAI_PASSWORD
const TEMP_ENABLE = process.env.CAI_TEMP_ENABLE_MODELS === '1'
const OUT = path.join(__dirname, 'screenshots')
const VIEWPORT = { width: 1360, height: 860 }

if (!PASSWORD) {
  console.error('Задайте CAI_PASSWORD (пароль Superadmin)')
  process.exit(2)
}

;(async () => {
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 1.25, locale: 'ru-RU' })
  const page = await ctx.newPage()
  let restore = null

  // API от имени той же сессии браузера: cookie общие, CSRF-токен — из cookie cai_csrf.
  const api = async (method, url, data) => {
    const csrf = (await ctx.cookies()).find((c) => c.name === 'cai_csrf')?.value
    const res = await page.request.fetch(BASE + url, {
      method,
      data,
      headers: { Origin: BASE, ...(csrf ? { 'X-CSRF-Token': csrf } : {}) },
    })
    if (!res.ok()) throw new Error(`${method} ${url}: ${res.status()}`)
    return res.status() === 204 ? null : res.json()
  }
  const settle = async () => {
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(500)
  }
  const save = async (name, options = {}) => {
    await settle()
    await page.screenshot({ path: path.join(OUT, `${name}.png`), ...options })
    console.log('снимок', name)
  }
  const open = async (name, url) => {
    await page.goto(BASE + url)
    await save(name)
  }
  const toHeading = async (text) => {
    await settle()
    await page.getByRole('heading', { name: text }).first().scrollIntoViewIfNeeded()
    await page.evaluate(() => window.scrollBy(0, -24))
  }
  const skip = (name, why) => console.warn(`пропущен ${name}: ${why}`)

  try {
    await page.goto(BASE + '/login')
    await save('login')
    await page.getByLabel('E-mail').fill(EMAIL)
    await page.getByLabel('Пароль').fill(PASSWORD)
    await page.getByRole('button', { name: 'Войти' }).click()
    await page.waitForURL(BASE + '/')

    // Данные стенда
    const me = await api('GET', '/api/auth/me')
    const documents = (await api('GET', '/api/documents')).items
    let doc = null
    let analysis = null
    for (const d of documents) {
      const found = (await api('GET', `/api/analyses?document_id=${d.id}`)).items.find((a) =>
        a.status.startsWith('completed'),
      )
      if (found) {
        doc = d
        analysis = found
        break
      }
    }
    const scan = documents.find((d) => d.current_version?.extraction_status === 'unsupported_no_text')
    const user = (await api('GET', '/api/users')).items.find((u) => u.id !== me.id)
    const models = (await api('GET', '/api/admin/models')).items
    const model = models.find((m) => !m.is_cloud) || models[0]

    if (TEMP_ENABLE) {
      const providers = (await api('GET', '/api/admin/providers')).items
      const llm = await api('GET', '/api/admin/llm-settings')
      const patch = async (kind, id, fields) => {
        const current = await api('GET', `/api/admin/${kind}/${id}`)
        await api('PATCH', `/api/admin/${kind}/${id}`, { version: current.version, ...fields })
      }
      const usedProviders = providers.filter((p) => models.some((m) => m.provider_config_id === p.id))
      restore = async () => {
        for (const m of models) await patch('models', m.id, { enabled: m.enabled })
        for (const p of usedProviders) await patch('providers', p.id, { enabled: p.enabled })
        await api('PUT', '/api/admin/llm-settings', { cloud_models_enabled: llm.cloud_models_enabled })
        console.log('каталог моделей возвращён в исходное состояние')
      }
      for (const p of usedProviders) await patch('providers', p.id, { enabled: true })
      for (const m of models) await patch('models', m.id, { enabled: true })
      await api('PUT', '/api/admin/llm-settings', { cloud_models_enabled: true })
      console.log('модели временно включены')
    }

    await save('overview')
    await open('documents', '/documents')

    if (doc) {
      await open('document', `/documents/${doc.id}`)
      await toHeading('Доступ к документу')
      await save('document-analyses-access')

      await page.goto(`${BASE}/analyses/new?document=${doc.id}`)
      await save('new-analysis')
      const available = (await api('GET', '/api/models/available')).items
      const select = page.getByRole('combobox', { name: 'Модель', exact: true })
      const local = available.find((m) => !m.is_cloud)
      const cloud = available.find((m) => m.is_cloud)
      if (local) {
        await select.selectOption(local.id)
        await page.getByRole('button', { name: 'Проверить модель и объём' }).click()
        await page.getByText('Оценка объёма').waitFor()
        await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight))
        await save('new-analysis-estimate')
      } else skip('new-analysis-estimate', 'нет доступной локальной модели (CAI_TEMP_ENABLE_MODELS=1)')
      if (cloud) {
        await select.selectOption(cloud.id)
        await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight))
        await save('new-analysis-cloud')
      } else skip('new-analysis-cloud', 'нет доступной облачной модели (CAI_TEMP_ENABLE_MODELS=1)')

      await open('report-summary', `/analyses/${analysis.id}`)
      await toHeading('Риски')
      await save('report-risks')
      const card = page.locator('article').first()
      await card.scrollIntoViewIfNeeded()
      await settle()
      await card.screenshot({ path: path.join(OUT, 'risk-card.png') })
      console.log('снимок risk-card')
      await toHeading('Экспорт')
      await save('report-export')
    } else {
      for (const n of ['document', 'document-analyses-access', 'new-analysis', 'report-summary', 'report-risks', 'risk-card', 'report-export'])
        skip(n, 'нет договора с завершённым анализом')
    }

    if (scan) await open('document-no-text', `/documents/${scan.id}`)
    else skip('document-no-text', 'нет договора без текстового слоя (загрузите скан PDF)')

    await open('analyses', '/analyses')
    // Профиль: только верх страницы с первой сессией (остальные сессии — шум).
    await page.goto(BASE + '/account')
    await settle()
    const current = await page.getByText('текущая', { exact: true }).first().boundingBox()
    const height = current ? Math.round(current.y + current.height + 44) : VIEWPORT.height
    await save('account', { clip: { x: 0, y: 0, width: VIEWPORT.width, height } })
    await open('users', '/admin/users')
    if (user) await open('user-detail', `/admin/users/${user.id}`)
    else skip('user-detail', 'нет других пользователей')
    await open('models', '/admin/models')
    if (model) await open('model-detail', `/admin/models/${model.id}`)
    else skip('model-detail', 'нет моделей в каталоге')
    await open('settings', '/admin/settings')
    await open('audit', '/admin/audit')
    await open('status', '/status')
    await open('change-password', '/change-password')
  } finally {
    if (restore) await restore()
    await browser.close()
  }
})().catch((e) => {
  console.error(e)
  process.exit(1)
})
