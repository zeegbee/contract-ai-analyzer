import { useState } from 'react'

import { ru } from '../i18n/ru'
import { Button } from './ui'

interface Props {
  email: string
  password: string
  onDone: () => void
}

/** Одноразовый показ временного пароля. После закрытия он больше нигде не доступен. */
export function TemporaryPasswordNotice({ email, password, onDone }: Props) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(password)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }
  return (
    <section
      aria-labelledby="temp-password-title"
      className="space-y-3 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950"
    >
      <h2 id="temp-password-title" className="font-semibold">
        {ru.users.temporaryPassword.title}: {email}
      </h2>
      <p>{ru.users.temporaryPassword.notice}</p>
      <p className="flex flex-wrap items-center gap-3">
        <code data-testid="temporary-password" className="rounded bg-white px-2 py-1 font-mono text-base select-all">
          {password}
        </code>
        <Button onClick={() => void copy()}>{copied ? ru.common.copied : ru.common.copy}</Button>
      </p>
      <Button variant="primary" onClick={onDone}>
        {ru.users.temporaryPassword.done}
      </Button>
    </section>
  )
}
