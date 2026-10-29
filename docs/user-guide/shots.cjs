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
// Данные берутся со стенда: завершённый анализ (предпочтительно с правилами организации и маскировкой),
// первый пользователь кроме себя, первая модель каталога. Чего на стенде нет — тот снимок
// пропускается, и в каталоге остаётся прежний файл. Снимок предпросмотра маскировки создаёт на стенде
// предпросмотр (поиск данных без обращения к модели; неиспользованный удаляется по сроку действия).
const path = require('path')
const { chromium } = require(path.join(__dirname, '..', '..', 'frontend', 'node_modules', '@playwright/test'))

const BASE = (process.env.CAI_URL || 'http://127.0.0.1:8080').replace(/\/$/, '')
const EMAIL = process.env.CAI_EMAIL || 'root@example.org'
const PASSWORD = process.env.CAI_PASSWORD
const TEMP_ENABLE = process.env.CAI_TEMP_ENABLE_MODELS === '1'
const OUT = path.join(__dirname, 'screenshots')
// Окно уже контейнера приложения (max-w-6xl = 1152 px) и снимок в 2x: интерфейс на рисунке крупнее и чётче.
const VIEWPORT = { width: 1024, height: 860 }

if (!PASSWORD) {
  console.error('Задайте CAI_PASSWORD (пароль Superadmin)')
  process.exit(2)
}

;(async () => {
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2, locale: 'ru-RU' })
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
    // Заголовок — к верху окна, даже если он уже виден.
    await page.getByRole('heading', { name: text }).first().evaluate((el) => el.scrollIntoView({ block: 'start' }))
    await page.evaluate(() => window.scrollBy(0, -24))
  }
  const skip = (name, why) => console.warn(`пропущен ${name}: ${why}`)
  // Снимок области элемента с полями вокруг — без пустого фона страницы.
  const around = async (locator, pad = 32) => {
    const box = await locator.boundingBox()
    const x = Math.max(0, Math.floor(box.x - pad))
    const y = Math.max(0, Math.floor(box.y - pad))
    return {
      x,
      y,
      width: Math.min(VIEWPORT.width - x, Math.ceil(box.width + 2 * pad)),
      height: Math.ceil(box.height + 2 * pad),
    }
  }

  try {
    await page.goto(BASE + '/login')
    await settle()
    await save('login', { clip: await around(page.locator('section[aria-labelledby="login-title"]')) })
    await page.getByLabel('E-mail').fill(EMAIL)
    await page.getByLabel('Пароль').fill(PASSWORD)
    await page.getByRole('button', { name: 'Войти' }).click()
    await page.waitForURL(BASE + '/')

    // Данные стенда
    const me = await api('GET', '/api/auth/me')
    const documents = (await api('GET', '/api/documents')).items
    // Отчёт: завершённый анализ, лучше всего — с применёнными правилами организации и маскировкой.
    let doc = null
    let analysis = null
    let best = -1
    for (const d of documents) {
      for (const a of (await api('GET', `/api/analyses?document_id=${d.id}`)).items) {
        if (!a.status.startsWith('completed')) continue
        const detail = await api('GET', `/api/analyses/${a.id}`)
        const score = (detail.summary?.risk_rules?.length ? 2 : 0) + (detail.redaction?.mode === 'masked' ? 1 : 0)
        if (score > best) {
          best = score
          doc = d
          analysis = a
        }
      }
      if (best === 3) break
    }
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
      const categories = page.locator('fieldset').filter({ has: page.locator('legend', { hasText: 'Категории рисков' }) })
      if (await page.getByText('Правил выбрано').count()) {
        await settle()
        await categories.screenshot({ path: path.join(OUT, 'new-analysis-rules.png') })
        console.log('снимок new-analysis-rules')
      } else skip('new-analysis-rules', 'нет включённых правил рисков организации')
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
        // Предпросмотр маскировки: согласие на передачу → «Проверить маскировку» (модель не вызывается).
        const consent = page.getByRole('checkbox', { name: /Я ознакомился/ })
        if (!(await consent.isChecked())) await consent.check()
        await page.getByRole('button', { name: 'Проверить маскировку' }).click()
        await page.getByText(/Будет скрыто значений|Данных для маскировки не найдено/).waitFor({ timeout: 120000 })
        await save('redaction-preview')
      } else {
        skip('new-analysis-cloud', 'нет доступной облачной модели (CAI_TEMP_ENABLE_MODELS=1)')
        skip('redaction-preview', 'нет доступной облачной модели (CAI_TEMP_ENABLE_MODELS=1)')
      }

      await open('report-summary', `/analyses/${analysis.id}`)
      await toHeading('Риски')
      await save('report-risks')
      // Карточка с отметкой правила и цитатами; иначе — первая.
      const withRule = page.locator('article').filter({ hasText: /Правило RR-/ }).filter({ hasText: 'Доказательства' })
      const card = (await withRule.count()) ? withRule.first() : page.locator('article').first()
      await card.scrollIntoViewIfNeeded()
      await settle()
      await card.screenshot({ path: path.join(OUT, 'risk-card.png') })
      console.log('снимок risk-card')
      await toHeading('Экспорт')
      await save('report-export')
    } else {
      for (const n of ['document', 'document-analyses-access', 'new-analysis', 'new-analysis-rules', 'redaction-preview', 'report-summary', 'report-risks', 'risk-card', 'report-export'])
        skip(n, 'нет договора с завершённым анализом')
    }

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
    await open('risk-rules', '/admin/risk-rules')
    const editRule = page.getByRole('button', { name: 'Изменить' }).first()
    if (await editRule.count()) {
      await editRule.click()
      const dialog = page.getByRole('dialog')
      await dialog.waitFor()
      await settle()
      await dialog.screenshot({ path: path.join(OUT, 'risk-rule-form.png') })
      console.log('снимок risk-rule-form')
      await dialog.getByRole('button', { name: 'Отмена' }).click()
    } else skip('risk-rule-form', 'нет правил рисков организации')
    await open('settings', '/admin/settings')
    await toHeading('Маскировка данных')
    await save('settings-redaction')
    await open('audit', '/admin/audit')
    await open('status', '/status')
    // Смена пароля: шапка и форма, без пустого места под кнопкой.
    await page.goto(BASE + '/change-password')
    await settle()
    const submit = await page.getByRole('button', { name: 'Сменить пароль' }).boundingBox()
    await save('change-password', { clip: { x: 0, y: 0, width: VIEWPORT.width, height: Math.round(submit.y + submit.height + 32) } })
  } finally {
    if (restore) await restore()
    await browser.close()
  }
})().catch((e) => {
  console.error(e)
  process.exit(1)
})
