import { describe, expect, it } from 'vitest'
import { constrainPopoverViewport, popoverPosition } from './popoverPosition'

describe('popover viewport boundaries', () => {
  const viewport = { left: 0, top: 0, width: 820, height: 600 }
  it.each([
    { left: 0, right: 30, top: 8, bottom: 38 },
    { left: 790, right: 820, top: 8, bottom: 38 },
    { left: 0, right: 30, top: 562, bottom: 592 },
    { left: 790, right: 820, top: 562, bottom: 592 },
  ])('keeps the complete menu reachable at each corner: %o', (anchor) => {
    const result = popoverPosition(anchor, { width: 220, height: 160 }, viewport)
    expect(result.left).toBeGreaterThanOrEqual(8)
    expect(result.left + result.width).toBeLessThanOrEqual(812)
    expect(result.top).toBeGreaterThanOrEqual(8)
    expect(result.top + Math.min(160, result.maxHeight)).toBeLessThanOrEqual(592)
    expect(result.maxHeight).toBeGreaterThanOrEqual(160)
    expect(result.placement).toBe(anchor.top > 500 ? 'above' : 'below')
  })
  it('bounds a long list inside a short viewport and leaves it scrollable', () => {
    const result = popoverPosition({ left: 50, right: 80, top: 60, bottom: 90 },
      { width: 360, height: 420 }, { left: 0, top: 0, width: 240, height: 140 })
    expect(result.width).toBe(224)
    expect(result.left).toBe(8)
    expect(result.placement).toBe('above')
    expect(result.maxHeight).toBe(124)
    expect(result.top).toBe(8)
  })
  it('uses the visible viewport offset and flips a mention list when needed', () => {
    const result = popoverPosition({ left: 10, right: 80, top: 105, bottom: 135 },
      { width: 200, height: 80 }, { left: 20, top: 100, width: 300, height: 200 }, 'above', 'start', 220)
    expect(result.placement).toBe('below')
    expect(result.left).toBe(28)
    expect(result.top).toBe(141)
    expect(result.top + 80).toBeLessThanOrEqual(292)
  })
  it('flips before a scroll boundary even if the viewport has space over the composer', () => {
    const bounds = constrainPopoverViewport(viewport, { left: 40, right: 480, top: 200, bottom: 420 })
    const result = popoverPosition({ left: 440, right: 470, top: 370, bottom: 400 }, { width: 200, height: 120 }, bounds)
    expect(result.placement).toBe('above')
    expect(result.top).toBe(244)
    expect(result.left).toBe(270)
    expect(result.top + 120).toBeLessThan(420)
  })
  it('shifts a short menu into the room when neither side fits but the whole rectangle does', () => {
    const bounds = constrainPopoverViewport(viewport, { left: 0, right: 560, top: 236, bottom: 414 })
    const result = popoverPosition({ left: 514, right: 544, top: 269, bottom: 299 }, { width: 176, height: 136 }, bounds)
    expect(result.maxHeight).toBeGreaterThanOrEqual(136)
    expect(result.top).toBe(270)
    expect(result.top + 136).toBe(406)
    expect(result.left).toBe(368)
  })
})
