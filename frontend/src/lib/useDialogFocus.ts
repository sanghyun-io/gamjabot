import { useEffect, useRef, type RefObject } from 'react'

function isVisible(element: HTMLElement, boundary: HTMLElement): boolean {
  let current: HTMLElement | null = element
  while (current && current !== boundary) {
    const style = window.getComputedStyle(current)
    if (style.display === 'none' || style.visibility === 'hidden') return false
    if (current.hasAttribute('inert') || current.hasAttribute('hidden')) return false
    
    // Check if current is a closed details element
    if (current.tagName === 'DETAILS' && !(current as HTMLDetailsElement).open) {
      const summary = (current as HTMLDetailsElement).querySelector(':scope > summary')
      // Element must be inside the summary to be visible
      if (!summary || !summary.contains(element)) return false
    }
    
    current = current.parentElement
  }
  return true
}

function isFocusableCandidate(el: HTMLElement): boolean {
  const tagName = el.tagName.toLowerCase()
  const type = el.getAttribute('type')
  
  // Type hidden inputs are never focusable
  if (tagName === 'input' && type === 'hidden') return false
  
  // Check disabled state (including disabled="" and disabled="disabled")
  const disabled = el.getAttribute('disabled')
  if (disabled !== null) return false
  
  // Check for negative tabindex (any negative value, not just -1)
  const tabindex = el.getAttribute('tabindex')
  if (tabindex !== null) {
    const tabindexValue = parseInt(tabindex, 10)
    if (tabindexValue < 0) return false
  }
  
  // Native disabled state includes fieldsets and their first legend exception.
  if (el.matches(':disabled')) return false
  
  return true
}

