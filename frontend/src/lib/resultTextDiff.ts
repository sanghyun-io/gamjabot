export type ResultDiffLine = Readonly<{ kind: 'same' | 'added' | 'removed'; text: string; beforeLine: number | null; afterLine: number | null }>
export type ResultTextDiff = Readonly<{ lines: readonly ResultDiffLine[]; added: number; removed: number }>
// Exact logical lines, including their original CR/LF and final-newline state.
// Bounded local display work only; originals and clipboard payload are untouched.
export function resultTextDiff(before: string, after: string): ResultTextDiff | null {
  if (before === after) return { lines: [], added: 0, removed: 0 }
  if (before.length + after.length > 200_000) return null
  const left = before.match(/[^\n]*\n|[^\n]+$/g) || [], right = after.match(/[^\n]*\n|[^\n]+$/g) || []
  if (left.length + right.length > 2_000) return null
  let prefix = 0, suffix = 0
  while (prefix < left.length && prefix < right.length && left[prefix] === right[prefix]) prefix++
  while (suffix < left.length - prefix && suffix < right.length - prefix && left[left.length - suffix - 1] === right[right.length - suffix - 1]) suffix++
  const a = left.slice(prefix, left.length - suffix), b = right.slice(prefix, right.length - suffix), width = b.length + 1
  if ((a.length + 1) * width > 500_000) return null
  const matrix = new Uint32Array((a.length + 1) * width)
  for (let i = a.length - 1; i >= 0; i--) for (let j = b.length - 1; j >= 0; j--) {
    matrix[i * width + j] = a[i] === b[j] ? matrix[(i + 1) * width + j + 1] + 1 : Math.max(matrix[(i + 1) * width + j], matrix[i * width + j + 1])
  }
  const lines: ResultDiffLine[] = []; let oldLine = 0, newLine = 0, added = 0, removed = 0
  const push = (kind: ResultDiffLine['kind'], text: string) => {
    const beforeLine = kind === 'added' ? null : ++oldLine, afterLine = kind === 'removed' ? null : ++newLine
    if (kind === 'added') added++
    if (kind === 'removed') removed++
    lines.push({ kind, text, beforeLine, afterLine })
  }
  for (let k = 0; k < prefix; k++) push('same', left[k])
  let i = 0, j = 0
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) { push('same', a[i]); i++; j++ }
    else if (i < a.length && (j === b.length || matrix[(i + 1) * width + j] >= matrix[i * width + j + 1])) push('removed', a[i++])
    else push('added', b[j++])
  }
  for (let k = suffix; k > 0; k--) push('same', left[left.length - k])
  return { lines, added, removed }
}
