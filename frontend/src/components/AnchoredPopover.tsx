import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { constrainPopoverViewport, popoverPosition } from '../lib/popoverPosition'

interface Props {
  anchor: HTMLElement | null
  boundary?: HTMLElement | null
  children: ReactNode
  onClose: (restoreFocus?: boolean) => void
  className: string
  label?: string
  menu?: boolean
  preferred?: 'above' | 'below'
  align?: 'start' | 'end'
  minWidth?: number
  maxWidth?: number
  maxHeight?: number
  contentKey?: string
}

export default function AnchoredPopover({ anchor, boundary, children, onClose, className, label, menu = false,
  preferred = 'below', align = 'end', minWidth = 176, maxWidth = 360, maxHeight = 280, contentKey = '' }: Props) {
  const panel = useRef<HTMLDivElement>(null)
  const close = useRef(onClose)
  close.current = onClose
  const [position, setPosition] = useState<ReturnType<typeof popoverPosition> | null>(null)
  const viewport = () => ({ left: window.visualViewport?.offsetLeft || 0, top: window.visualViewport?.offsetTop || 0,
    width: window.visualViewport?.width || window.innerWidth, height: window.visualViewport?.height || window.innerHeight })

  useLayoutEffect(() => {
    if (!anchor || !panel.current) return
    const measure = () => {
      if (!anchor.isConnected || anchor.closest('[inert], [hidden]')) { close.current(false); return }
      const node = panel.current
      if (!node) return
      const region = boundary?.getBoundingClientRect()
      const bounds = region && region.width > 0 && region.height > 0 ? constrainPopoverViewport(viewport(), region) : viewport()
      if (bounds.width <= 16 || bounds.height <= 16) { close.current(false); return }
      node.style.width = 'max-content'
      node.style.minWidth = `${Math.max(0, Math.min(minWidth, bounds.width - 16))}px`
      node.style.maxWidth = `${Math.max(0, Math.min(maxWidth, bounds.width - 16))}px`
      node.style.maxHeight = `${Math.max(0, Math.min(maxHeight, bounds.height - 16))}px`
      const rect = node.getBoundingClientRect()
      const next = popoverPosition(anchor.getBoundingClientRect(), { width: rect.width, height: rect.height }, bounds, preferred, align, maxHeight)
      node.style.width = `${next.width}px`
      node.style.maxHeight = `${next.maxHeight}px`
      setPosition((current) => current && current.top === next.top && current.left === next.left && current.width === next.width
        && current.maxHeight === next.maxHeight && current.placement === next.placement ? current : next)
    }
    measure()
    const resize = () => { if (menu) close.current(false); else measure() }
    const scroll = (event: Event) => {
      if (event.target instanceof Node && panel.current?.contains(event.target)) return
      if (menu) close.current(false); else measure()
    }
    window.addEventListener('resize', resize)
    window.addEventListener('scroll', scroll, true)
    window.visualViewport?.addEventListener('resize', resize)
    window.visualViewport?.addEventListener('scroll', scroll)
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    observer?.observe(anchor)
    if (boundary) observer?.observe(boundary)
    observer?.observe(panel.current)
    return () => {
      window.removeEventListener('resize', resize)
      window.removeEventListener('scroll', scroll, true)
      window.visualViewport?.removeEventListener('resize', resize)
      window.visualViewport?.removeEventListener('scroll', scroll)
      observer?.disconnect()
    }
  }, [anchor, boundary, menu, preferred, align, minWidth, maxWidth, maxHeight, contentKey])

  const placed = position !== null
  useEffect(() => {
    if (!anchor || !placed) return
    if (menu) panel.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()
    const outside = (event: Event) => {
      if (event.target instanceof Node && !anchor.contains(event.target) && !panel.current?.contains(event.target)) close.current(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || !menu || panel.current?.closest('[inert]')) return
      event.preventDefault()
      event.stopPropagation()
      close.current(true)
    }
    document.addEventListener('pointerdown', outside, true)
    if (menu) document.addEventListener('focusin', outside)
    document.addEventListener('keydown', escape, true)
    return () => {
      document.removeEventListener('pointerdown', outside, true)
      document.removeEventListener('focusin', outside)
      document.removeEventListener('keydown', escape, true)
    }
  }, [anchor, menu, placed])

  if (!anchor) return null
  return createPortal(<div ref={panel} className={`anchored-popover ${className}`} role={menu ? 'menu' : undefined}
    aria-label={label} style={{ top: position?.top, left: position?.left, width: position?.width,
      maxHeight: position?.maxHeight, visibility: position ? 'visible' : 'hidden' }} onKeyDown={(event) => {
      if (!menu) return
      const buttons = [...(panel.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') || [])]
      if (event.key === 'Tab') { event.preventDefault(); close.current(true); return }
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key) || !buttons.length) return
      event.preventDefault()
      const current = buttons.indexOf(document.activeElement as HTMLButtonElement)
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
        : (current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length
      buttons[next].focus()
    }}>{children}</div>, document.body)
}
