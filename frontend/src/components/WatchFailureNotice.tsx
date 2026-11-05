import type { Watch } from '../lib/roomPlanEditorState'
import { useI18n } from '../i18n'

const reasons = {
  invalid_public_url: 'roomPlans.watchFailure.address',
  private_address: 'roomPlans.watchFailure.private',
  insecure_redirect: 'roomPlans.watchFailure.insecure',
  redirect_limit: 'roomPlans.watchFailure.redirect',
  compressed_response: 'roomPlans.watchFailure.compressed',
  unsupported_content: 'roomPlans.watchFailure.content',
  page_too_large: 'roomPlans.watchFailure.large',
  timeout: 'roomPlans.watchFailure.timeout',
  request_failed: 'roomPlans.watchFailure.request',
  room_unavailable: 'roomPlans.watchFailure.room',
  http_429: 'roomPlans.watchFailure.rate',
} as const

function reasonFor(value: unknown) {
  if (typeof value === 'string') {
    if (Object.prototype.hasOwnProperty.call(reasons, value)) return { message: reasons[value as keyof typeof reasons], code: value }
    const http = /^http_([1-5]\d{2})$/.exec(value)
    if (http && http[1] !== '200') return { message: 'roomPlans.watchFailure.http' as const, code: value, status: Number(http[1]) }
  }
  // Unknown values are not rendered as arbitrary server text. The original
  // received record is untouched; no cause is guessed from its wording.
  return { message: 'roomPlans.watchFailure.unknown' as const, code: null }
}

export default function WatchFailureNotice({ watch, readOnly = false }: {
  watch: Pick<Watch, 'last_error' | 'failures' | 'state'>; readOnly?: boolean
}) {
  const { t } = useI18n()
  if (!watch.last_error) return null
  const reason = reasonFor(watch.last_error)
  const status = 'status' in reason && typeof reason.status === 'number' ? reason.status : '—'
  const state = watch.state === 'active' ? 'roomPlans.watchFailure.active'
    : watch.state === 'paused' ? 'roomPlans.watchFailure.paused'
    : watch.state === 'ended' ? 'roomPlans.watchFailure.ended' : 'roomPlans.watchFailure.stateUnknown'
  const count = Number.isSafeInteger(watch.failures) && watch.failures >= 0 ? watch.failures : '—'
  return <div role="status" className="notice notice--warning stack stack--tight">
    <p>{t(reason.message, { status })}</p>
    <p className="field-hint">{t(readOnly ? 'roomPlans.watchFailure.readOnly' : state)}</p>
    <details className="compact-details"><summary>{t('common.technicalDetails')}</summary>
      <p>{t('chat.detail.code')}: <code>{reason.code ?? t('roomPlans.watchFailure.unknownCode')}</code></p>
      <p>{t('roomPlans.watchFailure.count', { count })}</p>
    </details>
  </div>
}
