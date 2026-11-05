import { describe, expect, it } from 'vitest'
import { readPastedChatReference, readPastedResultLink } from './resultLink'

const id = '929d363e-c8b1-466c-a4fc-f7e24a3adb8c'
describe('pasted chat reference', () => {
  it('accepts exact event references in either order or local markdown', () => {
    const expected = { state: 'valid', kind: 'event', room: 21, seq: 13, event: id, href: `/chat/21?seq=13&event=${id}` }
    expect(readPastedChatReference(`/chat/21?event=${id}&seq=13`)).toEqual(expected)
    expect(readPastedChatReference(`[출처](/chat/21?seq=13&event=${id})`)).toEqual(expected)
  })
  it('rejects malformed, mixed, duplicate and nonlocal references', () => {
    const paths = [`/chat/021?seq=13&event=${id}`, `/chat/21?seq=013&event=${id}`, `/chat/21?seq=13&seq=13&event=${id}`, `/chat/21?seq=13&event=${id}&result=${id}`, `/chat/21?seq=13&event=${id}#x`, `/chat/21?seq=13&event=bad`, '/chat/21?seq=13', `https://example.invalid/chat/21?seq=13&event=${id}`, `/chat/9007199254740992?seq=13&event=${id}`]
    for (const path of paths) expect(readPastedChatReference(path).state, path).toBe('invalid')
  })
  it('preserves existing result/version/base parsing', () => {
    const path = `/chat/21?base=1&version=2&result=${id}`
    expect(readPastedChatReference(path)).toEqual({ ...readPastedResultLink(path), kind: 'result' })
    expect(readPastedChatReference(`/chat/21?result=${id}&version=2&base=2`).state).toBe('invalid')
  })
})
