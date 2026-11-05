import { useLayoutEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { useI18n } from '../i18n'
import { useDialogFocus } from '../lib/useDialogFocus'

export default function RoomManagementDialog({ id, children, onClose, onStop, stopPending = false }: {
  id: string
  children: ReactNode
  onClose: () => void
  onStop?: () => void
  stopPending?: boolean
}) {
  const backdrop = useRef<HTMLDivElement>(null)
  const dialog = useRef<HTMLElement>(null)
  const { t } = useI18n()

  useLayoutEffect(() => {
    const overflow = document.body.style.overflow
    const background = [...document.body.children].filter((node) => node !== backdrop.current)
      .map((node) => ({ node, inert: node.hasAttribute('inert'), hidden: node.getAttribute('aria-hidden') }))
    for (const { node } of background) { node.setAttribute('inert', ''); node.setAttribute('aria-hidden', 'true') }
    document.body.style.overflow = 'hidden'
    // Layout cleanup precedes the shared focus hook's passive cleanup.
    return () => {
      document.body.style.overflow = overflow
      for (const item of background) {
        if (!item.inert) item.node.removeAttribute('inert')
        if (item.hidden === null) item.node.removeAttribute('aria-hidden')
        else item.node.setAttribute('aria-hidden', item.hidden)
      }
    }
  }, [])
  useLayoutEffect(() => {
    const root = dialog.current
    if (!root) return
    // Only sibling top-level sections form the accordion; nested details stay usable.
    const toggle = (event: Event) => {
      const section = event.target
      if (!(section instanceof HTMLDetailsElement) || !section.open || section.parentElement?.closest('details')) return
      for (const other of root.querySelectorAll<HTMLDetailsElement>('details[open]')) {
        if (other !== section && !other.parentElement?.closest('details')) other.open = false
      }
    }
    root.addEventListener('toggle', toggle, true)
    return () => root.removeEventListener('toggle', toggle, true)
  }, [])
  useDialogFocus(dialog, onClose)

  return createPortal(<div ref={backdrop} className="modal-backdrop room-management-backdrop"
    onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section ref={dialog} id={id} role="dialog" aria-modal="true" aria-labelledby={`${id}-title`}
      className="modal-panel room-management-dialog">
      <header className="room-management-dialog__header"><h2 id={`${id}-title`}>{t('chat.tools.open')}</h2>
        <div className="inline-actions">
          {onStop && <button type="button" className="button button--danger button--compact" disabled={stopPending} onClick={onStop}>{t('chat.stop')}</button>}
          <button type="button" data-dialog-initial className="icon-button" aria-label={t('common.close')} onClick={onClose}><X size={20} aria-hidden="true" /></button>
        </div>
      </header>
      <div className="room-management-dialog__body room-column">{children}</div>
    </section>
  </div>, document.body)
}
