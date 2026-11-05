import { memo } from 'react'
import { useI18n } from '../i18n'
import { reviewTimestamp, reviewTimeOptions } from '../lib/reviewTime'
import ServerTime from './ServerTime'

// Presentation of the server UTC timestamp only. A browser's timezone support
// must not change or decide the Core's configured IANA timezone.
export default memo(function ZonedServerTime({ value, timeZone }: { value: unknown; timeZone: string }) {
  const { t, formatDateTime } = useI18n()
  const timestamp = reviewTimestamp(value)
  if (!timestamp) return <span>{t('results.unrecorded')}</span>
  let display: string
  try { display = formatDateTime(timestamp, { ...reviewTimeOptions, timeZone }) }
  catch {
    return <span style={{ overflowWrap: 'anywhere' }}><ServerTime value={timestamp}/> · {t('roomPlans.timing.zoneFallback', { zone: timeZone })}</span>
  }
  return <span style={{ overflowWrap: 'anywhere' }}><time dateTime={timestamp} title={`${display} · ${timeZone}`}>{display}</time> · {timeZone}</span>
})
