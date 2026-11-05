import type { Element, Root, RootContent } from 'hast'

export function sectionFragment(value: string): string | null {
  if (!value.startsWith('#') || value.length > 512) return null
  try {
    const decoded = decodeURIComponent(value.slice(1)).normalize('NFC').toLowerCase()
    return /^[\p{L}\p{N}\p{M}_-]+$/u.test(decoded) ? decoded : null
  } catch { return null }
}

function textOf(node: RootContent): string {
  if (node.type === 'text') return node.value
  if (node.type === 'element') return node.children.map(textOf).join('')
  return ''
}

// Render-only heading tokens. No document-global IDs, raw HTML anchors or
// mutation of the canonical Markdown, message exports, routes or URL hash.
export function messageSections() {
  return (tree: Root) => {
    const elements: Element[] = []
    const walk = (node: Root | RootContent) => {
      if (node.type === 'element') elements.push(node)
      if ('children' in node) node.children.forEach(walk)
    }
    walk(tree)
    // Ordinals identify tables within this one rendered message, not globally.
    let tableNumber = 0
    for (const element of elements) {
      if (element.tagName === 'table') element.properties['data-message-table'] = ++tableNumber
    }
    const used = new Set<string>()
    for (const element of elements) {
      if (!/^h[1-6]$/.test(element.tagName)) continue
      const base = element.children.map(textOf).join('').trim().normalize('NFC').toLowerCase()
        .replace(/[^\p{L}\p{N}\p{M}_\s-]/gu, '').replace(/\s/g, '-') || 'section'
      let slug = base, suffix = 1
      while (used.has(slug)) slug = `${base}-${suffix++}`
      used.add(slug)
      element.properties['data-message-heading'] = slug
      element.properties.tabIndex = -1
    }
    for (const element of elements) {
      if (element.tagName !== 'a' || typeof element.properties.href !== 'string') continue
      const slug = sectionFragment(element.properties.href)
      if (slug && used.has(slug)) element.properties['data-message-target'] = slug
    }
  }
}
