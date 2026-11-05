import { useEffect, useRef } from 'react'

// Navigation permission belongs to one live view action. Once focus/context moves,
// returning to the same screen never revives it; a new explicit action must begin it.
export function useViewNavigationIntent() {
  const mounted = useRef(false), intent = useRef<{ id: string; boundary: HTMLElement | null; allowed: boolean } | null>(null)
  const api = useRef({
    begin: (id: string, boundary: HTMLElement | null) => { intent.current = { id, boundary, allowed: mounted.current && !!boundary?.isConnected && document.visibilityState === 'visible' && document.hasFocus() } },
    cancel: () => { if (intent.current) intent.current.allowed = false },
    current: (id: string) => mounted.current && intent.current?.id === id && intent.current.allowed && !!intent.current.boundary?.isConnected && document.visibilityState === 'visible' && document.hasFocus(),
  }).current
  useEffect(() => {
    mounted.current = true
    const moved = (event: Event) => {
      if (!intent.current || event.type === 'focusin' && event.target === document.body) return
      if (event.target instanceof Node && !intent.current.boundary?.contains(event.target)) api.cancel()
    }
    const hidden = () => { if (document.visibilityState !== 'visible') api.cancel() }
    document.addEventListener('pointerdown', moved, true); document.addEventListener('focusin', moved, true)
    document.addEventListener('visibilitychange', hidden); window.addEventListener('blur', api.cancel)
    return () => {
      mounted.current = false; api.cancel(); intent.current = null
      document.removeEventListener('pointerdown', moved, true); document.removeEventListener('focusin', moved, true)
      document.removeEventListener('visibilitychange', hidden); window.removeEventListener('blur', api.cancel)
    }
  }, [api])
  return api
}
