const dateTime = new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'short' })

const number = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 })

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} Б`
  if (bytes < 1024 * 1024) return `${number.format(bytes / 1024)} КБ`
  return `${number.format(bytes / (1024 * 1024))} МБ`
}

export function formatDateTime(value: string | null | undefined, fallback = '—'): string {
  if (!value) return fallback
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? fallback : dateTime.format(date)
}
