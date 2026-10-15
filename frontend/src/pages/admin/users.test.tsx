import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import type { User, UserDetail } from '../../api/users'
import { makeMe, serveMe } from '../../test/auth'
import { renderRoute } from '../../test/render'
import { server } from '../../test/server'

function makeUser(overrides: Partial<UserDetail> = {}): UserDetail {
  return {
    id: 'u-1',
    email: 'ivan@example.org',
    display_name: 'Иван Петров',
    role: 'analyst',
    is_active: true,
    must_change_password: false,
    failed_login_count: 0,
    locked_until: null,
    last_login_at: null,
    password_changed_at: null,
    created_at: '2026-10-15T10:00:00Z',
    active_sessions: 1,
    ...overrides,
  }
}

const list = (items: User[], next_cursor: string | null = null) => HttpResponse.json({ items, next_cursor })

describe('список пользователей', () => {
  it('показывает пользователей и подгружает следующую страницу', async () => {
    serveMe(makeMe('admin'))
    server.use(
      http.get('*/api/users', ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor')
        return cursor
          ? list([makeUser({ id: 'u-2', email: 'olga@example.org', display_name: 'Ольга' })])
          : list([makeUser()], 'next-1')
      }),
    )
    renderRoute('/admin/users')
    expect(await screen.findByRole('link', { name: 'Иван Петров' })).toHaveAttribute('href', '/admin/users/u-1')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Показать ещё' }))
    expect(await screen.findByRole('link', { name: 'Ольга' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Показать ещё' })).not.toBeInTheDocument()
  })

  it('передаёт фильтры на сервер', async () => {
    serveMe(makeMe('admin'))
    const seen: string[] = []
    server.use(
      http.get('*/api/users', ({ request }) => {
        seen.push(new URL(request.url).search)
        return list([])
      }),
    )
    renderRoute('/admin/users')
    expect(await screen.findByText('Пользователи не найдены')).toBeInTheDocument()
    await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'Статус' }), 'false')
    await waitFor(() => expect(seen.at(-1)).toContain('is_active=false'))
  })
})

describe('создание пользователя', () => {
  it('администратору недоступна роль Superadmin', async () => {
    serveMe(makeMe('admin'))
    server.use(http.get('*/api/users', () => list([])))
    renderRoute('/admin/users')
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Создать пользователя' }))
    const options = within(screen.getByLabelText('Роль', { selector: '#new-role' })).getAllByRole('option')
    expect(options.map((o) => o.textContent)).toEqual(['Администратор', 'Аналитик', 'Наблюдатель'])
  })

  it('показывает временный пароль один раз', async () => {
    serveMe(makeMe('superadmin'))
    let body: unknown = null
    server.use(
      http.get('*/api/users', () => list([])),
      http.post('*/api/users', async ({ request }) => {
        body = await request.json()
        return HttpResponse.json(
          { user: makeUser({ email: 'new@example.org', must_change_password: true }), temporary_password: 'Tmp9Pass7Word3xyz' },
          { status: 201 },
        )
      }),
    )
    renderRoute('/admin/users')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Создать пользователя' }))
    await user.type(screen.getByLabelText('E-mail'), 'new@example.org')
    await user.type(screen.getByLabelText('Имя'), 'Новый')
    await user.selectOptions(screen.getByLabelText('Роль', { selector: '#new-role' }), 'viewer')
    await user.click(screen.getByRole('button', { name: 'Создать' }))
    expect(await screen.findByTestId('temporary-password')).toHaveTextContent('Tmp9Pass7Word3xyz')
    expect(body).toEqual({ email: 'new@example.org', display_name: 'Новый', role: 'viewer' })
    await user.click(screen.getByRole('button', { name: 'Готово' }))
    expect(screen.queryByText('Tmp9Pass7Word3xyz')).not.toBeInTheDocument()
  })

  it('проверяет e-mail до отправки', async () => {
    serveMe(makeMe('admin'))
    server.use(http.get('*/api/users', () => list([])))
    renderRoute('/admin/users')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Создать пользователя' }))
    await user.type(screen.getByLabelText('E-mail'), 'not-an-email')
    await user.type(screen.getByLabelText('Имя'), 'X')
    await user.click(screen.getByRole('button', { name: 'Создать' }))
    expect(await screen.findByText('Некорректный e-mail')).toBeInTheDocument()
  })
})

