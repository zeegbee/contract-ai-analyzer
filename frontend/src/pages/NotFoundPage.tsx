import { Link } from 'react-router'

import { ru } from '../i18n/ru'

export function NotFoundPage() {
  return (
    <section className="space-y-3">
      <h1 className="text-xl font-semibold">{ru.notFound.title}</h1>
      <Link to="/" className="text-blue-700 underline">
        {ru.notFound.back}
      </Link>
    </section>
  )
}
