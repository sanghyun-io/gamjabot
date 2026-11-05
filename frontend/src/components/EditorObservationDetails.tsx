import type { Agent } from '../api/agents'
import type { Project } from '../api/projects'
import type { Connection } from '../api/connections'
import { useI18n } from '../i18n'

// Received public fields only. These readers never fetch, apply a draft,
// inspect a command receipt, or infer whether the original write succeeded.
export function AgentObservationDetails({ agent }: { agent: Agent }) {
  const { t, formatNumber } = useI18n()
  return <details className="compact-details"><summary>{t('editor.observedDetails')}</summary>
    <div className="stack stack--tight">
      <p className="field-hint">{t('editor.observedReadOnlyHelp')}</p>
      <p>{t('agents.description')}</p><p className="section-copy" style={{ whiteSpace: 'pre-wrap' }}>{agent.description || t('projects.noDescription')}</p>
      <p>{t('agents.connection', { value: agent.connection_id ? `#${formatNumber(agent.connection_id)}` : t('projects.connectionMissing') })}</p>
      <p>{t('agents.model', { value: agent.model_id || agent.model || t('projects.modelUnspecified') })}</p>
      {agent.is_active === false && <p className="metadata">{t('projects.agentInactive')}</p>}
    </div>
  </details>
}

export function ProjectObservationDetails({ project }: { project: Project }) {
  const { t, formatNumber } = useI18n()
  const team = Array.isArray(project.agents) ? project.agents : null
  return <details className="compact-details"><summary>{t('editor.observedDetails')}</summary>
    <div className="stack stack--tight">
      <p className="field-hint">{t('editor.observedReadOnlyHelp')}</p>
      <p>{t('projects.description')}</p><p className="section-copy" style={{ whiteSpace: 'pre-wrap' }}>{project.description || t('projects.noDescription')}</p>
      <p>{team ? t('projects.assignedCount', { count: team.length }) : t('projects.assigned')}</p>
      {team === null ? <p className="metadata">{t('projects.assignment.notRead')}</p> : team.length ? <ul>{team.map(agent => <li key={agent.id}>
        #{formatNumber(agent.id)} · {agent.name} · {agent.model_id || agent.model || t('projects.modelUnspecified')}{agent.is_active === false && ` · ${t('projects.agentInactive')}`}
      </li>)}</ul> : <p className="metadata">{t('projects.noneAssigned')}</p>}
    </div>
  </details>
}

export type ConnectionObservation = Pick<Connection, 'provider_id' | 'provider_kind' | 'default_model_id' | 'enabled' | 'has_secret'>
export function ConnectionObservationDetails({ connection }: { connection: ConnectionObservation }) {
  const { t } = useI18n()
  return <details className="compact-details"><summary>{t('editor.observedDetails')}</summary>
    <div className="stack stack--tight">
      <p className="field-hint">{t('editor.observedReadOnlyHelp')}</p>
      <p>{t('connections.provider')} · <span className="technical">{connection.provider_id || connection.provider_kind}</span></p>
      <p>{t('connections.defaultModel')} · <span className="technical">{connection.default_model_id || t('projects.modelUnspecified')}</span></p>
      <p>{t('connections.observedUsage', { state: t(connection.enabled ? 'connections.enabled' : 'connections.disabled') })}</p>
      <p>{t(connection.has_secret ? 'connections.observedCredentialPresent' : 'connections.observedCredentialAbsent')}</p>
      <p className="field-hint">{t('connections.observedCredentialHelp')}</p>
    </div>
  </details>
}