describe('карточка пользователя', () => {
  const noModels = http.get('*/api/users/:id/model-permissions', () => HttpResponse.json({ items: [] }))

  it('Superadmin защищён от изменений администратором', async () => {
    serveMe(makeMe('admin'))
    server.use(
      http.get('*/api/users/:id', () => HttpResponse.json(makeUser({ role: 'superadmin', display_name: 'Root' }))),
      noModels,
    )
    renderRoute('/admin/users/u-1')
    expect(await screen.findByText(/может изменять только Superadmin/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Заблокировать' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Сбросить пароль' })).not.toBeInTheDocument()
  })

  it('для своей учётной записи нельзя менять роль и блокировать', async () => {
    const me = makeMe('admin')
    serveMe(me)
    server.use(http.get('*/api/users/:id', () => HttpResponse.json(makeUser({ id: me.id, role: 'admin' }))), noModels)
    renderRoute(`/admin/users/${me.id}`)
    expect(await screen.findByLabelText('Роль')).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Заблокировать' })).not.toBeInTheDocument()
  })

  it('сброс пароля после подтверждения показывает временный пароль', async () => {
    serveMe(makeMe('admin'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    let resetCalled = false
    server.use(
      http.get('*/api/users/:id', () => HttpResponse.json(makeUser())),
      noModels,
      http.post('*/api/users/:id/reset-password', () => {
        resetCalled = true
        return HttpResponse.json({ user: makeUser({ must_change_password: true }), temporary_password: 'Reset4Pass8Word2ab' })
      }),
    )
    renderRoute('/admin/users/u-1')
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Сбросить пароль' }))
    expect(await screen.findByTestId('temporary-password')).toHaveTextContent('Reset4Pass8Word2ab')
    expect(resetCalled).toBe(true)
  })

  it('без подтверждения блокировка не выполняется', async () => {
    serveMe(makeMe('admin'))
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    let patched = false
    server.use(
      http.get('*/api/users/:id', () => HttpResponse.json(makeUser())),
      noModels,
      http.patch('*/api/users/:id', () => ((patched = true), HttpResponse.json(makeUser()))),
    )
    renderRoute('/admin/users/u-1')
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Заблокировать' }))
    expect(patched).toBe(false)
  })

  it('показывает ошибку сервера при попытке понизить последнего Superadmin', async () => {
    serveMe(makeMe('superadmin'))
    server.use(
      http.get('*/api/users/:id', () => HttpResponse.json(makeUser({ role: 'superadmin' }))),
      noModels,
      http.patch('*/api/users/:id', () =>
        HttpResponse.json(
          {
            error: {
              code: 'last_superadmin',
              message: 'Нельзя понизить или заблокировать последнего активного Superadmin',
              request_id: 'r9',
              details: {},
            },
          },
          { status: 409 },
        ),
      ),
    )
    renderRoute('/admin/users/u-1')
    const user = userEvent.setup()
    await user.selectOptions(await screen.findByLabelText('Роль'), 'admin')
    await user.click(screen.getByRole('button', { name: 'Сохранить' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('последнего активного Superadmin')
  })

  it('настраивает доступ к моделям', async () => {
    serveMe(makeMe('admin'))
    let saved: unknown = null
    const model = {
      model_config_id: 'm-1',
      display_name: 'Llama',
      external_model_id: 'llama3.1:8b',
      enabled: true,
      allowed_by_role: true,
      override: null,
      effective: true,
    }
    server.use(
      http.get('*/api/users/:id', () => HttpResponse.json(makeUser())),
      http.get('*/api/users/:id/model-permissions', () => HttpResponse.json({ items: [model] })),
      http.put('*/api/users/:id/model-permissions', async ({ request }) => {
        saved = await request.json()
        return HttpResponse.json({ items: [{ ...model, override: false, effective: false }] })
      }),
    )
    renderRoute('/admin/users/u-1')
    const user = userEvent.setup()
    await user.selectOptions(await screen.findByLabelText('Доступ к моделям: Llama'), 'deny')
    await user.click(screen.getByRole('button', { name: 'Сохранить доступ к моделям' }))
    expect(await screen.findByText('Итог: недоступна')).toBeInTheDocument()
    expect(saved).toEqual({ overrides: [{ model_config_id: 'm-1', allowed: false }] })
  })

  it('пустой каталог моделей — без фиктивных данных', async () => {
    serveMe(makeMe('admin'))
    server.use(http.get('*/api/users/:id', () => HttpResponse.json(makeUser())), noModels)
    renderRoute('/admin/users/u-1')
    expect(await screen.findByText(/Модели ещё не настроены/)).toBeInTheDocument()
  })
})

describe('журнал аудита', () => {
  it('показывает записи с переводом действий', async () => {
    serveMe(makeMe('admin'))
    server.use(
      http.get('*/api/admin/audit', () =>
        HttpResponse.json({
          items: [
            {
              id: 5,
              created_at: '2026-10-15T10:00:00Z',
              actor_user_id: 'id-admin',
              actor_email: 'admin@example.org',
              action: 'user.role_changed',
              target_type: 'user',
              target_id: 'u-1',
              ip: '203.0.113.10',
              request_id: 'r',
              details: { from: 'viewer', to: 'analyst' },
            },
          ],
          next_cursor: null,
        }),
      ),
    )
    renderRoute('/admin/audit')
    expect(await screen.findByText('Изменена роль')).toBeInTheDocument()
    expect(screen.getByText('from: viewer; to: analyst')).toBeInTheDocument()
  })
})
