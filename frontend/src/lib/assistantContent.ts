/** Presentation only. Never changes the canonical event or an exported transcript. */
export function assistantVisibleMarkdown(source: string): string {
  // Protect fenced and inline code before interpreting a line-leading reasoning marker.
  const protectedRanges: [number, number][] = []
  const lines = source.match(/[^\n]*\n|[^\n]+$/g) ?? []
  let offset = 0
  let fence: { character: string; length: number; start: number } | null = null
  for (const line of lines) {
    if (fence) {
      const closing = /^ {0,3}(`+|~+)[ \t]*(?:\r?\n)?$/.exec(line)
      if (closing && closing[1][0] === fence.character && closing[1].length >= fence.length) {
        protectedRanges.push([fence.start, offset + line.length])
        fence = null
      }
    } else {
      const opening = /^ {0,3}(`{3,}|~{3,})([^\n]*)/.exec(line)
      if (opening && !(opening[1][0] === '`' && opening[2].includes('`'))) {
        fence = { character: opening[1][0], length: opening[1].length, start: offset }
      }
    }
    offset += line.length
  }
  if (fence) protectedRanges.push([fence.start, source.length])
  const protectedAt = (index: number) => protectedRanges.some(([start, end]) => index >= start && index < end)
  const ticks = /`+/g
  let tick: RegExpExecArray | null
  while ((tick = ticks.exec(source))) {
    if (protectedAt(tick.index)) continue
    const endTicks = /`+/g
    endTicks.lastIndex = ticks.lastIndex
    let closing: RegExpExecArray | null
    while ((closing = endTicks.exec(source))) {
      if (protectedAt(closing.index)) break
      if (closing[0].length === tick[0].length) {
        protectedRanges.push([tick.index, endTicks.lastIndex])
        ticks.lastIndex = endTicks.lastIndex
        break
      }
    }
  }

  const markers = /^ {0,3}<(\/?)think>[ \t]*/gim
  const removals: [number, number][] = []
  let marker: RegExpExecArray | null
  while ((marker = markers.exec(source))) {
    if (protectedAt(marker.index)) continue
    let end = markers.lastIndex
    if (!marker[1]) {
      const closings = /<\/think>/gi
      closings.lastIndex = end
      let closing: RegExpExecArray | null
      end = source.length // An unfinished reasoning block must not reveal its remainder.
      while ((closing = closings.exec(source))) {
        if (!protectedAt(closing.index)) { end = closings.lastIndex; break }
      }
    }
    removals.push([marker.index, end])
    markers.lastIndex = end
  }
  if (!removals.length) return source
  let visible = ''
  let previous = 0
  for (const [start, end] of removals) {
    visible += source.slice(previous, start)
    previous = end
  }
  return visible + source.slice(previous)
}
