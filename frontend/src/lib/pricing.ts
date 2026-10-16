import type { Pricing } from '../api/catalog'
import { ru } from '../i18n/ru'

/** Цена модели за 1 млн токенов с источником; неизвестная цена не подменяется оценкой. */
export function pricingText(pricing: Pricing | null): string {
  const t = ru.llm.models
  if (!pricing) return t.pricingUnknown
  return `${pricing.prompt_per_mtok} / ${pricing.completion_per_mtok} ${pricing.currency} · ${t.pricingSource[pricing.source]}`
}
