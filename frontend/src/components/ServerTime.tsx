import { memo } from 'react'
import { useI18n } from '../i18n'
import { reviewTimestamp, reviewTimeOptions } from '../lib/reviewTime'

// Only for the backend UTC timestamp contract, including legacy SQLite values
// without an explicit zone. Never normalize user-entered local date fields here.
export default memo(function ServerTime({ value, compact = false, className, fallback }: {
  value: unknown; compact?: boolean; className?: string; fallback?: string
}) {
  const { t, formatDateTime } = useI18n()
  const timestamp = reviewTimestamp(value)
  if (!timestamp) return <span className={className}>{fallback ?? t('results.unrecorded')}</span>
  const full = formatDateTime(timestamp, reviewTimeOptions)
  return <time className={className} dateTime={timestamp} title={t('common.serverTimeLocal', { time: full })}>{compact ? formatDateTime(timestamp, { hour: '2-digit', minute: '2-digit' }) : full}</time>
})
