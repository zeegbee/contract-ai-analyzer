import type { UploadLimits } from '../api/documents'
import { ru } from '../i18n/ru'

const t = ru.documents.upload

/** Проверка в браузере — только для удобства; сервер проверяет формат, сигнатуру и размер сам. */
export function validateFile(file: File, limits: UploadLimits): string | null {
  const ext = file.name.includes('.') ? (file.name.split('.').pop() ?? '').toLowerCase() : ''
  if (!limits.extensions.includes(ext)) return t.badExtension
  if (file.size === 0) return t.emptyFile
  if (file.size > limits.max_upload_mb * 1024 * 1024) return t.tooLarge(limits.max_upload_mb)
  return null
}