export function useDialogFocus(
  dialogRef: RefObject<HTMLElement | null>,
  onClose: () => void,
  options: { restoreFocus?: boolean } = {}
) {
  // Capture trigger at render time (stable across rerenders of same mount)
  const triggerRef = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null)
  // Always keep the latest onClose without re-running the main effect
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  // A scoped caller may own a stricter restoration intent; existing callers keep the default.
  const restoreFocusRef = useRef(options.restoreFocus !== false)
  restoreFocusRef.current = options.restoreFocus !== false

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return

    const selectors = [
      'button',
      'a[href]',
      'input:not([type="hidden"])',
      'select',
      'textarea',
      'summary',
      '[tabindex]'
    ]

    const getFocusable = (): HTMLElement[] => {
      const candidates = dialog.querySelectorAll<HTMLElement>(selectors.join(', '))
      return Array.from(candidates).filter(el => {
        // Exclude descendants of nested aria-modal dialogs
        const nestedModal = el.closest('[aria-modal="true"]')
        if (nestedModal && nestedModal !== dialog) return false
        // Verify element is a valid focus target for initial focus
        const isValidFocusTarget = isFocusableCandidate(el) && isVisible(el, dialog)
        return isValidFocusTarget
      })
    }

    const isTopmost = () => {
      const modals = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]'))
        .filter(modal => isVisible(modal, document.body))
      return modals[modals.length - 1] === dialog
    }

    const focusFirst = () => {
      const elements = getFocusable()
      if (elements.length > 0) elements[0].focus()
      else {
        dialog.setAttribute('tabindex', '-1')
        dialog.focus()
      }
    }

    // A parent effect can run after a nested dialog's effect.
    if (isTopmost()) {
      const focusable = getFocusable()
      const initial = dialog.querySelector<HTMLElement>('[data-dialog-initial]')
      if (initial && focusable.includes(initial)) initial.focus()
      else focusFirst()
    }

    // Native dialog Tab handling runs before React's bubbling input handlers.
    // Track the composing editor here so an IME candidate key cannot be mistaken
    // for dialog navigation, including WebKit's brief post-composition key gap.
    let composing = false, imeUntil = 0, imeTarget: EventTarget | null = null
    const clearIme = () => { composing = false; imeUntil = 0; imeTarget = null }
    const compositionStart = (event: CompositionEvent) => { composing = true; imeUntil = 0; imeTarget = event.target }
    const compositionEnd = (event: CompositionEvent) => { composing = false; imeUntil = performance.now() + 120; imeTarget = event.target }
    const compositionFocusOut = (event: FocusEvent) => { if (event.target === imeTarget) clearIme() }
    const compositionVisibility = () => { if (document.visibilityState !== 'visible') clearIme() }
    const imeHolds = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229) {
        imeTarget = event.target; imeUntil = performance.now() + 120
        return true
      }
      return event.target === imeTarget && (composing || performance.now() < imeUntil)
    }
    const guardImeKey = (event: KeyboardEvent) => {
      if (imeHolds(event) && (event.key === 'Tab' || event.key === 'Escape')) {
        // Preserve the native IME default. Suppress dialog/app key handlers only.
        event.stopPropagation()
      }
    }

    const handleTab = (event: KeyboardEvent) => {
      if (event.key !== 'Tab' || event.defaultPrevented || imeHolds(event)) return
      if (!isTopmost()) return
      const elements = getFocusable()

      event.preventDefault()

      if (elements.length === 0) {
        focusFirst()
        return
      }

      const first = elements[0]
      const last = elements[elements.length - 1]
      const active = document.activeElement
      const activeIndex = elements.indexOf(active as HTMLElement)

      // A programmatically focused heading/reading region may have tabindex=-1.
      // Continue beside it in DOM order instead of restarting at the dialog edge.
      if (activeIndex < 0 && active instanceof HTMLElement && dialog.contains(active)) {
        const adjacent = event.shiftKey
          ? [...elements].reverse().find(element => active.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_PRECEDING)
          : elements.find(element => active.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING)
        ;(adjacent ?? (event.shiftKey ? last : first)).focus()
        return
      }

      if (event.shiftKey) {
        if (activeIndex <= 0) {
          last.focus()
        } else {
          elements[activeIndex - 1].focus()
        }
      } else {
        if (activeIndex < 0 || activeIndex === elements.length - 1) {
          first.focus()
        } else {
          elements[activeIndex + 1].focus()
        }
      }
    }

    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || !isTopmost() || imeHolds(event)) return
      // One physical press closes at most one layer, even after a child unmounts.
      if (event.repeat) { event.preventDefault(); event.stopPropagation(); return }
      // Marked search controls clear their own query in React before Escape closes a dialog.
      // Leave other inputs unchanged; IME composition was handled above.
      if (event.target instanceof HTMLInputElement && event.target.matches('[data-dialog-search-clear]') && event.target.value) return
      event.preventDefault()
      onCloseRef.current()
    }

    const handleFocusIn = (event: FocusEvent) => {
      if (!isTopmost()) return
      const target = event.target as HTMLElement
      if (!dialog.contains(target)) {
        focusFirst()
      }
    }

    dialog.addEventListener('compositionstart', compositionStart, true)
    dialog.addEventListener('compositionend', compositionEnd, true)
    dialog.addEventListener('focusout', compositionFocusOut, true)
    dialog.addEventListener('keydown', guardImeKey, true)
    window.addEventListener('blur', clearIme)
    document.addEventListener('visibilitychange', compositionVisibility)
    dialog.addEventListener('keydown', handleTab)
    document.addEventListener('keydown', handleEscape)
    document.addEventListener('focusin', handleFocusIn, true)

    return () => {
      clearIme()
      dialog.removeEventListener('compositionstart', compositionStart, true)
      dialog.removeEventListener('compositionend', compositionEnd, true)
      dialog.removeEventListener('focusout', compositionFocusOut, true)
      dialog.removeEventListener('keydown', guardImeKey, true)
      window.removeEventListener('blur', clearIme)
      document.removeEventListener('visibilitychange', compositionVisibility)
      dialog.removeEventListener('keydown', handleTab)
      document.removeEventListener('keydown', handleEscape)
      document.removeEventListener('focusin', handleFocusIn, true)

      // Restore focus to trigger only if still connected, visible, and not inert
      const trigger = triggerRef.current
      if (
        restoreFocusRef.current &&
        trigger &&
        trigger.isConnected &&
        isVisible(trigger, document.body) &&
        !trigger.hasAttribute('inert') &&
        !trigger.matches(':disabled')
      ) {
        try { trigger.focus({ preventScroll: true }) } catch { trigger.focus() }
      }
    }
    // Dialog components mount conditionally; callback changes do not reset focus.
  }, [dialogRef])
}
