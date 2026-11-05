// @vitest-environment jsdom
import { useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import RoomManagementDialog from './RoomManagementDialog'

afterEach(() => { cleanup(); document.body.style.overflow = ''; vi.restoreAllMocks() })

function Example({ onStop }: { onStop?: () => void }) {
  const [open, setOpen] = useState(false)
  return <><textarea aria-label="Draft" defaultValue="Preserved draft" />
    <button onClick={() => setOpen(true)}>Members</button>
    {open && <RoomManagementDialog id="test-room" onClose={() => setOpen(false)} onStop={onStop}>
      <select aria-label="Member"><option>Ada</option></select>
      <details><summary>Advanced</summary><input aria-label="Hidden setting" /></details>
      <button disabled>Unavailable</button><button>Last action</button>
    </RoomManagementDialog>}
  </>
}

function open() { const trigger = screen.getByRole('button', { name: 'Members' }); trigger.focus(); fireEvent.click(trigger); return trigger }

it('blocks background access, leaves the draft mounted, and restores exact prior attributes and focus', () => {
  document.body.style.overflow = 'auto'
  const prior = document.createElement('div'); prior.setAttribute('inert', ''); prior.setAttribute('aria-hidden', 'false'); document.body.append(prior)
  const view = render(<Example />)
  const draft = screen.getByRole('textbox', { name: 'Draft' }) as HTMLTextAreaElement
  draft.setSelectionRange(4, 7)
  const trigger = open()
  const dialog = screen.getByRole('dialog', { name: 'Conversation options' })
  expect(document.body.style.overflow).toBe('hidden')
  expect(view.container.hasAttribute('inert')).toBe(true)
  expect(view.container.getAttribute('aria-hidden')).toBe('true')
  expect(screen.queryByRole('textbox', { name: 'Draft' })).toBeNull()
  expect(draft.isConnected).toBe(true)
  expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Close' }))
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(view.container.hasAttribute('inert')).toBe(false)
  expect(view.container.hasAttribute('aria-hidden')).toBe(false)
  expect(prior.hasAttribute('inert')).toBe(true)
  expect(prior.getAttribute('aria-hidden')).toBe('false')
  expect(document.body.style.overflow).toBe('auto')
  expect(document.activeElement).toBe(trigger)
  expect(draft.value).toBe('Preserved draft')
  expect([draft.selectionStart, draft.selectionEnd]).toEqual([4, 7])
  prior.remove()
})

it('traps Tab through visible select/summary controls, excluding disabled and closed-detail inputs', () => {
  render(<Example />); open()
  const dialog = screen.getByRole('dialog')
  const close = within(dialog).getByRole('button', { name: 'Close' })
  fireEvent.keyDown(close, { key: 'Tab' })
  expect(document.activeElement).toBe(within(dialog).getByRole('combobox', { name: 'Member' }))
  fireEvent.keyDown(document.activeElement!, { key: 'Tab' })
  expect(document.activeElement).toBe(within(dialog).getByText('Advanced'))
  fireEvent.keyDown(document.activeElement!, { key: 'Tab' })
  expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Last action' }))
  fireEvent.keyDown(document.activeElement!, { key: 'Tab' })
  expect(document.activeElement).toBe(close)
  fireEvent.keyDown(close, { key: 'Tab', shiftKey: true })
  expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Last action' }))
})

it('keeps Stop work available inside the modal without invoking it on open', () => {
  const stop = vi.fn(); render(<Example onStop={stop} />); open()
  expect(stop).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Stop work' }))
  expect(stop).toHaveBeenCalledTimes(1)
})

it('restores the background when the room unmounts while its modal is open', () => {
  const view = render(<Example />); open(); expect(view.container.hasAttribute('inert')).toBe(true)
  view.unmount(); expect(view.container.hasAttribute('inert')).toBe(false)
  expect(view.container.hasAttribute('aria-hidden')).toBe(false)
  expect(document.body.style.overflow).toBe('')
  expect(screen.queryByRole('dialog')).toBeNull()
})
