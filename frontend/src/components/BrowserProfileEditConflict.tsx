import { useI18n } from '../i18n'
import type { BrowserProfile } from './BrowserProfileActionDialog'

export function sameProfileEditBase(before: BrowserProfile, current: BrowserProfile | undefined) {
  return !!current && before.id === current.id && before.revision === current.revision && before.name === current.name
    && before.account_label === current.account_label && before.origin_urls.length === current.origin_urls.length
    && before.origin_urls.every((origin, index) => origin === current.origin_urls[index])
    && before.agent_ids.length === current.agent_ids.length && before.agent_ids.every((id, index) => id === current.agent_ids[index])
}
export default function BrowserProfileEditConflict({ original, current, draft, ready, busy, readOnly, agentName, onAdopt }: {
  original: BrowserProfile; current: BrowserProfile | undefined; draft: { name: string; account: string; origins: string; agentIds: readonly number[] }
  ready: boolean; busy: boolean; readOnly: boolean; agentName: (id: number) => string; onAdopt: () => void
}) {
  const { t } = useI18n()
  const changed = !sameProfileEditBase(original, current), closed = current?.controller === 'disconnected' && !current.connected
  const values = (name: string, account: string, origins: string, ids: readonly number[]) => <div className="stack stack--tight" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
    <p><strong>{t('browserProfiles.name')}</strong>: {name || t('results.unrecorded')}</p>
    <p><strong>{t('browserProfiles.account')}</strong>: {account || t('results.unrecorded')} · {t('browserProfiles.notVerified')}</p>
    <p><strong>{t('browserProfiles.origins')}</strong>{'\n'}{origins || t('results.unrecorded')}</p>
    <p><strong>{t('browserProfiles.agents')}</strong>: {ids.map(agentName).join(' · ') || t('browserProfiles.manualOnly')}</p>
  </div>
  return <section className="notice notice--warning stack stack--tight" aria-label={t('browserProfiles.editConflict.title')}>
    <strong>{t('browserProfiles.editConflict.title')} · {original.name}</strong>
    <p>{t('browserProfiles.editConflict.help')}</p>
    {!ready ? <p role="status">{t('browserProfiles.editConflict.unavailable')}</p>
      : !current ? <p role="status">{t('browserProfiles.editConflict.missing')}</p>
      : !closed && <p role="status">{t('browserProfiles.editConflict.closeFirst')} · {t(`browserProfiles.${current.controller}`)}</p>}
    <div className="form-grid form-grid--two">
      <section><h3 className="section-title">{t('browserProfiles.editConflict.draft')}</h3>{values(draft.name, draft.account, draft.origins, draft.agentIds)}</section>
      {current && <section><h3 className="section-title">{t('browserProfiles.editConflict.saved')}</h3>{values(current.name, current.account_label, current.origin_urls.join('\n'), current.agent_ids)}</section>}
    </div>
    {changed && current && <button type="button" className="button button--secondary" disabled={!ready || !closed || busy || readOnly} onClick={onAdopt}>{t('browserProfiles.editConflict.adopt')}</button>}
  </section>
}
