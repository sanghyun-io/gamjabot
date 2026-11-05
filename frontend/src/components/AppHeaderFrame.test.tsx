// @vitest-environment jsdom
import { StrictMode, useEffect, useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import { AppHeaderFrame } from './AppHeaderFrame'
import { PageHeader } from './ui'
import QuickRoomSwitcher from './QuickRoomSwitcher'

vi.mock('../api/roomIndex', () => ({ roomIndexApi: { list: vi.fn().mockResolvedValue({ items: [], next_cursor: null }) } }))
vi.mock('../api/projects', () => ({ projectsApi: { list: vi.fn().mockResolvedValue([]) } }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

const controls = <label>Global filter<input aria-label="Global filter" defaultValue="" /></label>

it('preserves the independent page header without introducing root controls', () => {
  render(<MemoryRouter><PageHeader title="Connections" backTo="/" actions={<button>Test</button>} width="narrow" /></MemoryRouter>)
  expect(screen.getByRole('heading', { name: 'Connections' }).closest('header')).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Go back' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Test' })).toBeTruthy()
  expect(document.querySelector('.app-header-controls-slot')).toBeNull()
})

it('puts one global controls tree inside the page header and hides the separate toolbar', async () => {
  render(<MemoryRouter><AppHeaderFrame controls={controls}><PageHeader title="Connections" /></AppHeaderFrame></MemoryRouter>)
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Global filter' }).closest('header')).toBeTruthy())
  expect(screen.getAllByRole('textbox', { name: 'Global filter' })).toHaveLength(1)
  expect(document.querySelector('.locale-toolbar')).toHaveProperty('hidden', true)
  expect(document.querySelector('.app-frame--integrated-header')).toBeTruthy()
})

it('does not register fresh header content or remount controls on every title update', async () => {
  let live = 0, mounts = 0
  function Control() { useEffect(() => { live += 1; mounts += 1; return () => { live -= 1 } }, []); return <input aria-label="Persistent filter" /> }
  function View({ title }: { title: string }) { return <MemoryRouter><AppHeaderFrame controls={<Control />}><PageHeader title={title} /></AppHeaderFrame></MemoryRouter> }
  const view = render(<StrictMode><View title="First" /></StrictMode>)
  const input = screen.getByRole('textbox', { name: 'Persistent filter' })
  fireEvent.change(input, { target: { value: 'keep this filter' } })
  const initialMounts = mounts
  for (const title of ['Loading', 'Very long translated title '.repeat(8), 'Updated']) view.rerender(<StrictMode><View title={title} /></StrictMode>)
  await waitFor(() => expect(screen.getByRole('heading', { name: 'Updated' })).toBeTruthy())
  expect(screen.getByRole('textbox', { name: 'Persistent filter' })).toBe(input)
  expect(input).toHaveProperty('value', 'keep this filter')
  expect(live).toBe(1)
  expect(mounts).toBe(initialMounts)
  view.unmount()
  expect(live).toBe(0)
})

it('moves the same controls DOM and preserves its filter across distinct route headers', async () => {
  render(<MemoryRouter initialEntries={['/one']}><AppHeaderFrame controls={controls}><Routes>
    <Route path="/one" element={<><PageHeader title="One" /><Link to="/two">Next</Link></>} />
    <Route path="/two" element={<PageHeader title="Two" backTo="/one" />} />
  </Routes></AppHeaderFrame></MemoryRouter>)
  const input = screen.getByRole('textbox', { name: 'Global filter' })
  fireEvent.change(input, { target: { value: 'my room query' } })
  fireEvent.click(screen.getByRole('link', { name: 'Next' }))
  await waitFor(() => expect(screen.getByRole('heading', { name: 'Two' })).toBeTruthy())
  expect(screen.getByRole('textbox', { name: 'Global filter' })).toBe(input)
  expect(input).toHaveProperty('value', 'my room query')
  expect(input.closest('header')).toBe(screen.getByRole('heading', { name: 'Two' }).closest('header'))
  expect(document.querySelectorAll('.app-header-controls')).toHaveLength(1)
})

it('returns controls to the startup/error fallback without losing state or old header content', async () => {
  function View() {
    const [show, setShow] = useState(true)
    return <AppHeaderFrame controls={controls}>{show ? <PageHeader title="Ready" /> : <p role="alert">Service unavailable</p>}
      <button onClick={() => setShow(!show)}>Toggle</button></AppHeaderFrame>
  }
  render(<MemoryRouter><View /></MemoryRouter>)
  const input = screen.getByRole('textbox', { name: 'Global filter' })
  fireEvent.change(input, { target: { value: 'saved query' } })
  fireEvent.click(screen.getByRole('button', { name: 'Toggle' }))
  await waitFor(() => expect(input.closest('.locale-toolbar')).toBeTruthy())
  expect(document.querySelector('.locale-toolbar')).toHaveProperty('hidden', false)
  expect(screen.queryByRole('heading', { name: 'Ready' })).toBeNull()
  expect(input).toHaveProperty('value', 'saved query')
  fireEvent.click(screen.getByRole('button', { name: 'Toggle' }))
  await waitFor(() => expect(input.closest('header')).toBeTruthy())
  expect(document.querySelectorAll('.app-header-controls')).toHaveLength(1)
})

it('cleans up the owned portal mount when the root frame unmounts', () => {
  const view = render(<MemoryRouter><AppHeaderFrame controls={controls}><PageHeader title="Ready" /></AppHeaderFrame></MemoryRouter>)
  const mount = document.querySelector('.app-header-controls')!
  view.unmount()
  expect(mount.isConnected).toBe(false)
  expect(document.querySelector('.app-header-controls')).toBeNull()
  expect(screen.queryByRole('textbox', { name: 'Global filter' })).toBeNull()
})

it('keeps the global shortcut and draft focus restoration while the header owns its trigger', async () => {
  render(<MemoryRouter><AppHeaderFrame room controls={<QuickRoomSwitcher />}><PageHeader title="Room" />
    <textarea aria-label="Draft" defaultValue="keep this draft" /></AppHeaderFrame></MemoryRouter>)
  const draft = screen.getByRole('textbox', { name: 'Draft' }) as HTMLTextAreaElement
  draft.focus(); draft.setSelectionRange(3, 6)
  fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
  const dialog = await screen.findByRole('dialog', { name: 'Switch rooms' })
  expect(dialog.closest('.app-header-controls')).toBeNull()
  fireEvent.keyDown(within(dialog).getByRole('combobox', { name: 'Search room titles' }), { key: 'Escape' })
  await waitFor(() => expect(document.activeElement).toBe(draft))
  expect(draft.value).toBe('keep this draft')
  expect([draft.selectionStart, draft.selectionEnd]).toEqual([3, 6])
  expect(screen.getByRole('button', { name: 'Open room switcher' }).closest('header')).toBeTruthy()
})
