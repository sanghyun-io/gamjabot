import { useEffect, useRef, useState } from 'react'
import { setAutoReporting } from '../lib/reporting'
import { useReportingReads } from '../lib/useReportingReads'
import { useAuthStore } from '../stores/authStore'
import ReportProblem from '../components/ReportProblem'
import { AppShell, PageHeader } from '../components/ui'
import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'

export default function ReportSettings() {
  const identity = useAuthStore(state => `${state.user?.id ?? 0}:${state.epoch}`)
  return <ReportSettingsPage key={identity} identity={identity} />
}

function ReportSettingsPage({ identity }: { identity: string }) {
  const reads = useReportingReads()
  const { status, snapshot, statusPhase, snapshotPhase, statusReceivedAt, snapshotReceivedAt, readStatus, readSnapshot, invalidateReads } = reads
  const [error, setError] = useState<MessageKey | ''>('')
  const [message, setMessage] = useState<MessageKey | ''>('')
  const [busy, setBusy] = useState(false)
  const lock = useRef(false), mounted = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const { t, formatDateTime } = useI18n()

  async function changeConsent(enabled: boolean) {
    if (lock.current || statusPhase !== 'ready' || !status) return
    lock.current = true; setBusy(true); setError(''); setMessage('')
    invalidateReads()
    try {
      const [actual, generation] = await setAutoReporting(enabled)
      if (actual !== enabled || !Number.isSafeInteger(generation) || generation < 0) throw new Error('Consent response unknown')
      if (!mounted.current) return
      setMessage(actual ? 'report.enabled' : 'report.disabled')
      await readStatus(true)
    } catch {
      if (!mounted.current) return
      const current = await readStatus(true)
      if (!mounted.current) return
      setError(enabled
        ? current?.auto_reporting_enabled ? 'report.enableUnconfirmed' : current ? 'report.enableFailure' : 'report.statusUnavailable'
        : current?.auto_reporting_enabled === false ? 'report.disableUnconfirmed' : 'report.disableFailure')
    } finally {
      if (mounted.current) { void readSnapshot(true); lock.current = false; setBusy(false) }
    }
  }

  return <AppShell><PageHeader title={t('report.title')} subtitle={t('report.subtitle')} backTo="/" width="narrow" />
  <main className="page-frame page-frame--narrow page-content stack">
    <section className="surface stack"><h2 className="section-title">{t('report.automatic')}</h2>
      <p>{t('report.destination')}</p><p>{t('report.contents')}</p><p>{t('report.retention')}</p><p>{t('report.consent')}</p>
      {error && <p role="alert" className="notice notice--danger">{t(error)}</p>}
      {message && <p role="status" className="notice notice--success">{t(message)}</p>}
      <label className="check-row"><input type="checkbox" checked={statusPhase === 'ready' && !!status?.auto_reporting_enabled}
        ref={element => { if (element) element.indeterminate = statusPhase !== 'ready' }} aria-checked={statusPhase !== 'ready' ? 'mixed' : !!status?.auto_reporting_enabled}
        disabled={!status || statusPhase !== 'ready' || busy || (!status.submission_available && !status.auto_reporting_enabled)} onChange={event => void changeConsent(event.target.checked)} />{t('report.autoSend')}</label>
      {statusPhase !== 'ready' && <p role={statusPhase === 'error' ? 'alert' : 'status'}>{t(statusPhase === 'loading' ? 'report.loading' : 'report.readStatusUnknown')}</p>}
      <button type="button" className="button button--secondary button--compact" disabled={busy || statusPhase === 'loading'} onClick={() => void readStatus()}>{t('report.refreshStatus')}</button>
      {statusPhase === 'ready' && status && !status.submission_available && <p>{t('report.unavailable')}</p>}
      <details className="compact-details"><summary>{t('report.recordDetails')}</summary><div className="stack stack--tight">
        {statusReceivedAt !== null && <p className="metadata">{t('report.statusReceivedAt', { time: formatDateTime(statusReceivedAt, { dateStyle: 'medium', timeStyle: 'medium' }) })}</p>}
      {snapshotPhase === 'loading' && <p role="status">{t('report.loadingRecords')}</p>}
      {snapshotPhase === 'error' && <p role="alert">{t('report.recordsUnavailable')}</p>}
      {snapshotReceivedAt !== null && <p className="metadata">{t('report.recordsReceivedAt', { time: formatDateTime(snapshotReceivedAt, { dateStyle: 'medium', timeStyle: 'medium' }) })}</p>}
      {snapshot && <p className="metadata">{snapshotPhase !== 'ready' && <>{t('report.lastRecords')} · </>}{t('report.localCount', { count: snapshot.reports.length })}; {t('report.entryCount', { count: snapshot.events.length })}.</p>}
      <button type="button" className="button button--secondary button--compact" disabled={busy || snapshotPhase === 'loading'} onClick={() => void readSnapshot()}>{t('report.refreshRecords')}</button>
      </div></details>
    </section>
    <section className="surface"><ReportProblem identity={identity} reads={reads} /></section>
  </main></AppShell>
}
