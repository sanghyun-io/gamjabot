export const avatarParts = { head: 10, hair: 12, eyes: 12, nose: 8, mouth: 12, skin: 12, accent: 10 } as const
export type AvatarPart = keyof typeof avatarParts
export type AvatarConfig = { version: 1 } & Record<AvatarPart, number>
export const skinColors = ['#f5d6b5','#c98e6b','#895c4d','#543c3c','#e9c1ad','#c1d9dc','#a8c6ed','#c6bce4','#b7d3b0','#f2c3cf','#e5d9ac','#dadde2']
export const accentColors = ['#276d89','#68549b','#b95158','#be7940','#49785d','#374663','#88617f','#6a7060','#825344','#466d83']
export function validAvatar(value: unknown): value is AvatarConfig {
  if (!value || typeof value !== 'object' || (value as AvatarConfig).version !== 1) return false
  return Object.entries(avatarParts).every(([part, count]) => { const n = (value as AvatarConfig)[part as AvatarPart]; return Number.isSafeInteger(n) && n >= 0 && n < count })
}
export function randomAvatar(): AvatarConfig {
  const values = crypto.getRandomValues(new Uint32Array(7))
  return { version: 1, ...Object.fromEntries(Object.entries(avatarParts).map(([part, count], i) => [part, values[i] % count])) } as AvatarConfig
}
// Stable placeholder for an old server/missing record, never claimed as a saved recipe.
export function avatarFromSeed(seed: number): AvatarConfig {
  let x = (Number.isSafeInteger(seed) ? seed : 0) >>> 0
  const next = () => { x = (Math.imul(x, 1664525) + 1013904223) >>> 0; return x }
  return { version: 1, ...Object.fromEntries(Object.entries(avatarParts).map(([part, count]) => [part, next() % count])) } as AvatarConfig
}
