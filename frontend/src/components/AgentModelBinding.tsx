import type { Agent } from '../api/agents'
import type { Connection } from '../api/connections'
import { useI18n, type Translate } from '../i18n'
import { en, type MessageKey } from '../i18n/locales/en'

export function agentBindingPresentation(connection: Connection | undefined, t: Translate) {
  const providerKey = `connections.providers.${connection?.provider_id || connection?.provider_kind}`
  const provider = Object.prototype.hasOwnProperty.call(en, providerKey)
    ? t(providerKey as MessageKey) : connection?.provider_id || t('connections.providers.unknown')
  const methodKey: MessageKey | undefined = connection?.auth_method_id ? ({
    api_key: 'connections.methodApiKey', local_none: 'connections.methodLocalNone',
    oauth_browser: 'connections.methodOauthBrowser', oauth_device: 'connections.methodOauthDevice',
    native_chain: 'connections.methodNativeChain',
  } as const)[connection.auth_method_id] : undefined
  const method = methodKey ? t(methodKey) : connection?.is_local && !connection.auth_required
    ? t('connections.methodLocalNone') : undefined
  const authGroup = connection?.auth_method_id === 'oauth_browser' || connection?.auth_method_id === 'oauth_device'
    ? 'oauth' : connection?.auth_method_id || (connection?.is_local && !connection.auth_required ? 'local_none' : 'unknown')
  const providerValue = JSON.stringify(connection ? connection.provider_id || connection.provider_kind : null)
  const authLabel = authGroup === 'oauth' ? 'OAuth' : method || t('connections.reviewUnset')
  return { provider, method, providerValue, authGroup, authLabel,
    searchTerms: connection ? [connection.label, provider, connection.provider_id, connection.provider_kind,
      connection.auth_method_id, method, authGroup, authLabel,
      authGroup === 'api_key' ? 'API key' : authGroup === 'local_none' ? 'local no key' : ''] : [] }
}

// Saved binding only: never substitute the connection's default model or infer
// authentication from a user-defined name, endpoint, or model identifier.
export default function AgentModelBinding({ agent, connection, status }: {
  agent: Agent; connection?: Connection; status: 'ready' | 'loading' | 'error'
}) {
  const { t } = useI18n()
  const { provider } = agentBindingPresentation(connection, t)
  const label = connection ? connection.label || provider : agent.connection_id
    ? t('agents.savedConnectionNumber', { id: String(agent.connection_id) }) : t('agents.noConnection')
  const oauth = connection?.auth_method_id === 'oauth_browser' || connection?.auth_method_id === 'oauth_device'
  const model = agent.model_id || agent.model
  return <div className="stack stack--tight" style={{ overflowWrap: 'anywhere', minWidth: 0 }}>
    <p><strong>{label}{oauth && !/oauth/i.test(label) ? ' OAuth' : ''}</strong><span aria-hidden="true"> / </span><span className="technical">{model || t('connections.reviewUnset')}</span></p>
    <p className="metadata">
      {connection ? <>{t(connection.enabled ? 'connections.enabled' : 'connections.disabled')}</>
        : status === 'ready' ? t('agents.noConnection') : t(status === 'loading' ? 'common.loading' : 'common.error')}
    </p>
  </div>
}
