import { http, HttpResponse } from 'msw'

/** Пустые списки для страницы «Обзор» (без фиктивных данных). */
export const emptyOverviewHandlers = [
  http.get('*/api/analyses', () => HttpResponse.json({ items: [], next_cursor: null })),
  http.get('*/api/documents', () => HttpResponse.json({ items: [], next_cursor: null })),
]
