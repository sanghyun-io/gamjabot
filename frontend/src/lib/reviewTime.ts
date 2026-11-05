export const reviewTimeOptions: Intl.DateTimeFormatOptions = { dateStyle: 'medium', timeStyle: 'medium' }

// Tool activity stores UTC datetimes without a zone; retained review snapshots
// already carry one. Normalize only this server time contract before local display.
export function reviewTimestamp(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 40) return null
  const parts = /^(\d{4}-\d\d-\d\d)T([01]\d|2[0-3]):([0-5]\d):([0-5]\d)(?:\.\d{1,6})?(Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?$/.exec(value)
  if (!parts) return null
  const date = new Date(`${parts[1]}T00:00:00Z`)
  if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== parts[1]) return null
  const normalized = parts[5] ? value : `${value}Z`
  return Number.isFinite(Date.parse(normalized)) ? normalized : null
}
