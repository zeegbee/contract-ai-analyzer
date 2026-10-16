import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import type { DocumentDetail, DocumentItem, Fragment, Version } from '../../api/documents'
import { makeMe, serveMe } from '../../test/auth'
import { renderRoute } from '../../test/render'
import { server } from '../../test/server'

const LIMITS = { max_upload_mb: 25, max_pages: 500, extensions: ['doc', 'docx', 'pdf'], can_upload: true }

function makeVersion(overrides: Partial<Version> = {}): Version {
  return {
    id: 'v-1',
    version_no: 1,
    original_filename: 'Договор поставки.pdf',
    detected_format: 'pdf',
    size_bytes: 2048,
    extraction_status: 'ready',
    extraction_error_code: null,
    page_count: 2,
    page_source: 'pdf_native',
    created_at: '2026-10-16T10:00:00Z',
    mime_type: 'application/pdf',
    sha256: 'a'.repeat(64),
    extractor_version: '1.0.0',
    warnings: [],
    fragment_count: 3,
    converted_from_doc: false,
    updated_at: '2026-10-16T10:00:05Z',
    ...overrides,
  }
}

function makeDocument(overrides: Partial<DocumentDetail> = {}, version: Version | null = makeVersion()): DocumentDetail {
  return {
    id: 'd-1',
    title: 'Договор поставки',
    owner_id: 'id-analyst',
    owner_display_name: 'Тест analyst',
    status: 'active',
    current_version_id: version?.id ?? null,
    current_version: version,
    created_at: '2026-10-16T10:00:00Z',
    is_owner: true,
    access: 'owner',
    can_manage: true,
    can_share: true,
    can_analyze: true,
    current_version_detail: version,
    versions: version ? [version] : [],
    ...overrides,
  }
}

function fragment(seq: number, text: string, overrides: Partial<Fragment> = {}): Fragment {
  return {
    id: `f-${seq}`,
    seq,
    fragment_key: `F${String(seq).padStart(6, '0')}`,
    kind: 'paragraph',
    text,
    char_start: 0,
    char_end: text.length,
    page_start: 1,
    page_end: 1,
    clause_ref: null,
    heading_path: [],
    table_ref: null,
    meta: {},
    ...overrides,
  }
}

const listOf = (items: DocumentItem[]) => HttpResponse.json({ items, next_cursor: null })
const limitsHandler = http.get('*/api/documents/upload-limits', () => HttpResponse.json(LIMITS))

function serveList(items: DocumentItem[] = []) {
  server.use(limitsHandler, http.get('*/api/documents', () => listOf(items)))
}

function pdfFile(name = 'contract.pdf', size = 10): File {
  return new File([new Uint8Array(size).fill(37)], name, { type: 'application/pdf' })
}

