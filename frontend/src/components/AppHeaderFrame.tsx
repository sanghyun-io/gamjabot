import { useI18n } from '../i18n'
import { createContext, useCallback, useContext, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

const HeaderTarget = createContext<((node: HTMLDivElement | null) => void) | null>(null)

export function useHostedHeader() { return useContext(HeaderTarget) !== null }

export function HeaderControlsSlot() {
  const attach = useContext(HeaderTarget)
  return attach ? <div ref={attach} className="app-header-controls-slot" /> : null
}

/** Keep one controls tree alive while its owned DOM mount moves between route headers. */
export function AppHeaderFrame({ children, controls, room = false }: {
  children: ReactNode; controls: ReactNode; room?: boolean
}) {
  const { t } = useI18n()
  const content = useRef<HTMLDivElement>(null)
  const [target, setTarget] = useState<HTMLDivElement | null>(null)
  const [mount] = useState(() => {
    const node = document.createElement('div')
    node.className = 'app-header-controls'
    return node
  })
  const fallback = useRef<HTMLDivElement>(null)
  const attach = useCallback((node: HTMLDivElement | null) => { setTarget(node) }, [])
  const integrated = target !== null && target.isConnected
  useLayoutEffect(() => {
    const host = target?.isConnected ? target : fallback.current
    host?.appendChild(mount)
    return () => { mount.remove() }
  }, [target, mount])

  const skipToContent = () => {
    const host = content.current
    if (!host?.isConnected || document.visibilityState !== 'visible' || !document.hasFocus()
        || document.querySelector('[aria-modal="true"], dialog[open]')) return
    const target = [...host.querySelectorAll<HTMLElement>('main, [role="main"], form')]
      .find(node => node.getClientRects().length > 0 && !node.closest('[hidden], [inert], [aria-hidden="true"]')) ?? host
    if (!target.getClientRects().length || target.closest('[hidden], [inert], [aria-hidden="true"]')) return
    // Programmatic landmark focus only: do not add a stop to ordinary Tab order.
    if (!target.hasAttribute('tabindex')) target.tabIndex = -1
    target.focus({ preventScroll: true })
    target.scrollIntoView({ block: 'start', inline: 'nearest' })
  }

  return <HeaderTarget.Provider value={attach}>
    <div className={`app-frame${integrated ? ' app-frame--integrated-header' : ''}`}>
      <button type="button" className="app-skip-content" onClick={skipToContent}>{t('app.skipToContent')}</button>
      <div ref={fallback} className="locale-toolbar" hidden={integrated} />
      <div ref={content} className={`app-frame__content${room ? ' app-frame__content--room' : ''}`}>{children}</div>
      {createPortal(controls, mount)}
    </div>
  </HeaderTarget.Provider>
}
