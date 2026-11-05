// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import MessageContent from './MessageContent'
import { assistantVisibleMarkdown } from '../lib/assistantContent'

const clipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
afterEach(() => {
  cleanup()
  if (clipboard) Object.defineProperty(navigator, 'clipboard', clipboard)
  else Reflect.deleteProperty(navigator, 'clipboard')
})
const show = (content: string) => render(<MemoryRouter><MessageContent content={content} /></MemoryRouter>)

it('keeps the paragraph node stable across parent refreshes so text selection and message anchors survive', () => {
  const view = show('First message')
  const paragraph = screen.getByText('First message')
  view.rerender(<MemoryRouter><MessageContent content="First message" /></MemoryRouter>)
  expect(screen.getByText('First message')).toBe(paragraph)
})

it('renders readable headings, emphasis, lists and tables rather than Markdown delimiters', () => {
  const view = show('## Result\n\n**Complete** and `value`.\n\n- First\n- Second\n\n| Input | Result |\n| --- | --- |\n| empty | allowed |')
  expect(screen.getByRole('heading', { name: 'Result' })).toBeTruthy()
  expect(view.container.querySelector('strong')?.textContent).toBe('Complete')
  expect(screen.getAllByRole('listitem')).toHaveLength(2)
  expect(screen.getByRole('table').textContent).toContain('allowed')
  expect(view.container.textContent).not.toContain('**Complete**')
})

it('copies only the selected code block on explicit click, preserving indentation', async () => {
  const write = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: write } })
  const source = 'function title(v) {\n  return Array.from(v).slice(0, 12).join(\'\');\n}'
  const view = show('Explanation\n\n```javascript\n' + source + '\n```\n\nOther `inline` text.')
  expect(view.container.querySelector('pre code')?.textContent).toBe(source + '\n')
  expect(write).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Copy code' }))
  await screen.findByText('Copied')
  expect(write).toHaveBeenCalledOnce()
  expect(write).toHaveBeenCalledWith(source)
  expect(screen.getAllByRole('button')).toHaveLength(1)
})

it('keeps code block copy targets separate', async () => {
  const write = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: write } })
  const view = show('```js\none()\n```\n\n```python\ntwo()\n```')
  const figures = view.container.querySelectorAll('figure')
  fireEvent.click(within(figures[1] as HTMLElement).getByRole('button', { name: 'Copy code' }))
  await within(figures[1] as HTMLElement).findByText('Copied')
  expect(write).toHaveBeenCalledWith('two()')
})

it('reports a clipboard rejection without a false success and allows an explicit retry', async () => {
  const write = vi.fn().mockRejectedValueOnce(new Error('denied')).mockResolvedValueOnce(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: write } })
  show('```\nvalue\n```')
  fireEvent.click(screen.getByRole('button', { name: 'Copy code' }))
  expect((await screen.findByRole('alert')).textContent).toContain('Could not copy')
  expect(screen.queryByText('Copied')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Copy code' }))
  await screen.findByText('Copied')
  expect(screen.queryByRole('alert')).toBeNull()
  expect(write).toHaveBeenCalledTimes(2)
})

it.each(['javascript:alert(1)', 'data:text/html,unsafe', 'vbscript:msgbox(1)', 'file:///etc/passwd', '//example.test/track'])('keeps unsafe or ambiguous link %s inert', (url) => {
  const view = show(`[Keep the label](${url})`)
  expect(view.container.querySelector('a')).toBeNull()
  expect(view.container.textContent).toContain('Keep the label')
})

it('keeps raw HTML inert and avoids automatic remote image requests', () => {
  const view = show('<script>alert(1)</script>\n\n<iframe src="https://example.test"></iframe>\n\n![Private image](https://example.test/track.png)')
  expect(view.container.querySelector('script, iframe, img')).toBeNull()
  expect(view.container.textContent).toContain('Private image')
})

it('preserves safe external links and internal room navigation', () => {
  show('[Docs](https://example.test/docs) and [Original](/chat/27?seq=1)')
  const docs = screen.getByRole('link', { name: 'Docs' })
  expect(docs.getAttribute('href')).toBe('https://example.test/docs')
  expect(docs.getAttribute('rel')).toBe('noopener noreferrer')
  expect(screen.getByRole('link', { name: 'Original' }).getAttribute('href')).toBe('/chat/27?seq=1')
})

it.each([
  '<think>Private reasoning</think>\n\nFinal answer',
  '<think>\n\nPrivate reasoning\n\n</think>\n\nFinal answer',
  '</think>\nFinal answer',
  '  </THINK>\n\nFinal answer',
])('shows the final assistant answer without reasoning wrappers: %s', (source) => {
  const view = render(<MemoryRouter><MessageContent content={source} assistant /></MemoryRouter>)
  expect(view.container.textContent).toContain('Final answer')
  expect(view.container.textContent).not.toContain('Private reasoning')
  expect(assistantVisibleMarkdown(source)).not.toMatch(/<\/?think>/i)
})

it('hides unfinished assistant reasoning instead of exposing its remainder', () => {
  const source = '<think>\n\nPrivate reasoning without a closing delimiter'
  const view = render(<MemoryRouter><MessageContent content={source} assistant /></MemoryRouter>)
  expect(view.container.textContent).not.toContain('Private reasoning')
  expect(assistantVisibleMarkdown(source)).toBe('')
  expect(source).toContain('Private reasoning')
})

it('keeps user-authored think examples separate from assistant presentation', () => {
  const source = '<think>\n\nUser-authored example\n\n</think>'
  const view = show(source)
  expect(view.container.textContent).toContain('User-authored example')
})

it.each([
  '```xml\n<think>\nprivate-looking literal\n</think>\n```',
  '~~~~xml\n<think>literal</think>\n~~~~',
  '````xml\n```\n<think>literal</think>\n```\n````',
  '`<think>literal</think>`',
  '``line one\n<think>literal</think>\nline three``',
  '    <think>indented code</think>',
  '\t<think>tab-indented code</think>',
  'The literal tag <think> appears in prose.',
])('preserves code or inline literal content byte for byte: %s', (source) => {
  expect(assistantVisibleMarkdown(source)).toBe(source)
})

it('keeps literal tags in a visible code block copy after hidden reasoning', async () => {
  const write = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: write } })
  const code = '<think>literal code</think>'
  render(<MemoryRouter><MessageContent assistant content={'<think>\n\nPrivate reasoning\n\n</think>\n\n```xml\n' + code + '\n```'} /></MemoryRouter>)
  expect(screen.queryByText('Private reasoning')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Copy code' }))
  await screen.findByText('Copied')
  expect(write).toHaveBeenCalledWith(code)
})
