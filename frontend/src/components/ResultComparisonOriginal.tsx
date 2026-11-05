import { useRef, useState, type ReactNode } from 'react'
import { useI18n } from '../i18n'

// Read an already received comparison body on demand. Keep the mounted reader
// after first opening so folding does not recreate its local reading state.
export default function ResultComparisonOriginal({ version, title, canUse, children }: {
  version: number; title: string; canUse: () => boolean; children: (canRead: () => boolean) => ReactNode
}) {
  const { t } = useI18n(), [openedOnce, setOpenedOnce] = useState(false)
  const root = useRef<HTMLDetailsElement>(null)
  const canRead = () => canUse() && !!root.current?.open && root.current.isConnected
    && !!root.current.getClientRects().length && !root.current.closest('[hidden], [inert], [aria-hidden="true"]')
  return <details ref={root} className="compact-details" onToggle={event => {
    if (event.target !== event.currentTarget || !event.currentTarget.open) return
    if (!canRead()) { event.currentTarget.open = false; return }
    setOpenedOnce(true)
  }}>
    <summary>{t('results.comparison.fullOriginal', { version })}</summary>
    {openedOnce && <div className="stack stack--tight">
      <p className="field-hint">{t('results.comparison.fullOriginalHelp')}</p>
      <strong>{title} · v{version}</strong>
      {children(canRead)}
    </div>}
  </details>
}
