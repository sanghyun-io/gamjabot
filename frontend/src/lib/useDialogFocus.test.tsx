// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode, useRef, useState, type ReactNode } from 'react'
import { useDialogFocus } from './useDialogFocus'

function TestDialog({ onClose, children, open = true }: { onClose: () => void; children?: ReactNode; open?: boolean }) {
  const dialogRef = useRef<HTMLDivElement>(null)
  useDialogFocus(dialogRef, onClose)
  if (!open) return null
  return (
    <div ref={dialogRef} role="dialog" aria-modal="true" aria-label="Test dialog" tabIndex={-1}>
      {children}
      <button data-dialog-initial>Initial</button>
      <button>Second</button>
      <input type="text" />
      <button disabled>Disabled</button>
      <button hidden>Hidden</button>
      <div hidden><button>In hidden parent</button></div>
      <div inert><button>In inert</button></div>
      <details>
        <summary>Summary in closed details</summary>
        <button>In closed details</button>
      </details>
      <details open>
        <summary>Summary in open details</summary>
        <button>In open details</button>
      </details>
      <button>Last</button>
    </div>
  )
}

function NestedPanel({ onClose }: { onClose: () => void }) {
  const nestedRef = useRef<HTMLDivElement>(null)
  useDialogFocus(nestedRef, onClose)
  return (
    <div ref={nestedRef} role="dialog" aria-modal="true" aria-label="Nested dialog">
      <input data-dialog-initial defaultValue="nested" />
      <button onClick={onClose}>Close nested</button>
    </div>
  )
}

function NestedDialog({ onCloseOuter }: { onCloseOuter: () => void }) {
  const [nestedOpen, setNestedOpen] = useState(false)
  const outerRef = useRef<HTMLDivElement>(null)
  const closeNested = () => setNestedOpen(false)
  useDialogFocus(outerRef, onCloseOuter)
  return (
    <div ref={outerRef} role="dialog" aria-modal="true" aria-label="Outer dialog">
      <button onClick={() => setNestedOpen(true)}>Open nested</button>
      {nestedOpen && <NestedPanel onClose={closeNested} />}
    </div>
  )
}

afterEach(() => { cleanup() })

it('focuses data-dialog-initial element on mount', async () => {
  const onClose = vi.fn()
  const trigger = document.createElement('button')
  trigger.textContent = 'Trigger'
  document.body.appendChild(trigger)
  trigger.focus()
  render(<TestDialog onClose={onClose} />)
  await waitFor(() => expect(document.activeElement?.getAttribute('data-dialog-initial')).toBe('true'))
  document.body.removeChild(trigger)
})

function ZeroFocusDialog({ onClose }: { onClose: () => void }) {
  const dialogRef = useRef<HTMLDivElement>(null)
  useDialogFocus(dialogRef, onClose)
  return <div ref={dialogRef} role="dialog" aria-modal="true" aria-label="Zero focus"><div hidden><button>Hidden</button></div></div>
}

it('falls back to dialog element when no visible focusable elements', async () => {
  const onClose = vi.fn()
  const trigger = document.createElement('button')
  document.body.appendChild(trigger)
  trigger.focus()
  render(<ZeroFocusDialog onClose={onClose} />)
  await waitFor(() => expect(document.activeElement?.getAttribute('role')).toBe('dialog'))
  document.body.removeChild(trigger)
})

it('cycles Tab forward through visible controls including closed details summary', async () => {
  const user = userEvent.setup()
  const onClose = vi.fn()
  render(<TestDialog onClose={onClose} />)
  const initial = await screen.findByRole('button', { name: 'Initial' })
  await waitFor(() => expect(document.activeElement).toBe(initial))
  await user.tab()
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Second' }))
  await user.tab()
  expect(document.activeElement).toBe(screen.getByRole('textbox'))
  await user.tab()
  expect(document.activeElement).toBe(screen.getByText('Summary in closed details'))
  await user.tab()
  expect(document.activeElement).toBe(screen.getByText('Summary in open details'))
  await user.tab()
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'In open details' }))
  await user.tab()
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Last' }))
  await user.tab()
  expect(document.activeElement).toBe(initial)
})

