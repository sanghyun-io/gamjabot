// Operator-supplied GUI delivery metadata, independent of runtime Core identity.
const label: unknown = import.meta.env.VITE_GAMJA_GUI_BUILD
const source: unknown = import.meta.env.VITE_GAMJA_GUI_SOURCE
export const guiBuildInfo = typeof label === 'string' && /^[A-Z][A-Z0-9-]{1,23}$/.test(label)
  && typeof source === 'string' && /^[a-f0-9]{40}$/.test(source)
  ? Object.freeze({ label, source }) : null
export function guiBuildCopyText(version: string) {
  return guiBuildInfo ? `Gamja Bot ${version}\nGUI build: ${guiBuildInfo.label}\nGUI source checkpoint: ${guiBuildInfo.source}` : ''
}
