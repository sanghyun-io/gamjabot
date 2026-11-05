import { useEffect, useRef, type FormEvent, type KeyboardEvent, type PointerEvent } from 'react'

export function useImeFormGuard(scope: string) {
  const composing = useRef(false), until = useRef(0)
  const pointerSubmit = useRef<{ control: HTMLElement; until: number } | null>(null)
  useEffect(() => { composing.current = false; until.current = 0; pointerSubmit.current = null }, [scope])
  return {
    isSubmitBlocked(event: FormEvent) {
      const submitter = (event.nativeEvent as SubmitEvent).submitter
      const pointer = pointerSubmit.current
      pointerSubmit.current = null
      // Distinguish explicit button activation from input Enter's implicit submit.
      if (submitter && (pointer && pointer.control === submitter && performance.now() < pointer.until
        || submitter === document.activeElement && submitter.matches('button, input[type="submit"], input[type="image"]'))) return false
      return composing.current || performance.now() < until.current
    },
    handlers: {
      onCompositionStartCapture: () => { composing.current = true; until.current = 0; pointerSubmit.current = null },
      onCompositionEndCapture: () => { composing.current = false; until.current = performance.now() + 100 },
      onBlurCapture: () => { composing.current = false; until.current = 0 },
      onPointerDownCapture: (event: PointerEvent<HTMLFormElement>) => {
        pointerSubmit.current = null
        if (event.button !== 0 || !(event.target instanceof Element)) return
        const control = event.target.closest('button, input[type="submit"], input[type="image"]')
        if ((control instanceof HTMLButtonElement || control instanceof HTMLInputElement)
          && !control.disabled && control.form === event.currentTarget && (control.type === 'submit' || control.type === 'image')) {
          pointerSubmit.current = { control, until: performance.now() + 1000 }
        }
      },
      onKeyDownCapture: (event: KeyboardEvent<HTMLFormElement>) => {
        pointerSubmit.current = null
        const active = event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229 || composing.current
        if (active) until.current = performance.now() + 100
        if (active && ['Enter', 'Escape', 'Tab', 'ArrowUp', 'ArrowDown'].includes(event.key)
          || performance.now() < until.current && ['Enter', 'Escape'].includes(event.key)) {
          // Keep the native IME default; stop app-level key handlers only.
          event.stopPropagation()
        }
      },
    },
  }
}