it('cycles Shift+Tab backward through visible controls', async () => {
  const user = userEvent.setup()
  const onClose = vi.fn()
  render(<TestDialog onClose={onClose} />)
  const initial = await screen.findByRole('button', { name: 'Initial' })
  await waitFor(() => expect(document.activeElement).toBe(initial))
  await user.tab({ shift: true })
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Last' }))
  await user.tab({ shift: true })
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'In open details' }))
  await user.tab({ shift: true })
  expect(document.activeElement).toBe(screen.getByText('Summary in open details'))
})

it('calls onClose when Escape pressed on topmost dialog', async () => {
  const onClose = vi.fn()
  render(<TestDialog onClose={onClose} />)
  const initial = await screen.findByRole('button', { name: 'Initial' })
  await waitFor(() => expect(document.activeElement).toBe(initial))
  fireEvent.keyDown(initial, { key: 'Escape' })
  expect(onClose).toHaveBeenCalled()
})

it('excludes disabled, hidden, inert, and closed details content from tab order', async () => {
  const user = userEvent.setup()
  const onClose = vi.fn()
  render(<TestDialog onClose={onClose} />)
  const initial = await screen.findByRole('button', { name: 'Initial' })
  await waitFor(() => expect(document.activeElement).toBe(initial))
  
  // Cycle through all focusable elements and verify closed details content is excluded
  const focusableElements: HTMLElement[] = [initial]
  let current = initial
  for (let i = 0; i < 10; i++) {
    await user.tab()
    current = document.activeElement as HTMLElement
    if (current === initial) break
    focusableElements.push(current)
  }
  
  // Closed details button should not be in the cycle
  const closedDetailsBtn = screen.queryByRole('button', { name: 'In closed details' })
  expect(focusableElements).not.toContain(closedDetailsBtn)
  
  // Open details button should be in the cycle
  const openDetailsBtn = screen.getByRole('button', { name: 'In open details' })
  expect(focusableElements).toContain(openDetailsBtn)
})

it('outer dialog Escape does not close when nested dialog is open', async () => {
  const user = userEvent.setup()
  const onCloseOuter = vi.fn()
  render(<NestedDialog onCloseOuter={onCloseOuter} />)
  const openBtn = screen.getByRole('button', { name: 'Open nested' })
  await user.click(openBtn)
  const nestedInput = await screen.findByDisplayValue('nested')
  await waitFor(() => expect(document.activeElement).toBe(nestedInput))
  await user.keyboard('{Escape}')
  expect(onCloseOuter).not.toHaveBeenCalled()
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Nested dialog' })).toBeNull())
  await user.keyboard('{Escape}')
  expect(onCloseOuter).toHaveBeenCalled()
})

it('nested dialog Tab does not cycle to outer controls', async () => {
  const user = userEvent.setup()
  const onCloseOuter = vi.fn()
  render(<NestedDialog onCloseOuter={onCloseOuter} />)
  await user.click(screen.getByRole('button', { name: 'Open nested' }))
  const nestedInput = await screen.findByDisplayValue('nested')
  await waitFor(() => expect(document.activeElement).toBe(nestedInput))
  await user.tab()
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close nested' }))
  await user.tab()
  expect(document.activeElement).toBe(nestedInput)
})

it('restores trigger focus after unmount with preventScroll', async () => {
  const onClose = vi.fn()
  const trigger = document.createElement('button')
  trigger.textContent = 'Trigger'
  document.body.appendChild(trigger)
  trigger.focus()
  const { unmount } = render(<TestDialog onClose={onClose} />)
  unmount()
  await waitFor(() => expect(document.activeElement).toBe(trigger))
  document.body.removeChild(trigger)
})

