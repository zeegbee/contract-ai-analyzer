import type { RouteObject } from 'react-router'

import { NotFoundPage } from '../pages/NotFoundPage'
import { StatusPage } from '../pages/StatusPage'
import { Layout } from './Layout'

export const routes: RouteObject[] = [
  {
    path: '/',
    element: <Layout />,
    children: [
      { index: true, element: <StatusPage /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]
