import { useI18n } from '../../i18n'
import type { LocalSetupAttempt } from '../../lib/localSetupWorkspace'
import CommandHistoryArchive, { type ArchiveCommand } from '../CommandHistoryArchive'
import FeatureCheckHistory from './FeatureCheckHistory'
import { FeatureReportDetails } from './FeatureTable'

// Read only the original retained attempts. Never restore the form or re-run a check.
export default function LocalSetupHistory({ identity, attempts, currentId }: {
  identity: string; attempts: readonly LocalSetupAttempt[]; currentId: string | null
}) {
  const { t } = useI18n()
  const previous = attempts.filter(attempt => attempt.id !== currentId)
  if (!previous.length) return null
  const latest = previous.at(-1)
  const pinned = new Set(previous.filter(attempt => attempt.status === 'creating' || attempt.status === 'unknown'
    || attempt.checks.some(check => ['preparing', 'checking', 'unknown'].includes(check.status))).map(attempt => attempt.id))
  if (latest) pinned.add(latest.id)
  const status = (attempt: LocalSetupAttempt) => t(attempt.status === 'unknown' ? 'localModels.saveUnknown'
    : attempt.status === 'saved' ? 'localModels.session.saved' : attempt.status === 'rejected' ? 'editor.rejected' : 'editor.saving')
  const content = (attempt: LocalSetupAttempt) => <div className="notice stack stack--tight" style={{ overflowWrap: 'anywhere' }}>
    <strong>{attempt.body.label}{attempt.saved && ` · #${attempt.saved.id}`}</strong>
    <p className="metadata">{attempt.model} · {attempt.url}</p>
    <p role="status">{status(attempt)}</p>
    {attempt.checks.length > 0 && <FeatureCheckHistory checks={attempt.checks}
      context={{ connectionId: attempt.saved?.id, modelId: attempt.model, baseUrl: attempt.url }}/>} 
    {attempt.storedReport && <FeatureReportDetails report={attempt.storedReport} summary={t('localModels.session.storedReport')}
      help={t('localModels.session.readNotReceipt')}/>} 
    {!attempt.storedReport && !attempt.checks.some(check => check.report) && <p className="field-hint">{t('localModels.previous.noReport')}</p>}
  </div>
  const older: ArchiveCommand[] = previous.filter(attempt => !pinned.has(attempt.id)).slice().reverse().map<ArchiveCommand>(attempt => ({
    id: attempt.id, phase: attempt.status === 'saved' ? 'succeeded' : 'rejected',
    search: [attempt.body.label, attempt.body.provider_id, attempt.model, attempt.url, String(attempt.saved?.id ?? ''), attempt.id, status(attempt)],
    content: content(attempt),
  }))
  return <section className="stack stack--tight" aria-label={t('localModels.previous.title')}>
    <h3 className="section-title">{t('localModels.previous.title')}</h3>
    <p className="field-hint">{t('localModels.previous.scope')}</p>
    {previous.filter(attempt => pinned.has(attempt.id)).slice().reverse().map(attempt => <div key={attempt.id}>{content(attempt)}</div>)}
    {older.length > 0 && <CommandHistoryArchive key={identity} title={t('localModels.session.previous')}
      items={older} phases={['succeeded', 'rejected']}/>}
  </section>
}
