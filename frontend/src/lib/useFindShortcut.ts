import { useEffect, useLayoutEffect, useRef } from 'react'

// Listen within this app window while the reading view is enabled, including
// when navigation left focus on body or the sidebar rather than inside the view.
// Exactly one Meta/Control modifier; combined chords remain available to the OS.
// Each view still decides whether its current Owner/content can handle the key.
export function useFindShortcut(enabled: boolean, handle: (event: KeyboardEvent) => boolean, options: { modalBoundary?: () => HTMLElement | null } = {}) {
  const latest = useRef({ enabled, handle, options })
  useLayoutEffect(() => {
    latest.current = { enabled, handle, options }
    return () => { latest.current = { enabled: false, handle, options } }
  })
  useEffect(() => {
    if (!enabled) return
    const key = (event: KeyboardEvent) => {
      if (!latest.current.enabled || event.defaultPrevented || event.metaKey === event.ctrlKey || event.altKey || event.shiftKey
        || event.key.toLowerCase() !== 'f' || event.isComposing || event.keyCode === 229
        || document.visibilityState !== 'visible' || !document.hasFocus()) return
      if (document.querySelector('[aria-modal="true"]')) {
        // Existing callers remain blocked by dialogs. Only an explicitly scoped
        // reader can opt into its own current, topmost visible dialog.
        const allowed = latest.current.options.modalBoundary?.()
        const dialogs = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).filter(node => node.isConnected && node.getClientRects().length > 0 && !node.closest('[hidden], [inert], [aria-hidden="true"]'))
        if (!allowed || dialogs.at(-1) !== allowed) return
      }
      if (latest.current.handle(event)) { event.preventDefault(); event.stopPropagation() }
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [enabled])
}
