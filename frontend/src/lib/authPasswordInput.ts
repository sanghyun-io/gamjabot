// Mirror the current Core password input limit without trimming, normalization,
// truncation, encoded credential copies or added strength/character rules.
export function passwordInputInfo(value: string) {
  let bytes = 0
  for (const character of value) {
    const point = character.codePointAt(0)!
    if (point === 0 || point >= 0xd800 && point <= 0xdfff) return { bytes: null, malformed: true, valid: false }
    bytes += point <= 0x7f ? 1 : point <= 0x7ff ? 2 : point <= 0xffff ? 3 : 4
  }
  return { bytes, malformed: false, valid: bytes >= 1 && bytes <= 72 }
}
