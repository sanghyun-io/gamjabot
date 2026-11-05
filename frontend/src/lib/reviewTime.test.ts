import { expect, it } from 'vitest'
import { reviewTimeOptions, reviewTimestamp } from './reviewTime'

it('interprets zone-less database UTC the same as an explicit UTC snapshot, including microseconds', () => {
  const naive = reviewTimestamp('2026-10-16T00:48:12.674149')!
  expect(naive).toBe('2026-10-16T00:48:12.674149Z')
  expect(Date.parse(naive)).toBe(Date.UTC(2026, 9, 16, 0, 48, 12, 674))
  expect(new Intl.DateTimeFormat('ko', { ...reviewTimeOptions, timeZone: 'Asia/Seoul' }).format(new Date(naive)))
    .toBe('2026. 10. 16. 오전 9:48:12')
  expect(new Intl.DateTimeFormat('en', { ...reviewTimeOptions, timeZone: 'America/Los_Angeles' }).format(new Date(naive)))
    .toBe('Oct 15, 2026, 5:48:12 PM')
})

it('retains explicit offsets rather than applying UTC a second time', () => {
  const offset = '2026-10-16T09:48:12.674149+09:00'
  expect(reviewTimestamp(offset)).toBe(offset)
  expect(Date.parse(reviewTimestamp(offset)!)).toBe(Date.parse(reviewTimestamp('2026-10-16T00:48:12.674149')!))
})

it.each([null, undefined, '', 0, false, {}, '2026-10-16', '2026-02-30T00:00:00', '2026-10-16T24:00:00Z',
  '2026-10-16T00:60:00Z', '2026-10-16T00:00:00+25:00', '2026-10-16T00:00:00garbage', 'x'.repeat(41)])
  ('does not invent a timestamp for missing or malformed audit data: %j', (value) => {
    expect(reviewTimestamp(value)).toBeNull()
  })