describe('список договоров и загрузка', () => {
  it('показывает документы со статусом извлечения и владельцем', async () => {
    serveMe(makeMe('analyst'))
    serveList([
      makeDocument(),
      makeDocument(
        { id: 'd-2', title: 'Скан', is_owner: false, owner_display_name: 'Ольга', access: 'view' },
        makeVersion({ id: 'v-2', extraction_status: 'unsupported_no_text', page_count: null }),
      ),
    ])
    renderRoute('/documents')
    expect(await screen.findByRole('link', { name: 'Договор поставки' })).toHaveAttribute('href', '/documents/d-1')
    const scanRow = screen.getByRole('link', { name: 'Скан' }).closest('tr')
    expect(scanRow).not.toBeNull()
    expect(within(scanRow as HTMLElement).getByText('нет текстового слоя')).toBeInTheDocument()
    expect(within(scanRow as HTMLElement).getByText('Ольга')).toBeInTheDocument()
    expect(screen.getByText('готов')).toBeInTheDocument()
    expect(screen.getByLabelText('Выберите файлы')).toBeInTheDocument()
  })

  it('наблюдателю не показывает загрузку', async () => {
    serveMe(makeMe('viewer'))
    serveList([])
    renderRoute('/documents')
    expect(await screen.findByText('Вам ещё не открыли доступ ни к одному договору.')).toBeInTheDocument()
    expect(screen.queryByText('Загрузка договоров')).not.toBeInTheDocument()
  })

  it('загружает выбранный файл: имя, CSRF и Idempotency-Key в заголовках, тело — сам файл', async () => {
    serveMe(makeMe('analyst'))
    serveList([])
    document.cookie = 'cai_csrf=csrf-token; path=/'
    const seen: { name: string | null; csrf: string | null; key: string | null; type: string | null; size: number }[] =
      []
    server.use(
      http.post('*/api/documents', async ({ request }) => {
        seen.push({
          name: request.headers.get('X-File-Name'),
          csrf: request.headers.get('X-CSRF-Token'),
          key: request.headers.get('Idempotency-Key'),
          type: request.headers.get('Content-Type'),
          size: (await request.arrayBuffer()).byteLength,
        })
        return HttpResponse.json(makeDocument({ id: 'd-new' }), { status: 201 })
      }),
    )
    renderRoute('/documents')
    await userEvent.setup().upload(await screen.findByLabelText('Выберите файлы'), pdfFile('Договор №1.pdf', 42))
    const link = await screen.findByRole('link', { name: 'Открыть' })
    expect(link).toHaveAttribute('href', '/documents/d-new')
    expect(seen).toHaveLength(1)
    expect(seen[0]).toMatchObject({
      name: encodeURIComponent('Договор №1.pdf'),
      csrf: 'csrf-token',
      type: 'application/pdf',
      size: 42,
    })
    expect(seen[0]?.key).toMatch(/^[A-Za-z0-9_-]{8,128}$/)
  })

  it('принимает файлы перетаскиванием', async () => {
    serveMe(makeMe('analyst'))
    serveList([])
    let uploads = 0
    server.use(
      http.post('*/api/documents', () => {
        uploads += 1
        return HttpResponse.json(makeDocument(), { status: 201 })
      }),
    )
    renderRoute('/documents')
    const zone = await screen.findByTestId('dropzone')
    fireEvent.dragEnter(zone, { dataTransfer: { files: [] } })
    expect(screen.getByText('Отпустите файлы для загрузки')).toBeInTheDocument()
    fireEvent.drop(zone, { dataTransfer: { files: [pdfFile('a.pdf'), pdfFile('b.pdf')] } })
    await waitFor(() => expect(screen.getAllByRole('link', { name: 'Открыть' })).toHaveLength(2))
    expect(uploads).toBe(2)
  })

  it('не отправляет файл с неподдерживаемым расширением или больше лимита', async () => {
    serveMe(makeMe('analyst'))
    serveList([])
    const upload = vi.fn()
    server.use(http.post('*/api/documents', upload))
    renderRoute('/documents')
    const zone = await screen.findByTestId('dropzone')
    fireEvent.drop(zone, {
      dataTransfer: { files: [new File(['x'], 'photo.png', { type: 'image/png' }), pdfFile('big.pdf', 26 * 1024 * 1024)] },
    })
    expect(await screen.findByText('Поддерживаются только файлы .docx, .doc и .pdf')).toBeInTheDocument()
    expect(screen.getByText('Файл больше 25 МБ')).toBeInTheDocument()
    expect(upload).not.toHaveBeenCalled()
  })

  it('показывает ошибку сервера с кодом запроса (подмена MIME)', async () => {
    serveMe(makeMe('analyst'))
    serveList([])
    server.use(
      http.post('*/api/documents', () =>
        HttpResponse.json(
          {
            error: {
              code: 'file_type_mismatch',
              message: 'Расширение файла не соответствует его содержимому',
              request_id: 'req-415',
              details: {},
            },
          },
          { status: 415 },
        ),
      ),
    )
    renderRoute('/documents')
    await userEvent.setup().upload(await screen.findByLabelText('Выберите файлы'), pdfFile('fake.pdf'))
    expect(await screen.findByText('Расширение файла не соответствует его содержимому')).toBeInTheDocument()
    expect(screen.getByText('req-415')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Повторить' })).toBeInTheDocument()
  })
})

describe('карточка документа', () => {
  it('показывает текст по страницам с ключами фрагментов, пунктами и предупреждениями', async () => {
    serveMe(makeMe('analyst'))
    const version = makeVersion({ warnings: [{ code: 'pages_without_text', count: 1, pages: [2] }] })
    const pages: (string | null)[] = []
    server.use(
      http.get('*/api/documents/d-1', () => HttpResponse.json(makeDocument({}, version))),
      http.get('*/api/documents/d-1/access', () => HttpResponse.json({ items: [] })),
      http.get('*/api/documents/d-1/versions/v-1/fragments', ({ request }) => {
        const page = new URL(request.url).searchParams.get('page')
        pages.push(page)
        const items =
          page === '2'
            ? [fragment(2, 'Вторая страница', { page_start: 2, page_end: 2 })]
            : [
                fragment(0, 'Статья 1. Предмет', { kind: 'heading', clause_ref: 'Статья 1' }),
                fragment(1, 'Бумага | 350', { kind: 'table_row', meta: { cells: ['Бумага', '350'] } }),
              ]
        return HttpResponse.json({ items, next_after_seq: null, total: items.length })
      }),
    )
    renderRoute('/documents/d-1')
    expect(await screen.findByRole('heading', { name: 'Договор поставки' })).toBeInTheDocument()
    expect(await screen.findByText('Статья 1. Предмет')).toBeInTheDocument()
    expect(screen.getByText('F000000')).toBeInTheDocument()
    expect(screen.getByText('п. Статья 1')).toBeInTheDocument()
    expect(screen.getByText('Бумага')).toBeInTheDocument()
    expect(screen.getByText(/На некоторых страницах нет текстового слоя.*стр\. 2/)).toBeInTheDocument()
    expect(screen.getByText(/Страницы соответствуют страницам PDF-файла/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Скачать оригинал' })).toHaveAttribute(
      'href',
      '/api/documents/d-1/versions/v-1/file',
    )
    await userEvent.setup().click(screen.getByRole('button', { name: 'Следующая' }))
    expect(await screen.findByText('Вторая страница')).toBeInTheDocument()
    expect(pages).toEqual(['1', '2'])
  })

  it('скан без текстового слоя: объясняет, что OCR не поддерживается', async () => {
    serveMe(makeMe('analyst'))
    const version = makeVersion({ extraction_status: 'unsupported_no_text', extraction_error_code: 'no_text_layer' })
    server.use(
      http.get('*/api/documents/d-1', () => HttpResponse.json(makeDocument({ can_share: false }, version))),
    )
    renderRoute('/documents/d-1')
    expect(await screen.findByText(/Распознавание текста \(OCR\) не поддерживается/)).toBeInTheDocument()
    expect(screen.queryByText('Извлечённый текст')).not.toBeInTheDocument()
  })

  it('ошибка извлечения: показывает причину и повторяет по кнопке', async () => {
    serveMe(makeMe('analyst'))
    let status: Version['extraction_status'] = 'failed'
    const retry = vi.fn()
    server.use(
      http.get('*/api/documents/d-1', () =>
        HttpResponse.json(
          makeDocument(
            { can_share: false },
            makeVersion({
              extraction_status: status,
              extraction_error_code: status === 'failed' ? 'encrypted_document' : null,
            }),
          ),
        ),
      ),
      http.post('*/api/documents/d-1/versions/v-1/retry-extraction', () => {
        retry()
        status = 'pending'
        return HttpResponse.json(makeVersion({ extraction_status: 'pending' }), { status: 202 })
      }),
    )
    renderRoute('/documents/d-1')
    expect(await screen.findByText(/Документ защищён паролем/)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Повторить извлечение' }))
    expect(await screen.findByText(/Извлекаем текст/)).toBeInTheDocument()
    expect(retry).toHaveBeenCalledTimes(1)
  })

  it('удаляет документ после подтверждения', async () => {
    serveMe(makeMe('analyst'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const removed = vi.fn()
    server.use(
      limitsHandler,
      http.get('*/api/documents/d-1', () => HttpResponse.json(makeDocument({ can_share: false }))),
      http.get('*/api/documents/d-1/versions/v-1/fragments', () =>
        HttpResponse.json({ items: [], next_after_seq: null, total: 0 }),
      ),
      http.delete('*/api/documents/d-1', () => {
        removed()
        return HttpResponse.json({ id: 'd-1', status: 'deleting' }, { status: 202 })
      }),
      http.get('*/api/documents', () => listOf([])),
    )
    renderRoute('/documents/d-1')
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Удалить документ' }))
    expect(await screen.findByRole('heading', { name: 'Договоры' })).toBeInTheDocument()
    expect(removed).toHaveBeenCalledTimes(1)
  })

  it('без права управления нет кнопок удаления и выдачи доступа', async () => {
    serveMe(makeMe('viewer'))
    server.use(
      http.get('*/api/documents/d-1', () =>
        HttpResponse.json(makeDocument({ is_owner: false, access: 'view', can_manage: false, can_share: false })),
      ),
      http.get('*/api/documents/d-1/versions/v-1/fragments', () =>
        HttpResponse.json({ items: [], next_after_seq: null, total: 0 }),
      ),
    )
    renderRoute('/documents/d-1')
    expect(await screen.findByText('просмотр')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Удалить документ' })).not.toBeInTheDocument()
    expect(screen.queryByText('Доступ к документу')).not.toBeInTheDocument()
  })

  it('владелец выдаёт доступ по e-mail', async () => {
    serveMe(makeMe('analyst'))
    const grants: unknown[] = []
    server.use(
      http.get('*/api/documents/d-1', () => HttpResponse.json(makeDocument())),
      http.get('*/api/documents/d-1/versions/v-1/fragments', () =>
        HttpResponse.json({ items: [], next_after_seq: null, total: 0 }),
      ),
      http.get('*/api/documents/d-1/access', () => HttpResponse.json({ items: [] })),
      http.put('*/api/documents/d-1/access', async ({ request }) => {
        grants.push(await request.json())
        return HttpResponse.json({
          items: [
            {
              user_id: 'u-2',
              email: 'olga@example.org',
              display_name: 'Ольга',
              role: 'viewer',
              permission: 'view',
              granted_at: '2026-10-16T10:00:00Z',
            },
          ],
        })
      }),
    )
    renderRoute('/documents/d-1')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('E-mail пользователя'), 'olga@example.org')
    await user.click(screen.getByRole('button', { name: 'Выдать доступ' }))
    expect(await screen.findByText(/Ольга/)).toBeInTheDocument()
    expect(grants).toEqual([{ email: 'olga@example.org', permission: 'view' }])
  })

  it('чужой документ — сообщение «не найден» из API', async () => {
    serveMe(makeMe('analyst'))
    server.use(
      http.get('*/api/documents/d-x', () =>
        HttpResponse.json(
          { error: { code: 'document_not_found', message: 'Документ не найден', request_id: 'r-404', details: {} } },
          { status: 404 },
        ),
      ),
    )
    renderRoute('/documents/d-x')
    expect(await screen.findByText('Документ не найден')).toBeInTheDocument()
  })
})