it('maintains cleanup when onClose callback changes', async () => {
  const onClose1 = vi.fn()
  const onClose2 = vi.fn()
  const trigger = document.createElement('button')
  document.body.appendChild(trigger)
  trigger.focus()
  const { rerender, unmount } = render(<TestDialog onClose={onClose1} />)
  const second = screen.getByRole('button', { name: 'Second' })
  second.focus()
  rerender(<TestDialog onClose={onClose2} />)
  expect(document.activeElement).toBe(second)
  const initial = await screen.findByRole('button', { name: 'Initial' })
  fireEvent.keyDown(initial, { key: 'Escape' })
  expect(onClose2).toHaveBeenCalled()
  expect(onClose1).not.toHaveBeenCalled()
  unmount()
  await waitFor(() => expect(document.activeElement).toBe(trigger))
  document.body.removeChild(trigger)
})

it('keeps Tab and Escape handlers after StrictMode effect replay and restores the trigger', async () => {
  const user = userEvent.setup()
  const onClose = vi.fn()
  const trigger = document.createElement('button')
  document.body.appendChild(trigger)
  try {
    trigger.focus()
    const { unmount } = render(<StrictMode><TestDialog onClose={onClose} /></StrictMode>)
    const initial = screen.getByRole('button', { name: 'Initial' })
    expect(document.activeElement).toBe(initial)
    await user.tab({ shift: true })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Last' }))
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
    unmount()
    expect(document.activeElement).toBe(trigger)
  } finally { trigger.remove() }
})

it('prevents both Tab directions from leaving a dialog with no controls', () => {
  render(<ZeroFocusDialog onClose={vi.fn()} />)
  const dialog = screen.getByRole('dialog', { name: 'Zero focus' })
  for (const shiftKey of [false, true]) {
    const event = new KeyboardEvent('keydown', { key: 'Tab', shiftKey, bubbles: true, cancelable: true })
    dialog.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(dialog)
  }
})

function AlreadyNestedDialog() {
  const ref = useRef<HTMLDivElement>(null)
  useDialogFocus(ref, () => {})
  return <div ref={ref} role="dialog" aria-modal="true" aria-label="Already outer">
    <button data-dialog-initial>Older control</button>
    <NestedPanel onClose={() => {}} />
  </div>
}

it('keeps initial focus in an already mounted nested modal and rejects older-modal focus', () => {
  render(<AlreadyNestedDialog />)
  const nestedInput = screen.getByDisplayValue('nested')
  expect(document.activeElement).toBe(nestedInput)
  screen.getByRole('button', { name: 'Older control' }).focus()
  expect(document.activeElement).toBe(nestedInput)
})

function NativeDisabledDialog() {
  const ref = useRef<HTMLDivElement>(null)
  useDialogFocus(ref, () => {})
  return <div ref={ref} role="dialog" aria-modal="true" aria-label="Native disabled">
    <fieldset disabled>
      <legend><button>Legend control</button></legend>
      <button data-dialog-initial>Fieldset disabled</button>
      <input aria-label="Disabled field" />
    </fieldset>
    <button tabIndex={-2}>Negative index</button>
    <button>Enabled last</button>
  </div>
}

it('skips disabled fieldset controls and negative indices but keeps the first legend exception', async () => {
  const user = userEvent.setup()
  render(<NativeDisabledDialog />)
  const legend = screen.getByRole('button', { name: 'Legend control' })
  expect(document.activeElement).toBe(legend)
  await user.tab()
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Enabled last' }))
  await user.tab()
  expect(document.activeElement).toBe(legend)
})

it('does not restore focus to a trigger whose parent became hidden', () => {
  const container = document.createElement('div')
  const trigger = document.createElement('button')
  container.appendChild(trigger)
  document.body.appendChild(container)
  try {
    trigger.focus()
    const { unmount } = render(<TestDialog onClose={vi.fn()} />)
    const spy = vi.spyOn(trigger, 'focus')
    container.hidden = true
    unmount()
    expect(spy).not.toHaveBeenCalled()
  } finally { container.remove() }
})
