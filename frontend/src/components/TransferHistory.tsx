import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import WorkspaceImportPreview from './WorkspaceImportPreview'
import { Link } from 'react-router-dom'
import { connectionsApi } from '../api/connections'
import { agentsApi } from '../api/agents'
import { projectsApi } from '../api/projects'
import { useI18n } from '../i18n'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { transferOwnerCurrent } from '../lib/transferActions'
import { acknowledgeTransfer, editTransfer, readTransfer, stopTransferWait, subscribeTransfer, transferVersion, type TransferCommand } from '../lib/transferWorkspace'
function OriginalImport({ identity, command }: { identity: string; command: TransferCommand }) {
  const { t, formatDateTime } = useI18n(), generation = useRef(directoryViewGeneration()).current, valid = () => transferOwnerCurrent(identity, generation)
  const [observed, setObserved] = useState<string[] | null>(null), [failed, setFailed] = useState(false), [reading, setReading] = useState(false), pending = useRef(false), mounted = useRef(false)
  useEffect(() => { mounted.current = true;return () => { mounted.current = false } }, [])
  const inspect = async () => {
    if (!valid() || pending.current) return
    pending.current = true;setReading(true);setFailed(false)
    try {
      const [connections, agents, projects] = await Promise.all([connectionsApi.list(), agentsApi.list(), projectsApi.list('all')])
      if (!valid() || !mounted.current) return
      const p = command.plan.preview
      setObserved([...connections.filter(item => p.connections.some(expected => expected.target_name === item.label)).map(item => `#${item.id} · ${item.label}`), ...agents.filter(item => p.agents.some(expected => expected.target_name === item.name)).map(item => `#${item.id} · ${item.name}`), ...projects.filter(item => p.projects.some(expected => expected.target_name === item.name)).map(item => `#${item.id} · ${item.name}`)])
    } catch { if (valid() && mounted.current) setFailed(true) }
    finally { pending.current = false;if (valid() && mounted.current) setReading(false) }
  }
  const counts = command.result?.counts || command.plan.preview.counts
  return <div className={`notice stack stack--tight ${command.phase === 'succeeded' ? 'notice--success' : command.phase === 'pending' ? 'notice--info' : 'notice--warning'}`}>
    <p className="metadata">{t('projects.workspace.started', { time: formatDateTime(command.startedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>
    <p role={command.phase === 'unknown' || command.phase === 'rejected' ? 'alert' : 'status'}>{t(command.phase === 'pending' ? 'projects.transfer.applyingHelp' : command.phase === 'succeeded' ? 'projects.transfer.applied' : command.phase === 'unknown' ? 'projects.workspace.unknown' : command.code === 'workspace_plan_stale' ? 'projects.transfer.stale' : 'editor.rejected')}</p>
    <p>{t('projects.transfer.connections', { count: counts.connections })} · {t('projects.transfer.agents', { count: counts.agents })} · {t('projects.transfer.projects', { count: counts.projects })} · {t('projects.transfer.templates', { count: counts.room_templates })}</p>
    {command.phase !== 'succeeded' && <p className="metadata">{t('projects.transferHistory.countsPlanned')}</p>}
    {command.finishedAt && <p className="metadata">{t('projects.workspace.received', { time: formatDateTime(command.finishedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>}
    {command.result && <><p>{t('projects.transferHistory.originalResult')}</p>{command.result.credentials_unset > 0 && <p>{t('projects.transfer.registerKeys', { count: command.result.credentials_unset })}</p>}<details className="compact-details"><summary>{t('projects.transferHistory.added')}</summary><ul>{command.result.connections.map(item=><li key={`c${item.id}`}>#{item.id} · {item.label}</li>)}{command.result.agents.map(item=><li key={`a${item.id}`}>#{item.id} · {item.name}</li>)}{command.result.projects.map(item=><li key={`p${item.id}`}>#{item.id} · {item.name}</li>)}{command.result.room_templates.map(item=><li key={`t${item.id}`}>#{item.id} · {item.name}</li>)}</ul></details></>}
    <WorkspaceImportPreview identity={identity} plan={command.plan}/>
    <div className="page-actions"><Link className="text-link" to="/connections" onClick={event=>{if(!valid())event.preventDefault()}}>{t('connections.title')}</Link><Link className="text-link" to="/agents" onClick={event=>{if(!valid())event.preventDefault()}}>{t('agents.title')}</Link></div>
    {command.phase === 'pending' && <button type="button" className="button button--secondary button--compact" onClick={() => { if (valid() && window.confirm(t('projects.workspace.stopConfirm')) && valid()) stopTransferWait(identity, generation, command.id) }}>{t('projects.workspace.stopWaiting')}</button>}
    {command.phase === 'unknown' && <><button type="button" className="button button--secondary button--compact" disabled={reading} onClick={() => void inspect()}>{t('editor.inspect')}</button>{failed && <p role="alert">{t('editor.inspectFailure')}</p>}{observed && <><p>{t('projects.transferHistory.observation')}</p>{observed.length ? <ul>{observed.map((name,index)=><li key={index}>{name}</li>)}</ul> : <p>{t('editor.noMatch')}</p>}</>}{!command.acknowledged && <button type="button" className="button button--secondary button--compact" disabled={reading || failed || !observed} onClick={() => { if (valid() && window.confirm(t('projects.transferHistory.newConfirm')) && valid()) acknowledgeTransfer(identity, generation, command.id) }}>{t('projects.workspace.allowNew')}</button>}</>}
    <button type="button" className="button button--secondary button--compact" disabled={!!readTransfer(identity).pending} onClick={() => { if (valid() && window.confirm(t('projects.transferHistory.useConfirm')) && valid()) editTransfer(identity, generation, command.plan.raw, command.plan.inputSource) }}>{t('projects.transferHistory.useInput')}</button>
    <details className="compact-details"><summary>{t('common.technicalDetails')}</summary><p>{t('projects.transferHistory.contract')}</p><pre className="technical">{JSON.stringify({ plan_hash: command.plan.preview.plan_hash, preview: command.plan.preview, blueprint: command.plan.blueprint },null,2)}</pre>{command.code && <p className="technical">{command.code}</p>}</details>
  </div>
}
export default function TransferHistory({ identity }: { identity: string }) {
  const { t } = useI18n();useSyncExternalStore(subscribeTransfer, transferVersion)
  const commands = readTransfer(identity).commands, latest = commands[commands.length - 1]
  if (!latest) return null
  const important = new Set(commands.filter(command => command.phase === 'pending' || command.phase === 'unknown' && !command.acknowledged).map(command=>command.id));important.add(latest.id)
  return <section className="stack stack--tight" aria-label={t('projects.transferHistory.title')}><p className="field-hint">{t('projects.transferHistory.retained')}</p>{commands.filter(command=>important.has(command.id)).map(command=><OriginalImport key={command.id} identity={identity} command={command}/>)}{commands.some(command=>!important.has(command.id)) && <details className="compact-details"><summary>{t('projects.workspace.previous')}</summary>{commands.filter(command=>!important.has(command.id)).map(command=><OriginalImport key={command.id} identity={identity} command={command}/>)}</details>}</section>
}
