import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import { useRef, useState } from 'react'
import { useI18n } from '../i18n'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { transferOwnerCurrent } from '../lib/transferActions'
import type { TransferInputSource, TransferPlan } from '../lib/transferWorkspace'

export function WorkspaceImportSource({ source }: { source: TransferInputSource | null | undefined }) {
  const { t, formatDateTime } = useI18n()
  if (!source) return null
  return <div className="metadata stack stack--tight" style={{ overflowWrap: 'anywhere' }}>
    <p>{source.kind === 'file' ? t('projects.importDetails.file', { name: source.name || 'JSON' }) : t('projects.importDetails.manual')}</p>
    <p>{t('projects.importDetails.readAt', { time: formatDateTime(source.readAt, { dateStyle: 'medium', timeStyle: 'medium' }), bytes: source.bytes })}</p>
    {source.kind === 'file' && source.modified && <p className="notice notice--warning">{t('projects.importDetails.modified')}</p>}
  </div>
}

// Render the captured server-resolved plan, never a new read of current settings.
// Prompts/endpoints remain in the original JSON, not copied into this overview.
export default function WorkspaceImportPreview({ identity, plan, fullPlanHint = true }: { identity: string; plan: TransferPlan; fullPlanHint?: boolean }) {
  return <ImportNames key={`${identity}:${directoryViewGeneration()}:${plan.id}`} identity={identity} plan={plan} fullPlanHint={fullPlanHint}/>
}
function ImportNames({ identity, plan, fullPlanHint }: { identity: string; plan: TransferPlan; fullPlanHint: boolean }) {
  const { t } = useI18n(), generation = useRef(directoryViewGeneration()).current
  const [open, setOpen] = useState(false), [query, setQuery] = useState(''), preview = plan.preview
  if (!preview) return null
  const connectionNames = new Map(preview.connections.map(item => [item.ref, item.target_name]))
  const agentNames = new Map(preview.agents.map(item => [item.ref, item.target_name]))
  const projectNames = new Map(preview.projects.map(item => [item.ref, item.target_name]))
  const agentList = (refs: readonly string[]) => refs.length ? refs.map(ref => agentNames.get(ref) || ref).join(', ') : t('projects.exportSnapshot.empty')
  const nameChange = (before: string, after: string) => before === after ? <strong>{after}</strong> : <><span>{before}</span> → <strong>{after}</strong></>
  const matches = directoryMatcher(query)
  const connections = plan.blueprint.connections.filter(item => matches([item.label, connectionNames.get(item.ref), 'public_config' in item ? item.public_config.provider_id : item.provider_kind, item.default_model_id || t('projects.modelUnspecified')]))
  const agents = plan.blueprint.agents.filter(item => matches([item.name, agentNames.get(item.ref), item.model_id || t('projects.modelUnspecified'), connectionNames.get(item.connection_ref) || item.connection_ref]))
  const projects = plan.blueprint.projects.flatMap(project => {
    const fields = [project.name, projectNames.get(project.ref), agentList(project.agent_refs)]
    const projectMatch = matches(fields)
    const templates = projectMatch ? project.room_templates : project.room_templates.filter(template => {
      const resolved = preview.room_templates.find(item => item.project_ref === project.ref && item.ref === template.ref)
      return matches([...fields, template.name, resolved?.target_name, template.room_title, agentList(template.agent_refs)])
    })
    return projectMatch || templates.length > 0 ? [{ project, templates }] : []
  })
  const shown = connections.length + agents.length + projects.length
  const total = plan.blueprint.connections.length + plan.blueprint.agents.length + plan.blueprint.projects.length
  return <details className="compact-details" onToggle={event => { if (transferOwnerCurrent(identity, generation)) setOpen(event.currentTarget.open) }}>
    <summary>{t('projects.importDetails.title')}</summary>
    {open && <div className="stack stack--tight" style={{ overflowWrap: 'anywhere' }}>
      <WorkspaceImportSource source={plan.inputSource}/>
      <p className="field-hint">{t('projects.importDetails.plan')}</p>
      <DirectorySearch value={query} onChange={value => { if (transferOwnerCurrent(identity, generation)) setQuery(value) }}
        label={t('projects.blueprintFind.title')} hint={t('projects.blueprintFind.placeholder')} shown={shown} total={total} known loading={false} preventSubmit/>
      <p className="field-hint">{t('projects.blueprintFind.scope')}</p>
      {fullPlanHint && <p className="field-hint">{t('projects.blueprintFind.importAll')}</p>}
      {query.trim() && !shown && <p role="status">{t('directory.noMatches')}</p>}
      {(!query.trim() || connections.length > 0) && <section className="stack stack--tight"><strong>{t('connections.title')}</strong>
        {connections.length ? <ul>{connections.map(item => <li key={item.ref}>
          {nameChange(item.label, connectionNames.get(item.ref) || item.label)}
          <p className="metadata">{'public_config' in item ? item.public_config.provider_id : item.provider_kind} · {item.default_model_id || t('projects.modelUnspecified')} · {t(item.enabled ? 'connections.enabled' : 'connections.disabled')}</p>
        </li>)}</ul> : <p>{t('projects.exportSnapshot.empty')}</p>}
      </section>}
      {(!query.trim() || agents.length > 0) && <section className="stack stack--tight"><strong>{t('agents.title')}</strong>
        {agents.length ? <ul>{agents.map(item => <li key={item.ref}>
          {nameChange(item.name, agentNames.get(item.ref) || item.name)}
          <p className="metadata">{item.model_id || t('projects.modelUnspecified')} · {connectionNames.get(item.connection_ref) || item.connection_ref}</p>
        </li>)}</ul> : <p>{t('projects.exportSnapshot.empty')}</p>}
      </section>}
      {(!query.trim() || projects.length > 0) && <section className="stack stack--tight"><strong>{t('projects.title')}</strong>
        {projects.length ? <ul>{projects.map(({ project, templates }) => <li key={project.ref}>
          {nameChange(project.name, projectNames.get(project.ref) || project.name)}
          <p className="metadata">{t('projects.participants')}: {agentList(project.agent_refs)}</p>
          {templates.length < project.room_templates.length && <p className="metadata">{t('projects.blueprintFind.templateMatches', { shown: templates.length, total: project.room_templates.length })}</p>}
          {templates.length > 0 && <ul>{templates.map(template => {
            const resolved = preview.room_templates.find(item => item.project_ref === project.ref && item.ref === template.ref)
            return <li key={template.ref}>{nameChange(template.name, resolved?.target_name || template.name)}<p className="metadata">{template.room_title}</p><p className="metadata">{t('projects.participants')}: {agentList(template.agent_refs)}</p></li>
          })}</ul>}
        </li>)}</ul> : <p>{t('projects.exportSnapshot.empty')}</p>}
      </section>}
      <p className="field-hint">{t('projects.importDetails.notVerified')}</p>
    </div>}
  </details>
}
