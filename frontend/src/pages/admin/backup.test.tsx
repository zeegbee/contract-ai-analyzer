import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { makeMe, serveMe } from '../../test/auth'
import { backupHandler, makeBackupState, retentionHandler } from '../../test/fixtures'
import { redactionSettingsHandler } from '../../test/redaction'
import { renderRoute } from '../../test/render'
import { server } from '../../test/server'

const otherSettings = [
  http.get('*/api/admin/system-settings', () => HttpResponse.json({ groups: {}, editable: false })),
  redactionSettingsHandler(),
  http.get('*/api/admin/llm-settings', () =>
    HttpResponse.json({ cloud_models_enabled: false, cloud_notice: { version: '1', text: 'x' }, can_edit: false }),
  ),
  retentionHandler(),
]

const section = async () => within(await screen.findByRole('region', { name: 'Резервное копирование' }))

describe('резервное копирование в настройках', () => {
  it('Admin видит состояние, проблемы и журнал, но не меняет параметры', async () => {
    serveMe(makeMe('admin'))
    server.use(
      backupHandler({
        problems: [{ code: 's3_not_configured', message: 'Для цели s3 задайте BACKUP_S3_ENDPOINT и BACKUP_S3_BUCKET' }],
        scheduler_seen_at: null,
        overdue: true,
        runs: [
          {
            id: 'r-1',
            started_at: '2026-10-17T03:00:00Z',
            finished_at: '2026-10-17T03:01:00Z',
            status: 'failed',
            trigger: 'scheduled',
            target: 'local',
            encrypted: true,
            name: '20261017T030000Z.tar.age',
            size_bytes: 2048,
            uploaded: false,
            verified: false,
            pruned_local: null,
            pruned_remote: null,
            error_code: 'upload_failed',
          },
        ],
      }),
      ...otherSettings,
    )
    renderRoute('/admin/settings')
    const s = await section()
    expect(await s.findByRole('alert')).toHaveTextContent('Для цели s3 задайте BACKUP_S3_ENDPOINT')
    expect(s.getByText(/Планировщик на сервере не опрашивал настройки/)).toBeInTheDocument()
    expect(s.getByText(/Бэкап по расписанию не выполнен вовремя/)).toBeInTheDocument()
    expect(s.getByText('Изменять параметры может только Superadmin.')).toBeInTheDocument()
    expect(s.getByLabelText(/Расписание \(cron/)).toBeDisabled()
    expect(s.queryByRole('button', { name: 'Запустить сейчас' })).not.toBeInTheDocument()
    expect(s.queryByRole('button', { name: 'Сохранить параметры' })).not.toBeInTheDocument()
    const row = s.getByRole('row', { name: /20261017T030000Z\.tar\.age/ })
    expect(row).toHaveTextContent('ошибка')
    expect(row).toHaveTextContent('по расписанию')
    expect(row).toHaveTextContent('ошибка выгрузки во внешнее хранилище')
    expect(row).toHaveTextContent('не выгружен')
  })

  it('Superadmin меняет только изменённые параметры; ненастроенная цель недоступна', async () => {
    serveMe(makeMe('superadmin'))
    let body: unknown = null
    server.use(
      http.put('*/api/admin/backup', async ({ request }) => {
        body = await request.json()
        return HttpResponse.json(
          makeBackupState({
            can_manage: true,
            config: { ...makeBackupState().config, schedule: '0 */6 * * *', target: 'local', remote_prune: 'script' },
          }),
        )
      }),
      backupHandler({ can_manage: true }),
      ...otherSettings,
    )
    renderRoute('/admin/settings')
    const s = await section()
    const user = userEvent.setup()
    expect(await s.findByLabelText(/S3-совместимое хранилище/)).toBeDisabled()
    expect(s.getByText(/не настроено в \.env сервера/)).toBeInTheDocument()
    expect(s.getByText(/публичный ключ age1abcd…wxyz12/)).toBeInTheDocument()

    await user.selectOptions(s.getByLabelText('Шаблоны'), '0 */6 * * *')
    await user.click(s.getByLabelText(/Каталог \(NAS, второй диск\)/))
    await user.click(s.getByLabelText(/скриптом по схеме хранения/))
    await user.click(s.getByRole('button', { name: 'Сохранить параметры' }))
    await waitFor(() =>
      expect(body).toEqual({ schedule: '0 */6 * * *', target: 'local', remote_prune: 'script', reset: [] }),
    )
    expect(await screen.findByText('Параметры резервного копирования сохранены.')).toBeInTheDocument()
  })

  it('значение из интерфейса можно вернуть к значению из .env', async () => {
    serveMe(makeMe('superadmin'))
    let body: unknown = null
    const state = makeBackupState({
      can_manage: true,
      config: { ...makeBackupState().config, keep_local: 3 },
      sources: { ...makeBackupState().sources, keep_local: 'ui' },
    })
    server.use(
      http.put('*/api/admin/backup', async ({ request }) => {
        body = await request.json()
        return HttpResponse.json(makeBackupState({ can_manage: true }))
      }),
      http.get('*/api/admin/backup', () => HttpResponse.json(state)),
      ...otherSettings,
    )
    renderRoute('/admin/settings')
    const s = await section()
    const user = userEvent.setup()
    expect(await s.findByLabelText(/Копий на сервере/)).toHaveValue(3)
    await user.click(s.getByRole('button', { name: 'вернуть значение из .env' }))
    expect(s.getByLabelText(/Копий на сервере/)).toHaveValue(7)
    await user.click(s.getByRole('button', { name: 'Сохранить параметры' }))
    await waitFor(() => expect(body).toEqual({ reset: ['keep_local'] }))
  })

  it('«Запустить сейчас» отправляет запрос планировщику', async () => {
    serveMe(makeMe('superadmin'))
    let requested = false
    server.use(
      http.post('*/api/admin/backup/run', () => {
        requested = true
        return HttpResponse.json(makeBackupState({ can_manage: true, requested_at: '2026-10-17T12:00:00Z' }), {
          status: 202,
        })
      }),
      backupHandler({ can_manage: true }),
      ...otherSettings,
    )
    renderRoute('/admin/settings')
    const s = await section()
    await userEvent.setup().click(await s.findByRole('button', { name: 'Запустить сейчас' }))
    await waitFor(() => expect(requested).toBe(true))
    expect(await s.findByText(/Внеочередной бэкап запрошен/)).toBeInTheDocument()
    expect(s.getByRole('button', { name: 'Запустить сейчас' })).toBeDisabled()
  })
})
