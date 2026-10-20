import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'

import { safeNextPath } from '../app/session'
import { makeMe, readyHandler, serveMe } from '../test/auth'
import { emptyOverviewHandlers } from '../test/handlers'
import { renderRoute } from '../test/render'
import { server } from '../test/server'

const invalidCredentials = HttpResponse.json(
  { error: { code: 'invalid_credentials', message: 'Неверный e-mail или пароль', request_id: 'rid-7', details: {} } },
  { status: 401 },
)

beforeEach(() => server.use(readyHandler))

describe('вход', () => {
  it('без сессии перенаправляет на страницу входа', async () => {
    serveMe(null)
    renderRoute('/admin/users')
    expect(await screen.findByRole('heading', { name: 'Вход в систему' })).toBeInTheDocument()
  })

  it('входит и возвращает на исходную страницу', async () => {
    serveMe(null)
    let sent: unknown = null
    const me = makeMe('admin')
    server.use(
      http.post('*/api/auth/login', async ({ request }) => {
        sent = await request.json()
        serveMe(me)
        return HttpResponse.json(me)
      }),
      http.get('*/api/users', () => HttpResponse.json({ items: [], next_cursor: null })),
    )
    renderRoute('/admin/users')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('E-mail'), 'admin@example.org')
    await user.type(screen.getByLabelText('Пароль'), 'Granite-Velvet-Comet-81')
    await user.click(screen.getByRole('button', { name: 'Войти' }))
    expect(await screen.findByRole('heading', { name: 'Пользователи' })).toBeInTheDocument()
    expect(sent).toEqual({ email: 'admin@example.org', password: 'Granite-Velvet-Comet-81' })
  })

  it('показывает ошибку и очищает пароль', async () => {
    serveMe(null)
    server.use(http.post('*/api/auth/login', () => invalidCredentials))
    renderRoute('/login')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('E-mail'), 'x@example.org')
    await user.type(screen.getByLabelText('Пароль'), 'wrong-password')
    await user.click(screen.getByRole('button', { name: 'Войти' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Неверный e-mail или пароль')
    expect(screen.getByText('rid-7')).toBeInTheDocument()
    expect(screen.getByLabelText('Пароль')).toHaveValue('')
  })

  it('не отправляет пустую форму', async () => {
    serveMe(null)
    let called = false
    server.use(http.post('*/api/auth/login', () => ((called = true), invalidCredentials)))
    renderRoute('/login')
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Войти' }))
    expect(await screen.findByText('Укажите e-mail')).toBeInTheDocument()
    expect(called).toBe(false)
  })

  it('сообщает об отсутствии администратора', async () => {
    serveMe(null)
    server.use(
      http.get('*/api/health/ready', () =>
        HttpResponse.json(
          { status: 'not_ready', checks: { accounts: 'bootstrap_required' }, version: '0.1.0' },
          { status: 503 },
        ),
      ),
    )
    renderRoute('/login')
    expect(await screen.findByText(/нет активного администратора/)).toBeInTheDocument()
  })

  it('принимает только внутренний путь возврата', () => {
    expect(safeNextPath('/admin/users?x=1')).toBe('/admin/users?x=1')
    for (const bad of ['//evil.example', 'https://evil.example', '/\\evil.example', null, '']) {
      expect(safeNextPath(bad)).toBe('/')
    }
  })
})

describe('принудительная смена пароля', () => {
  it('перенаправляет на смену пароля и скрывает навигацию', async () => {
    serveMe(makeMe('admin', { must_change_password: true }))
    renderRoute('/admin/users')
    expect(await screen.findByRole('heading', { name: 'Смена пароля' })).toBeInTheDocument()
    expect(screen.getByText(/Временный пароль больше не будет действовать/)).toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('проверяет совпадение паролей до отправки', async () => {
    serveMe(makeMe('analyst', { must_change_password: true }))
    let called = false
    server.use(http.post('*/api/auth/change-password', () => ((called = true), HttpResponse.json({}))))
    renderRoute('/change-password')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Текущий пароль'), 'Temp-Password-111')
    await user.type(screen.getByLabelText('Новый пароль'), 'Saffron-Glacier-Willow-37')
    await user.type(screen.getByLabelText('Повторите новый пароль'), 'Saffron-Glacier-Willow-38')
    await user.click(screen.getByRole('button', { name: 'Сменить пароль' }))
    expect(await screen.findByText('Пароли не совпадают')).toBeInTheDocument()
    expect(called).toBe(false)
  })

  it('после смены открывает приложение', async () => {
    const me = makeMe('analyst', { must_change_password: true })
    serveMe(me)
    let body: unknown = null
    server.use(
      http.post('*/api/auth/change-password', async ({ request }) => {
        body = await request.json()
        const updated = { ...me, must_change_password: false }
        serveMe(updated)
        return HttpResponse.json(updated)
      }),
      readyHandler,
      ...emptyOverviewHandlers,
    )
    renderRoute('/change-password')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Текущий пароль'), 'Temp-Password-111')
    await user.type(screen.getByLabelText('Новый пароль'), 'Saffron-Glacier-Willow-37')
    await user.type(screen.getByLabelText('Повторите новый пароль'), 'Saffron-Glacier-Willow-37')
    await user.click(screen.getByRole('button', { name: 'Сменить пароль' }))
    expect(await screen.findByRole('heading', { name: 'Обзор' })).toBeInTheDocument()
    expect(body).toEqual({ current_password: 'Temp-Password-111', new_password: 'Saffron-Glacier-Willow-37' })
  })

  it('показывает ошибку политики паролей от сервера', async () => {
    serveMe(makeMe('analyst'))
    server.use(
      http.post('*/api/auth/change-password', () =>
        HttpResponse.json(
          {
            error: {
              code: 'password_policy',
              message: 'Пароль входит в список распространённых',
              request_id: 'r1',
              details: { rule: 'password_too_common' },
            },
          },
          { status: 422 },
        ),
      ),
    )
    renderRoute('/change-password')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Текущий пароль'), 'Temp-Password-111')
    await user.type(screen.getByLabelText('Новый пароль'), 'Password1234')
    await user.type(screen.getByLabelText('Повторите новый пароль'), 'Password1234')
    await user.click(screen.getByRole('button', { name: 'Сменить пароль' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Пароль входит в список распространённых')
  })
})

describe('сессия и выход', () => {
  it('при 401 от API возвращает на страницу входа', async () => {
    serveMe(makeMe('admin'))
    server.use(
      http.get('*/api/users', () => {
        serveMe(null)
        return HttpResponse.json(
          { error: { code: 'authentication_required', message: 'Требуется вход в систему', request_id: 'r', details: {} } },
          { status: 401 },
        )
      }),
    )
    renderRoute('/admin/users')
    expect(await screen.findByRole('heading', { name: 'Вход в систему' })).toBeInTheDocument()
  })

  it('выход вызывает API с CSRF-токеном', async () => {
    document.cookie = 'cai_csrf=csrf-xyz; path=/'
    serveMe(makeMe('viewer'))
    let csrf: string | null = null
    server.use(
      http.post('*/api/auth/logout', ({ request }) => {
        csrf = request.headers.get('X-CSRF-Token')
        serveMe(null)
        return new HttpResponse(null, { status: 204 })
      }),
      ...emptyOverviewHandlers,
    )
    renderRoute('/')
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Выйти' }))
    expect(await screen.findByRole('heading', { name: 'Вход в систему' })).toBeInTheDocument()
    await waitFor(() => expect(csrf).toBe('csrf-xyz'))
  })
})

describe('навигация по ролям (только UX)', () => {
  it('аналитик не видит администрирование', async () => {
    serveMe(makeMe('analyst'))
    renderRoute('/')
    const nav = await screen.findByRole('navigation')
    expect(nav).toHaveTextContent('Мой профиль')
    expect(nav).not.toHaveTextContent('Пользователи')
    expect(nav).not.toHaveTextContent('Журнал аудита')
  })

  it('прямой переход аналитика на админ-страницу не запрашивает API', async () => {
    serveMe(makeMe('viewer'))
    let called = false
    server.use(http.get('*/api/users', () => ((called = true), HttpResponse.json({ items: [], next_cursor: null }))))
    renderRoute('/admin/users')
    expect(await screen.findByText('Недостаточно прав для просмотра этой страницы')).toBeInTheDocument()
    expect(called).toBe(false)
  })

  it('администратор видит пользователей и аудит', async () => {
    serveMe(makeMe('admin'))
    renderRoute('/')
    const nav = await screen.findByRole('navigation')
    expect(nav).toHaveTextContent('Пользователи')
    expect(nav).toHaveTextContent('Журнал аудита')
  })

  it('в подвале — предупреждение и ссылка на состояние системы, без автора', async () => {
    serveMe(makeMe('viewer'))
    renderRoute('/')
    const footer = await screen.findByRole('contentinfo')
    expect(footer).toHaveTextContent('не заменяют консультацию юриста')
    expect(footer).not.toHaveTextContent('Автор')
    expect(within(footer).getByRole('link', { name: 'Состояние системы' })).toHaveAttribute('href', '/status')
  })
})
