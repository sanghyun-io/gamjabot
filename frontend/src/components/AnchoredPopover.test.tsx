// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useRef, useState } from 'react'
import AnchoredPopover from './AnchoredPopover'

function Fixture() {
  const trigger = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  return <div style={{ overflow: 'hidden' }}><button ref={trigger} onClick={() => setOpen((value) => !value)}>Actions</button>
    <button>Outside</button>
    {open && <AnchoredPopover anchor={trigger.current} menu className="test-popover" label="Message actions" onClose={(restore) => {
      setOpen(false); if (restore) trigger.current?.focus()
    }}><button role="menuitem">Copy</button><button role="menuitem">Quote</button><button role="menuitem">Link</button></AnchoredPopover>}</div>
}
beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    return this.classList.contains('anchored-popover')
      ? { left: 0, top: 0, right: 200, bottom: 120, width: 200, height: 120, x: 0, y: 0, toJSON() {} }
      : { left: 700, top: 550, right: 730, bottom: 580, width: 30, height: 30, x: 700, y: 550, toJSON() {} }
  })
  vi.stubGlobal('innerWidth', 820)
  vi.stubGlobal('innerHeight', 600)
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('portals out of a clipping parent, measures actual size, and supports keyboard focus and Escape', () => {
  const { container } = render(<Fixture />)
  fireEvent.click(screen.getByText('Actions'))
  const menu = screen.getByRole('menu')
  expect(container.contains(menu)).toBe(false)
  expect(menu.style.top).toBe('424px')
  expect(menu.style.left).toBe('530px')
  expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: 'Copy' }))
  fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' })
  expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: 'Quote' }))
  fireEvent.keyDown(document.activeElement!, { key: 'End' })
  expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: 'Link' }))
  fireEvent.keyDown(document.activeElement!, { key: 'Home' })
  expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: 'Copy' }))
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('menu')).toBeNull()
  expect(document.activeElement).toBe(screen.getByText('Actions'))
})
it('dismisses when the ancestor scrolls or the viewport resizes, but permits scrolling within a long menu', () => {
  const { container } = render(<Fixture />)
  fireEvent.click(screen.getByText('Actions'))
  fireEvent.scroll(screen.getByRole('menu'))
  expect(screen.getByRole('menu')).toBeTruthy()
  fireEvent.scroll(container)
  expect(screen.queryByRole('menu')).toBeNull()
  fireEvent.click(screen.getByText('Actions'))
  fireEvent.resize(window)
  expect(screen.queryByRole('menu')).toBeNull()
})
it('dismisses on outside pointer interaction without stealing focus and releases document handlers when unmounted', () => {
  const view = render(<Fixture />)
  fireEvent.click(screen.getByText('Actions'))
  const outside = screen.getByText('Outside')
  act(() => outside.focus())
  expect(screen.queryByRole('menu')).toBeNull()
  expect(document.activeElement).toBe(outside)
  fireEvent.click(screen.getByText('Actions'))
  fireEvent.pointerDown(outside)
  expect(screen.queryByRole('menu')).toBeNull()
  fireEvent.click(screen.getByText('Actions'))
  view.unmount()
  expect(fireEvent.keyDown(document, { key: 'Escape' })).toBe(true)
})
it('does not consume the Escape of a modal that made the old popup inert', () => {
  render(<Fixture />)
  fireEvent.click(screen.getByText('Actions'))
  const menu = screen.getByRole('menu')
  menu.setAttribute('inert', '')
  expect(fireEvent.keyDown(document, { key: 'Escape' })).toBe(true)
})
