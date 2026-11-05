import { useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { directoryViewGeneration, readDirectoryView, writeDirectoryView } from './directoryViewMemory'

export function useDirectoryValue<T extends string | boolean>(identity: string, key: string, initial: T): [T, (value: T) => void] {
  const generation = directoryViewGeneration()
  const [state, setState] = useState(() => ({ identity, key, generation, value: readDirectoryView(identity, key, initial) }))
  const value = state.identity === identity && state.key === key && state.generation === generation
    ? state.value : readDirectoryView(identity, key, initial)
  const change = (next: T) => {
    if (!writeDirectoryView(identity, key, next, generation)) return
    setState(current => current.identity === identity && current.key === key && current.generation === generation && current.value === next
      ? current : { identity, key, generation, value: next })
  }
  return [value, change]
}

type Position = { view: string; card: string | null; offset: number; top: number }
function capturePosition(root: HTMLElement, scroller: HTMLElement, page: string, view: string): Position {
  const boundary = scroller.getBoundingClientRect()
  const card = Array.from(root.querySelectorAll<HTMLElement>(`[id^="${page}-card-"]`)).find(node => {
    const box = node.getBoundingClientRect()
    return box.height > 0 && box.bottom > boundary.top && box.top < boundary.bottom
  })
  return { view, card: card?.id || null, offset: card ? card.getBoundingClientRect().top - boundary.top : 0, top: scroller.scrollTop }
}

export function useDirectoryPosition(identity: string, page: 'project' | 'agent' | 'connection', view: string,
  rootRef: RefObject<HTMLElement | null>, ready: boolean, enabled = true) {
  const generation = directoryViewGeneration()
  const context = `${identity}:${generation}:${page}`
  const latestView = useRef(view)
  latestView.current = view
  const memory = useRef<{ context: string; pending: Position | null; latest: Position | null; interacted: boolean }>({
    context, pending: readDirectoryView<Position | null>(identity, `position:${page}`, null), latest: null, interacted: false,
  })
  if (memory.current.context !== context) memory.current = {
    context, pending: readDirectoryView<Position | null>(identity, `position:${page}`, null), latest: null, interacted: false,
  }
  useLayoutEffect(() => {
    const root = rootRef.current
    const scroller = root?.closest<HTMLElement>('.app-frame__content')
    if (!root || !scroller || !enabled) return
    const state = memory.current
    const capture = () => {
      if (directoryViewGeneration() !== generation || !root.isConnected) return
      state.latest = capturePosition(root, scroller, page, latestView.current)
    }
    const interact = () => { state.interacted = true; state.pending = null; capture() }
    const key = (event: KeyboardEvent) => {
      if (['Tab', 'ArrowDown', 'ArrowUp', 'PageDown', 'PageUp', 'Home', 'End', ' '].includes(event.key)) interact()
    }
    scroller.addEventListener('scroll', capture, { passive: true })
    scroller.addEventListener('wheel', interact, { passive: true })
    scroller.addEventListener('touchstart', interact, { passive: true })
    root.addEventListener('pointerdown', interact, true)
    root.addEventListener('keydown', key, true)
    root.addEventListener('input', interact, true)
    return () => {
      // The next route may already be changing the shared scroller's size/class.
      // Prefer the position captured while this directory was actually visible.
      if (!state.latest) capture()
      if (state.latest && !state.pending) writeDirectoryView(identity, `position:${page}`, state.latest, generation)
      scroller.removeEventListener('scroll', capture)
      scroller.removeEventListener('wheel', interact)
      scroller.removeEventListener('touchstart', interact)
      root.removeEventListener('pointerdown', interact, true)
      root.removeEventListener('keydown', key, true)
      root.removeEventListener('input', interact, true)
    }
  }, [context, enabled, rootRef])

  useLayoutEffect(() => {
    const root = rootRef.current
    const scroller = root?.closest<HTMLElement>('.app-frame__content')
    const state = memory.current
    if (!enabled || !ready || !root || !scroller) return
    if (!state.pending || state.interacted) {
      state.latest = capturePosition(root, scroller, page, view)
      return
    }
    const position = state.pending
    state.pending = null
    if (position.view !== view) return
    const card = position.card ? Array.from(root.querySelectorAll<HTMLElement>(`[id^="${page}-card-"]`)).find(node => node.id === position.card && node.getBoundingClientRect().height > 0) : null
    if (card) scroller.scrollTop += card.getBoundingClientRect().top - scroller.getBoundingClientRect().top - position.offset
    else scroller.scrollTop = position.top
    state.latest = capturePosition(root, scroller, page, view)
    // Do not move focus or wait for later image/detail resizes; a new interaction wins.
  }, [context, view, ready, enabled, rootRef])
}
