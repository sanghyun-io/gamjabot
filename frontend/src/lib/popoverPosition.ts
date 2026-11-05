export interface PopoverRect { left: number; right: number; top: number; bottom: number }
export interface PopoverViewport { left: number; top: number; width: number; height: number }

export function constrainPopoverViewport(viewport: PopoverViewport, boundary: PopoverRect): PopoverViewport {
  const left = Math.max(viewport.left, boundary.left)
  const top = Math.max(viewport.top, boundary.top)
  return { left, top, width: Math.max(0, Math.min(viewport.left + viewport.width, boundary.right) - left),
    height: Math.max(0, Math.min(viewport.top + viewport.height, boundary.bottom) - top) }
}

export function popoverPosition(anchor: PopoverRect, size: { width: number; height: number }, viewport: PopoverViewport,
  preferred: 'above' | 'below' = 'below', align: 'start' | 'end' = 'end', heightLimit = 280) {
  const gutter = 8
  const gap = 6
  const leftEdge = viewport.left + gutter
  const topEdge = viewport.top + gutter
  const rightEdge = viewport.left + viewport.width - gutter
  const bottomEdge = viewport.top + viewport.height - gutter
  const width = Math.max(0, Math.min(size.width, rightEdge - leftEdge))
  const above = Math.max(0, anchor.top - gap - topEdge)
  const below = Math.max(0, bottomEdge - anchor.bottom - gap)
  const desiredHeight = Math.min(size.height, heightLimit)
  const preferredSpace = preferred === 'above' ? above : below
  const otherSpace = preferred === 'above' ? below : above
  const placement = preferredSpace < desiredHeight && otherSpace > preferredSpace
    ? preferred === 'above' ? 'below' : 'above' : preferred
  // If neither side fits, shift inside the whole boundary before introducing
  // scrolling. A short menu can still fit in a short room around its anchor.
  const sideFits = Math.max(above, below) >= desiredHeight
  const maxHeight = Math.max(0, Math.min(heightLimit, sideFits ? placement === 'above' ? above : below : bottomEdge - topEdge))
  const height = Math.min(size.height, maxHeight)
  const desiredTop = placement === 'above' ? anchor.top - gap - height : anchor.bottom + gap
  const desiredLeft = align === 'start' ? anchor.left : anchor.right - width
  return {
    top: Math.max(topEdge, Math.min(desiredTop, bottomEdge - height)),
    left: Math.max(leftEdge, Math.min(desiredLeft, rightEdge - width)),
    width, maxHeight, placement,
  }
}
