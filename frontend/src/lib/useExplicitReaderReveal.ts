import { useEffect, useLayoutEffect, useRef } from 'react'

// Only a response to an explicit read may move the view, and only while its
// foreground intent survives. A cancelled reveal never cancels the read itself.
export function useExplicitReaderReveal(readId: number | null, canUse: () => boolean) {
  const target = useRef<HTMLDivElement>(null), source = useRef<HTMLElement | null>(null)
  const pending = useRef<{ origin: HTMLElement; auto: boolean } | null>(null)
  const cleanup = useRef<(() => void) | null>(null), live = useRef(canUse)
  live.current = canUse
  const cancel = () => { cleanup.current?.(); cleanup.current = null; pending.current = null }
  const visible = (node: HTMLElement | null): node is HTMLElement => !!node?.isConnected && node.getClientRects().length > 0
  const jump = () => {
    if (!live.current() || !visible(target.current)) return false
    target.current.scrollIntoView({ block: 'start', inline: 'nearest' }); target.current.focus({ preventScroll: true })
    return true
  }
  const back = () => {
    if (!live.current() || !visible(source.current) || source.current instanceof HTMLButtonElement && source.current.disabled) return false
    source.current.scrollIntoView({ block: 'nearest', inline: 'nearest' }); source.current.focus({ preventScroll: true })
    return true
  }
  const begin = (origin: HTMLElement) => {
    cancel(); pending.current = { origin, auto: true }
    const stop = () => { if (pending.current) pending.current.auto = false; cleanup.current?.(); cleanup.current = null }
    const focus = (event: FocusEvent) => { if (event.target !== origin && event.target !== document.body) stop() }
    document.addEventListener('pointerdown', stop, true)
    document.addEventListener('keydown', stop, true)
    document.addEventListener('wheel', stop, { capture: true, passive: true })
    document.addEventListener('focusin', focus, true)
    document.addEventListener('visibilitychange', stop)
    window.addEventListener('blur', stop)
    cleanup.current = () => {
      document.removeEventListener('pointerdown', stop, true)
      document.removeEventListener('keydown', stop, true)
      document.removeEventListener('wheel', stop, true)
      document.removeEventListener('focusin', focus, true)
      document.removeEventListener('visibilitychange', stop)
      window.removeEventListener('blur', stop)
    }
  }
  useLayoutEffect(() => {
    if (readId === null) { cancel(); source.current = null; return }
    const intent = pending.current
    cancel()
    if (!intent) return
    source.current = intent.origin
    if (intent.auto && live.current() && document.visibilityState === 'visible' && document.hasFocus()
      && visible(intent.origin) && (document.activeElement === intent.origin || document.activeElement === document.body)) jump()
  }, [readId])
  useEffect(() => () => { cancel(); source.current = null }, [])
  return { target, begin, cancel, jump, back }
}
