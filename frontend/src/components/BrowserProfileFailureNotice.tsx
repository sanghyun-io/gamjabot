import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import ServerTime from './ServerTime'

export type ProfileOperationTarget = Readonly<{ action: 'save' | 'takeover' | 'return' | 'close' | 'reset' | 'delete'; name: string }>
const reasons = {
  browser_profile_invalid_name: 'name', browser_profile_name_exists: 'name',
  browser_profile_invalid_origin: 'origin', browser_profile_invalid_agent: 'agent', browser_profile_agent_already_bound: 'agent',
  browser_profile_changed: 'changed', browser_profile_not_found: 'changed',
  browser_profile_busy: 'busy', browser_profile_close_unconfirmed: 'busy',
  browser_profile_disk_space: 'disk', browser_profile_profile_size: 'size',
  browser_profile_unsafe_storage: 'storage', browser_profile_unavailable: 'storage',
  browser_profile_limit: 'limit', browser_profile_authority_unavailable: 'authority', browser_profile_operation_failed: 'unknown',
} as const
export type ProfileFailure = { reason: typeof reasons[keyof typeof reasons] | 'unknown'; code?: keyof typeof reasons; status?: number; target: ProfileOperationTarget; at: string }
export function profileFailure(failure: unknown, target: ProfileOperationTarget): ProfileFailure {
  const response = (failure as { response?: { status?: unknown; data?: { detail?: { code?: unknown } } } } | null)?.response
  const status = typeof response?.status === 'number' && Number.isInteger(response.status) && response.status >= 100 && response.status <= 599 ? response.status : undefined
  const rawCode = response?.data?.detail?.code
  // Never display arbitrary server detail/body/error strings, even in technical details.
  const code = typeof rawCode === 'string' && Object.prototype.hasOwnProperty.call(reasons, rawCode)
    && (status !== undefined && status >= 400 && status < 500 || status === 503 && rawCode === 'browser_profile_unavailable') ? rawCode as keyof typeof reasons : undefined
  const reason = code ? reasons[code] : status === 401 || status === 403 ? 'authority' : 'unknown'
  return { reason, code, status, target: Object.freeze({ ...target }), at: new Date().toISOString() }
}
const actions: Record<ProfileOperationTarget['action'], MessageKey> = {
  save: 'common.save', takeover: 'browserProfiles.takeover', return: 'browserProfiles.return', close: 'common.close', reset: 'browserProfiles.clear', delete: 'common.delete',
}
export default function BrowserProfileFailureNotice({ failure }: { failure: ProfileFailure }) {
  const { t } = useI18n()
  return <section role="alert" className="notice notice--warning stack stack--tight">
    <strong>{t('browserProfiles.failure.title')}</strong>
    <p style={{ overflowWrap: 'anywhere' }}>{t(actions[failure.target.action])} · {failure.target.name}</p>
    <p>{t(`browserProfiles.failure.${failure.reason}`)}</p>
    <p className="field-hint">{t('browserProfiles.failure.next')}</p>
    <p className="metadata"><ServerTime value={failure.at} compact/></p>
    {(failure.code || failure.status) && <details className="compact-details"><summary>{t('common.technicalDetails')}</summary>
      {failure.code && <p><code>{failure.code}</code></p>}{failure.status && <p>HTTP {failure.status}</p>}
    </details>}
  </section>
}
