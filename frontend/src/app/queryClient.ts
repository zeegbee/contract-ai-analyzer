import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query'

import { ME_QUERY_KEY } from '../api/auth'
import { ApiError } from '../api/client'

export function createQueryClient() {
  // Сессия истекла/отозвана или требуется смена пароля — перечитываем текущего пользователя,
  // маршрутизация сама перенаправит на вход или смену пароля.
  const onError = (error: unknown) => {
    if (!(error instanceof ApiError)) return
    if (error.status === 401) client.setQueryData(ME_QUERY_KEY, null)
    else if (error.code === 'password_change_required') void client.invalidateQueries({ queryKey: ME_QUERY_KEY })
  }
  const client: QueryClient = new QueryClient({
    queryCache: new QueryCache({ onError }),
    mutationCache: new MutationCache({ onError }),
    defaultOptions: {
      queries: {
        // Ошибки клиента (4xx) не повторяем — повтор не изменит результат.
        retry: (count, error) =>
          !(error instanceof ApiError && error.status >= 400 && error.status < 500) && count < 2,
        refetchOnWindowFocus: false,
      },
    },
  })
  return client
}
