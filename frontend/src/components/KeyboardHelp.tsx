import { useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Keyboard, X } from 'lucide-react'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { useDialogFocus } from '../lib/useDialogFocus'
import { KeyboardReadingContent } from './KeyboardReadingGuide'

function HelpDialog({ id, onClose }: { id: string; onClose: () => void }) {
  const { t } = useI18n(), root = useRef<HTMLElement>(null)
  useDialogFocus(root, onClose)
  return createPortal(<div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
    <section ref={root} id={id} className="keyboard-help-dialog" role="dialog" aria-modal="true" aria-labelledby={`${id}-title`} aria-describedby={`${id}-scope`}>
      <header className="split"><h2 id={`${id}-title`} tabIndex={0} data-dialog-initial className="section-title">{t('keyboardReading.title')}</h2>
        <button type="button" className="button button--ghost button--compact" aria-label={t('common.close')} onClick={onClose}><X size={18} aria-hidden="true"/></button></header>
      <p id={`${id}-scope`} className="field-hint">{t('keyboardReading.inPlace')}</p>
      <KeyboardReadingContent/>
      <div><button type="button" className="button button--secondary button--compact" onClick={onClose}>{t('common.close')}</button></div>
    </section>
  </div>, document.body)
}

// Static guidance only. No navigation, shortcut activation, request or edit.
export default function KeyboardHelp({ identity }: { identity: string }) {
  const { t } = useI18n(), id = useId(), trigger = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const show = () => {
    const auth = useAuthStore.getState(), button = trigger.current
    if (!auth.token || `${auth.user?.id || 0}:${auth.epoch}` !== identity || !button?.isConnected
      || !button.getClientRects().length || button.closest('[hidden],[inert],[aria-hidden="true"]')
      || document.visibilityState !== 'visible' || !document.hasFocus()) return
    const modal = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"],dialog[open]'))
      .some(node => node.getClientRects().length && !node.closest('[hidden],[aria-hidden="true"]'))
    if (modal) return
    // Capture the explicit launcher even on platforms where pointer clicks do
    // not focus buttons. The shared dialog hook restores this live control.
    button.focus({ preventScroll: true }); setOpen(true)
  }
  return <>
    <button ref={trigger} type="button" className="button button--ghost button--compact" aria-label={t('keyboardReading.title')}
      title={t('keyboardReading.title')} aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? id : undefined} onClick={show}><Keyboard size={18} aria-hidden="true"/></button>
    {open && <HelpDialog id={id} onClose={() => setOpen(false)}/>}
  </>
}
