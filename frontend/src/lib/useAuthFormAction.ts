import { useEffect, useRef, useState } from 'react'
import { useAuthStore } from '../stores/authStore'

type Kind = 'login' | 'restore' | 'register'
export type AuthFormAction = Readonly<{ isCurrent: () => boolean }>

// View-local admission only. Never retain credentials or change native saved-login policy.
export function useAuthFormAction(viewKey: string) {
  const epoch = useAuthStore(state => state.epoch)
  const owner = useAuthStore(state => state.user?.id ?? null)
  const scope = `${viewKey}:${epoch}:${owner ?? 0}`
  const scopeRef = useRef(scope); scopeRef.current = scope
  const mounted = useRef(false), active = useRef<AuthFormAction | null>(null)
  const [kind, setKind] = useState<Kind | null>(null)
  useEffect(() => {
    mounted.current = true; active.current = null; setKind(null)
    return () => { mounted.current = false; active.current = null }
  }, [scope])
  const begin = (next: Kind): AuthFormAction | null => {
    const auth = useAuthStore.getState()
    if (!mounted.current || active.current || auth.epoch !== epoch || (auth.user?.id ?? null) !== owner) return null
    const action: AuthFormAction = Object.freeze({ isCurrent: () => {
      const live = useAuthStore.getState()
      return mounted.current && active.current === action && scopeRef.current === scope
        && live.epoch === epoch && (live.user?.id ?? null) === owner
    } })
    active.current = action; setKind(next); return action
  }
  const finish = (action: AuthFormAction) => {
    if (active.current !== action) return
    active.current = null
    if (mounted.current && scopeRef.current === scope) setKind(null)
  }
  return { kind, scope, begin, finish }
}
