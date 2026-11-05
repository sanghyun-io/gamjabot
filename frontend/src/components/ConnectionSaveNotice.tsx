import type { AuthMethodId, ProtocolId, Capability } from '../api/connections'
import type { MessageKey } from '../i18n/locales/en'
import { useI18n } from '../i18n'
import EditorSaveNotice, { type SavedItem } from './EditorSaveNotice'
import { ConnectionObservationDetails, type ConnectionObservation } from './EditorObservationDetails'

export type ConnectionSavedItem = SavedItem & { observation?: ConnectionObservation }

export type ConnectionSaveFeedback = {
  identity: string; attempt: number; phase: 'saving' | 'success' | 'rejected' | 'unknown'
  target: { id: number | null; label: string; providerId: string; modelId: string }
  hadKey: boolean
  requestDetails?: { authMethodId: AuthMethodId; protocolId: ProtocolId; enabled: boolean;
    requestedOptions: Capability[]; keyHandling: 'included' | 'retain' | 'none' }
}

export function connectionSentKeyLabel(handling: 'included' | 'retain' | 'none'): MessageKey {
  return handling === 'included' ? 'connections.sentKeyIncluded'
    : handling === 'retain' ? 'connections.sentKeyRetained' : 'connections.sentKeyNone'
}
const methodLabels: Record<AuthMethodId, MessageKey> = {
  api_key: 'connections.methodApiKey', local_none: 'connections.methodLocalNone', oauth_browser: 'connections.methodOauthBrowser',
  oauth_device: 'connections.methodOauthDevice', native_chain: 'connections.methodNativeChain',
}
const capabilityLabels: Record<Capability, MessageKey> = {
  max_tokens: 'connections.cap.maxTokens', temperature: 'connections.cap.temperature', tools: 'connections.cap.tools', vision: 'connections.cap.vision',
}

export default function ConnectionSaveNotice({ feedback, onInspect, onDismiss, history = false, onShowSaved, findingSaved = false }: {
  feedback: ConnectionSaveFeedback | null; onInspect: () => Promise<ConnectionSavedItem[]>; onDismiss: () => void; history?: boolean; onShowSaved?: () => void; findingSaved?: boolean
}) {
  const { t } = useI18n()
  if (!feedback) return null
  return <section className="stack stack--tight" aria-label={t('connections.saveFeedback')}>
    <p className="metadata">{feedback.target.id !== null && <>#{feedback.target.id} · </>}{feedback.target.label}</p>
    {feedback.phase === 'success'
      ? <p role="status" className="notice notice--success">{t('connections.savedNamed', { name: feedback.target.label })}</p>
      : <EditorSaveNotice key={`${feedback.identity}:${feedback.attempt}`} phase={feedback.phase} identity={feedback.identity} onInspect={onInspect} renderItem={item => <><strong>#{item.id} · {item.name}</strong>{item.observation && <ConnectionObservationDetails connection={item.observation}/>}</>} />}
    {feedback.requestDetails && <details className="compact-details">
      <summary>{t('connections.saveRequestDetails')}</summary>
      <div className="stack stack--tight">
        <p className="field-hint">{t('connections.saveRequestHelp')}</p>
        <dl className="connection-confirm">
          <dt>{t('connections.provider')}</dt><dd className="technical">{feedback.target.providerId}</dd>
          <dt>{t('connections.defaultModel')}</dt><dd className="technical">{feedback.target.modelId}</dd>
          <dt>{t('connections.authMethod')}</dt><dd>{t(methodLabels[feedback.requestDetails.authMethodId])}</dd>
          <dt>{t('connections.protocol')}</dt><dd className="technical">{feedback.requestDetails.protocolId}</dd>
          <dt>{t('connections.use')}</dt><dd>{t(feedback.requestDetails.enabled ? 'connections.enabled' : 'connections.disabled')}</dd>
          <dt>{t('connections.capabilities')}</dt><dd>{feedback.requestDetails.requestedOptions.map(option => t(capabilityLabels[option])).join(', ') || t('connections.reviewRequestsNone')}</dd>
          <dt>{t('connections.reviewCredential')}</dt><dd>{t(connectionSentKeyLabel(feedback.requestDetails.keyHandling))}</dd>
        </dl>
      </div>
    </details>}
    {feedback.hadKey && (feedback.phase === 'saving' || feedback.phase === 'rejected' || feedback.phase === 'unknown') && <p className="field-hint">{t('connections.sentKeyCleared')}</p>}
    {feedback.phase === 'unknown' && <p className="field-hint">{t('connections.unknownSaveHelp')}</p>}
    {feedback.phase === 'success' && feedback.target.id !== null && onShowSaved && <button type="button" className="button button--secondary button--compact" disabled={findingSaved} aria-label={`${t('connections.savedFind.action')} · ${feedback.target.label} · #${feedback.target.id}`} onClick={onShowSaved}>{t(findingSaved?'common.loading':'connections.savedFind.action')}</button>}
    {!history && (feedback.phase === 'success' || feedback.phase === 'rejected') && <button type="button" className="button button--ghost button--compact" onClick={onDismiss}>{t('common.close')}</button>}
  </section>
}
