import { z } from 'zod'

import { roleSchema } from './auth'
import { apiErrorFromBody, apiRequest, networkError, readCookie } from './client'

export const DOCUMENTS_KEY = ['documents'] as const

export const extractionStatusSchema = z.enum(['pending', 'processing', 'ready', 'failed', 'unsupported_no_text'])
export type ExtractionStatus = z.infer<typeof extractionStatusSchema>
const pageSourceSchema = z.enum(['pdf_native', 'docx_rendered_breaks', 'libreoffice_render'])
export type PageSource = z.infer<typeof pageSourceSchema>
const formatSchema = z.enum(['pdf', 'docx', 'doc'])
const permissionSchema = z.enum(['view', 'analyze'])
export type DocumentPermission = z.infer<typeof permissionSchema>

const warningSchema = z.looseObject({ code: z.string() })
export type ExtractionWarning = z.infer<typeof warningSchema>

const versionSummarySchema = z.object({
  id: z.string(),
  version_no: z.number(),
  original_filename: z.string(),
  detected_format: formatSchema,
  size_bytes: z.number(),
  extraction_status: extractionStatusSchema,
  extraction_error_code: z.string().nullable(),
  page_count: z.number().nullable(),
  page_source: pageSourceSchema.nullable(),
  created_at: z.string(),
})
export type VersionSummary = z.infer<typeof versionSummarySchema>

const versionSchema = versionSummarySchema.extend({
  mime_type: z.string(),
  sha256: z.string(),
  extractor_version: z.string().nullable(),
  warnings: z.array(warningSchema),
  fragment_count: z.number(),
  converted_from_doc: z.boolean(),
  updated_at: z.string(),
})
export type Version = z.infer<typeof versionSchema>

const documentSchema = z.object({
  id: z.string(),
  title: z.string(),
  owner_id: z.string(),
  owner_display_name: z.string(),
  status: z.enum(['active', 'deleting']),
  current_version_id: z.string().nullable(),
  current_version: versionSummarySchema.nullable(),
  created_at: z.string(),
  is_owner: z.boolean(),
  access: z.enum(['owner', 'view', 'analyze', 'system']),
  can_manage: z.boolean(),
  can_share: z.boolean(),
  can_analyze: z.boolean(),
})
export type DocumentItem = z.infer<typeof documentSchema>

const documentDetailSchema = documentSchema.extend({
  current_version_detail: versionSchema.nullable(),
  versions: z.array(versionSummarySchema),
})
export type DocumentDetail = z.infer<typeof documentDetailSchema>

const documentListSchema = z.object({ items: z.array(documentSchema), next_cursor: z.string().nullable() })
export type DocumentList = z.infer<typeof documentListSchema>

const pageListSchema = z.object({
  items: z.array(z.object({ page_no: z.number(), char_start: z.number(), char_end: z.number(), source: pageSourceSchema })),
  page_source: pageSourceSchema.nullable(),
  page_count: z.number().nullable(),
})
export type PageList = z.infer<typeof pageListSchema>

const fragmentSchema = z.object({
  id: z.string(),
  seq: z.number(),
  fragment_key: z.string(),
  kind: z.enum(['heading', 'paragraph', 'list_item', 'table_row', 'table_cell', 'footnote', 'header_footer']),
  text: z.string(),
  char_start: z.number(),
  char_end: z.number(),
  page_start: z.number().nullable(),
  page_end: z.number().nullable(),
  clause_ref: z.string().nullable(),
  heading_path: z.array(z.string()),
  table_ref: z.record(z.string(), z.unknown()).nullable(),
  meta: z.record(z.string(), z.unknown()),
})
export type Fragment = z.infer<typeof fragmentSchema>

const fragmentListSchema = z.object({
  items: z.array(fragmentSchema),
  next_after_seq: z.number().nullable(),
  total: z.number(),
})
export type FragmentList = z.infer<typeof fragmentListSchema>

const accessSchema = z.object({
  user_id: z.string(),
  email: z.string(),
  display_name: z.string(),
  role: roleSchema,
  permission: permissionSchema,
  granted_at: z.string(),
})
export type AccessEntry = z.infer<typeof accessSchema>
const accessListSchema = z.object({ items: z.array(accessSchema) })

const uploadLimitsSchema = z.object({
  max_upload_mb: z.number(),
  max_pages: z.number(),
  extensions: z.array(z.string()),
  can_upload: z.boolean(),
})
export type UploadLimits = z.infer<typeof uploadLimitsSchema>

