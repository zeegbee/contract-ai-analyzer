import { type ReactNode, useEffect, useId, useRef } from 'react'

/**
 * Модальное окно подтверждения: фокус переходит внутрь, Escape и «Отмена» закрывают окно без запроса.
 * <dialog open> с собственным затемнением: jsdom (тесты) не поддерживает showModal().
 */
export function Modal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const id = useId()
  const ref = useRef<HTMLDialogElement>(null)
  // Актуальный обработчик без перезапуска эффекта: иначе фокус возвращался бы в начало при каждом рендере.
  const close = useRef(onClose)
  useEffect(() => {
    close.current = onClose
  }, [onClose])
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    ref.current?.querySelector<HTMLElement>('input, button, select, textarea')?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close.current()
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      previous?.focus()
    }
  }, [])
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
      <dialog
        ref={ref}
        open
        aria-modal="true"
        aria-labelledby={id}
        className="static max-h-full w-full max-w-lg space-y-3 overflow-y-auto rounded-lg bg-white p-4 text-sm shadow-lg"
      >
        <h2 id={id} className="text-lg font-semibold">
          {title}
        </h2>
        {children}
      </dialog>
    </div>
  )
}
