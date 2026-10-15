import { setupServer } from 'msw/node'

/** MSW-сервер для тестов. Обработчики задаются в каждом тесте — фиктивных данных по умолчанию нет. */
export const server = setupServer()
