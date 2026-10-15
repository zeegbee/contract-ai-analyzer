import { useQuery } from '@tanstack/react-query'
import { createContext, useContext } from 'react'

import { fetchMe, ME_QUERY_KEY, type Me } from '../api/auth'

export function useMe() {
  return useQuery({ queryKey: ME_QUERY_KEY, queryFn: ({ signal }) => fetchMe(signal), staleTime: 60_000 })
}

/**
 * Пользователь, переданный RequireAuth. Контекст (а не прямое чтение кеша) нужен, чтобы при истечении
 * сессии дочерние страницы не перерисовывались без пользователя раньше, чем RequireAuth уведёт на вход.
 */
export const CurrentUserContext = createContext<Me | null>(null)

export function useCurrentUser(): Me {
  const me = useContext(CurrentUserContext)
  if (!me) throw new Error('useCurrentUser вне RequireAuth')
  return me
}

/** Разрешён только внутренний путь возврата после входа (защита от open redirect). */
export function safeNextPath(value: string | null): string {
  if (!value || !value.startsWith('/') || value.startsWith('//') || value.startsWith('/\\')) return '/'
  return value
}

export const CHANGE_PASSWORD_PATH = '/change-password'
