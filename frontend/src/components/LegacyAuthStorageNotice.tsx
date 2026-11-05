import { useSyncExternalStore } from 'react'
import { useI18n } from '../i18n'
import { clearLegacyAuthStorage, dismissLegacyAuthCleanup, legacyAuthCleanupSnapshot, subscribeLegacyAuthCleanup } from '../lib/legacyAuthStorage'

export default function LegacyAuthStorageNotice() {
  const { t } = useI18n()
  const state = useSyncExternalStore(subscribeLegacyAuthCleanup, legacyAuthCleanupSnapshot)
  if (state.status !== 'unavailable' || state.dismissed) return null
  return <details className="compact-details" style={{ minWidth: 0 }}>
    <summary className="status-text status-text--error">{t('auth.legacyCleanup.title')}</summary>
    <div className="notice notice--warning stack stack--tight" style={{ maxWidth: '32rem', maxHeight: '50dvh', overflow: 'auto', padding: '.75rem' }}>
      <p>{t('auth.legacyCleanup.body')}</p>
      <p className="field-hint">{t('auth.legacyCleanup.scope')}</p>
      {state.attempts > 1 && <p role="status">{t('auth.legacyCleanup.retryFailed')}</p>}
      <div className="page-actions">
        <button type="button" className="button button--secondary button--compact" onClick={() => { clearLegacyAuthStorage() }}>{t('common.retry')}</button>
        <button type="button" className="text-link" onClick={dismissLegacyAuthCleanup}>{t('auth.legacyCleanup.dismiss')}</button>
      </div>
    </div>
  </details>
}
