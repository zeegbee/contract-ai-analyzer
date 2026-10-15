/** Тексты интерфейса. Все строки UI — здесь, чтобы их можно было проверять и менять централизованно. */
export const ru = {
  appName: 'Contract AI Analyzer',
  disclaimer:
    'Результаты анализа — предварительная поддержка работы с договором и не заменяют консультацию юриста.',
  nav: {
    status: 'Состояние системы',
  },
  status: {
    title: 'Состояние системы',
    loading: 'Проверяем состояние сервисов…',
    ready: 'Все сервисы готовы к работе',
    notReady: 'Часть сервисов недоступна',
    refresh: 'Обновить',
    version: 'Версия',
    checks: {
      database: 'База данных',
      migrations: 'Миграции схемы',
      storage: 'Файловое хранилище',
    } as Record<string, string>,
    values: {
      ok: 'в порядке',
      pending: 'не применены',
      unavailable: 'недоступно',
      unknown: 'неизвестно',
    } as Record<string, string>,
  },
  errors: {
    requestId: 'Код запроса',
    retry: 'Повторить',
  },
  notFound: {
    title: 'Страница не найдена',
    back: 'На главную',
  },
} as const