const id = (value: string) => encodeURIComponent(value)
const versionPath = (documentId: string, versionId: string) => `/documents/${id(documentId)}/versions/${id(versionId)}`

export async function fetchDocuments(cursor: string | null, signal?: AbortSignal): Promise<DocumentList> {
  const params = new URLSearchParams({ limit: '50' })
  if (cursor) params.set('cursor', cursor)
  return documentListSchema.parse(await apiRequest<unknown>(`/documents?${params.toString()}`, { signal }))
}

export async function fetchUploadLimits(signal?: AbortSignal): Promise<UploadLimits> {
  return uploadLimitsSchema.parse(await apiRequest<unknown>('/documents/upload-limits', { signal }))
}

export async function fetchDocument(documentId: string, signal?: AbortSignal): Promise<DocumentDetail> {
  return documentDetailSchema.parse(await apiRequest<unknown>(`/documents/${id(documentId)}`, { signal }))
}

export async function deleteDocument(documentId: string): Promise<void> {
  await apiRequest<unknown>(`/documents/${id(documentId)}`, { method: 'DELETE' })
}

export async function retryExtraction(documentId: string, versionId: string): Promise<void> {
  await apiRequest<unknown>(`${versionPath(documentId, versionId)}/retry-extraction`, { method: 'POST' })
}

export async function fetchPages(documentId: string, versionId: string, signal?: AbortSignal): Promise<PageList> {
  return pageListSchema.parse(await apiRequest<unknown>(`${versionPath(documentId, versionId)}/pages`, { signal }))
}

export async function fetchFragments(
  documentId: string,
  versionId: string,
  options: { page?: number | null; afterSeq?: number | null },
  signal?: AbortSignal,
): Promise<FragmentList> {
  const params = new URLSearchParams({ limit: '200' })
  if (options.page) params.set('page', String(options.page))
  if (options.afterSeq !== null && options.afterSeq !== undefined) params.set('after_seq', String(options.afterSeq))
  return fragmentListSchema.parse(
    await apiRequest<unknown>(`${versionPath(documentId, versionId)}/fragments?${params.toString()}`, { signal }),
  )
}

export function originalFileUrl(documentId: string, versionId: string): string {
  return `/api${versionPath(documentId, versionId)}/file`
}

export async function fetchAccess(documentId: string, signal?: AbortSignal): Promise<AccessEntry[]> {
  return accessListSchema.parse(await apiRequest<unknown>(`/documents/${id(documentId)}/access`, { signal })).items
}

export async function grantAccess(documentId: string, email: string, permission: DocumentPermission): Promise<AccessEntry[]> {
  const raw = await apiRequest<unknown>(`/documents/${id(documentId)}/access`, {
    method: 'PUT',
    body: { email, permission },
  })
  return accessListSchema.parse(raw).items
}

export async function revokeAccess(documentId: string, userId: string): Promise<AccessEntry[]> {
  const raw = await apiRequest<unknown>(`/documents/${id(documentId)}/access/${id(userId)}`, { method: 'DELETE' })
  return accessListSchema.parse(raw).items
}

/**
 * Загрузка файла с прогрессом (fetch не сообщает о прогрессе отправки, поэтому XHR).
 * Тело — сам файл; имя — в X-File-Name. Idempotency-Key позволяет безопасно повторить запрос после обрыва.
 */
export function uploadDocument(
  file: File,
  idempotencyKey: string,
  onProgress: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<DocumentDetail> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/documents')
    xhr.setRequestHeader('Accept', 'application/json')
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream')
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(file.name))
    xhr.setRequestHeader('Idempotency-Key', idempotencyKey)
    const csrf = readCookie('cai_csrf')
    if (csrf) xhr.setRequestHeader('X-CSRF-Token', csrf)
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total)
    }
    xhr.onload = () => {
      let body: unknown = null
      try {
        body = JSON.parse(xhr.responseText) as unknown
      } catch {
        // не JSON — ошибка прокси
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(documentDetailSchema.parse(body))
        } catch (error) {
          reject(error instanceof Error ? error : new Error('invalid_response'))
        }
      } else {
        reject(apiErrorFromBody(xhr.status, body, xhr.getResponseHeader('X-Request-ID')))
      }
    }
    xhr.onerror = () => reject(networkError())
    xhr.onabort = () => reject(new DOMException('Загрузка отменена', 'AbortError'))
    signal?.addEventListener('abort', () => xhr.abort(), { once: true })
    xhr.send(file)
  })
}

export const ACTIVE_STATUSES: ReadonlySet<ExtractionStatus> = new Set(['pending', 'processing'])
