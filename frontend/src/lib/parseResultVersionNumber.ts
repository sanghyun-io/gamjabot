// Only ASCII/full-width decimal digits and an optional v/V prefix are accepted.
// The caller retains the selected/latest-version and positive safe-integer bounds.
export function parseResultVersionNumber(value: string): number {
  if (value.length > 24) return NaN
  const digits = value.trim().replace(/^[vVｖＶ]/, '')
    .replace(/[０-９]/g, digit => String.fromCharCode(digit.charCodeAt(0) - 0xfee0))
  return /^\d+$/.test(digits) ? Number(digits) : NaN
}
