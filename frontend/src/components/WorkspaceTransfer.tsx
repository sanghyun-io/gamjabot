import { useClipboardText } from '../lib/useClipboardText'
import ClipboardTextFallback from './ClipboardTextFallback'
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { Upload } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { invoke, isTauri } from '@tauri-apps/api/core'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { useViewNavigationIntent } from '../lib/useViewNavigationIntent'
import { requestTransferApply, requestTransferExport, requestTransferPreview, transferOwnerCurrent } from '../lib/transferActions'
import { editTransfer, openTransfer, readTransfer, subscribeTransfer, transferBlocked, transferVersion } from '../lib/transferWorkspace'
import TransferHistory from './TransferHistory'
import WorkspaceImportPreview, { WorkspaceImportSource } from './WorkspaceImportPreview'
import WorkspaceExportPreview from './WorkspaceExportPreview'
import { summarizeWorkspaceExport, workspaceExportText } from '../lib/workspaceExportSnapshot'
import WorkspaceSaveHistory from './WorkspaceSaveHistory'
import { saveReceivedWorkspace } from '../lib/workspaceFileSave'
import { readWorkspaceSaves,subscribeWorkspaceSaves,workspaceSaveVersion } from '../lib/workspaceSaveWorkspace'
const MAX_BLUEPRINT_BYTES = 2 * 1024 * 1024
export default function WorkspaceTransfer({ identity, authEpoch }: { identity: string; authEpoch: number }) {
  const { t } = useI18n(), generation = useRef(directoryViewGeneration()).current
  useSyncExternalStore(subscribeTransfer, transferVersion)
  useSyncExternalStore(subscribeWorkspaceSaves,workspaceSaveVersion)
  const savePending=!!readWorkspaceSaves(identity).pending
  const workspace = readTransfer(identity), raw = workspace.raw, applying = !!workspace.pending, previewing = workspace.plan?.phase === 'pending', exporting = workspace.exported?.phase === 'pending'
  const plan = workspace.plan?.phase === 'succeeded' && workspace.plan.preview && workspace.plan.version === workspace.version && workspace.plan.raw === raw && !workspace.plan.consumed ? { ...workspace.plan, preview: workspace.plan.preview } : null
  const exportSnapshot=workspace.receivedExport
  const exportSummary=useMemo(()=>exportSnapshot?summarizeWorkspaceExport(exportSnapshot):null,[exportSnapshot])
  const nativeOversize=!!exportSummary&&exportSummary.compactBytes>MAX_BLUEPRINT_BYTES
  const [error, setError] = useState<'export' | 'invalid' | 'tooLarge' | ''>(''), [readingFile, setReadingFile] = useState(false)
  const fileSequence = useRef(0), mounted = useRef(false), root = useRef<HTMLDetailsElement>(null), resultRef = useRef<HTMLDivElement>(null), navigation = useViewNavigationIntent(), submitted = useRef<string | null>(null)
  const fileTransport=useQuery({queryKey:['workspace-export-transport'],enabled:isTauri()&&workspace.open,retry:false,refetchOnWindowFocus:false,refetchOnReconnect:false,queryFn:()=>invoke<string>('workspace_export_transport')})
  const valid = () => transferOwnerCurrent(identity, generation) && authEpoch === useAuthStore.getState().epoch
  const blueprintCopy = useClipboardText<{ text: string; exportId: string }>(
    JSON.stringify([identity, authEpoch, generation, workspace.open, exportSnapshot?.id, workspace.exported?.id, savePending]),
    item => valid() && mounted.current && !!root.current?.open && !readWorkspaceSaves(identity).pending
      && readTransfer(identity).receivedExport?.id === item.exportId && readTransfer(identity).exported?.phase !== 'pending')

  useEffect(() => { mounted.current = true;return () => { mounted.current = false;fileSequence.current++;submitted.current = null } }, [])
  const edit = (value: string) => {
    if (!valid()) return
    fileSequence.current++;setReadingFile(false);navigation.cancel();blueprintCopy.invalidate()
    if (new TextEncoder().encode(value).byteLength > MAX_BLUEPRINT_BYTES) { setError('tooLarge');return }
    if (editTransfer(identity,generation,value)) setError('')
  }
  const readFile = async (file: File | undefined) => {
    if (!file || !valid() || applying) return
    const run = ++fileSequence.current, version = readTransfer(identity).version
    navigation.cancel();setReadingFile(true);blueprintCopy.invalidate();setError('')
    try {
      if (file.size > MAX_BLUEPRINT_BYTES) { setError('tooLarge');return }
      const value = await file.text()
      if (valid() && mounted.current && run === fileSequence.current && version === readTransfer(identity).version) {
        if (new TextEncoder().encode(value).byteLength > MAX_BLUEPRINT_BYTES) { setError('tooLarge');return }
        if (editTransfer(identity,generation,value,{kind:'file',name:file.name,readAt:new Date().toISOString(),bytes:file.size,modified:false})) setError('')
      }
    } catch { if (valid() && mounted.current && run === fileSequence.current) setError('invalid') }
    finally { if (valid() && mounted.current && run === fileSequence.current) setReadingFile(false) }
  }
  const preview = () => {
    if (!valid() || readingFile || applying || previewing) return
    navigation.cancel();setError('')
    try { if (!raw.trim()) throw new Error('empty');requestTransferPreview(identity,generation) } catch { setError('invalid') }
  }
  const apply = () => {
    if (!valid() || readingFile || !plan) return
    const original = requestTransferApply(identity,generation,plan.id)
    if (original) { submitted.current = original.id;navigation.begin(original.id,root.current) }
  }
  const emitExport = async (mode: 'copy' | 'download', id: string) => {
    const original = readTransfer(identity).receivedExport
    if (!valid() || !mounted.current || readTransfer(identity).exported?.phase==='pending' || original?.id !== id || !original.result) return
    try {
      const content = workspaceExportText(original.result)
      if (mode === 'copy') await blueprintCopy.copy({ text: content, exportId: id })
      else { if (isTauri()) return;const href = URL.createObjectURL(new Blob([content],{type:'application/json;charset=utf-8'}));try { const anchor=document.createElement('a');anchor.href=href;anchor.download='gamja-workspace-blueprint.json';anchor.click() } finally { URL.revokeObjectURL(href) } }
    } catch (failure) { if (valid() && mounted.current) setError(failure instanceof Error&&failure.message==='workspace_export_too_large'?'tooLarge':'export') }
  }
  const saveReceived=(id:string)=>{if(!valid()||fileTransport.data!=='native'||savePending)return;navigation.cancel();blueprintCopy.invalidate();void saveReceivedWorkspace(identity,generation,id,t('projects.fileExport.title'),()=>mounted.current&&!!root.current?.open&&readTransfer(identity).receivedExport?.id===id)}
  const exportBlueprint = () => {
    if(!valid()||savePending||exporting)return
    navigation.cancel();blueprintCopy.invalidate();setError('')
    requestTransferExport(identity,generation,'preview')
  }
  useEffect(()=>{
    const command=workspace.commands.find(value=>value.id===submitted.current)
    if(!command||!valid())return
    if(!workspace.open||command.waitStopped||workspace.commands.at(-1)?.id!==command.id){navigation.cancel();submitted.current=null;return}
    if(command.phase==='pending')return
    submitted.current=null
    if(command.phase==='succeeded'&&navigation.current(command.id))resultRef.current?.focus({preventScroll:true})
    navigation.cancel()
  },[workspace.commands,workspace.open])
  const message = error ? t(`projects.transfer.${error}` as 'projects.transfer.export') : ''
  const renamed = plan ? [...plan.preview.connections,...plan.preview.agents,...plan.preview.projects,...plan.preview.room_templates].filter(item=>item.source_name!==item.target_name) : []
  const uncertain = transferBlocked(identity) && !applying
  return <div className="stack stack--tight"><details ref={root} open={workspace.open} className="workspace-transfer compact-details" onToggle={event=>{if(!valid())return;openTransfer(identity,generation,event.currentTarget.open);if(!event.currentTarget.open){navigation.cancel();submitted.current=null;blueprintCopy.invalidate()}}}><summary>{t('projects.transfer.title')}</summary>
    <div className="workspace-transfer__body stack">
      <div className="split workspace-transfer__intro"><div><h3 className="section-title">{t('projects.transfer.heading')}</h3><p className="section-copy">{t('projects.transfer.safe')}</p></div><button type="button" className="button button--secondary" disabled={exporting||applying||savePending} onClick={exportBlueprint}>{exporting?t('common.loading'):t(exportSnapshot?'projects.exportSnapshot.refresh':'projects.exportSnapshot.fetch')}</button></div>
      {isTauri()&&fileTransport.isError&&<p role="status">{t('projects.fileExport.unavailable')} <button type="button" className="text-link" onClick={()=>{if(valid())void fileTransport.refetch({cancelRefetch:false})}}>{t('common.retry')}</button></p>}
      <p className="field-hint">{t('projects.transferHistory.retained')}</p>
      {blueprintCopy.result?.status==='copied'&&<p role="status" className="metadata metadata--success">{t('projects.transfer.copied')}</p>}
      {workspace.exported?.phase==='rejected'&&<p role="alert">{t('projects.transfer.export')}{exportSnapshot&&<> {t('projects.exportSnapshot.retainedAfterFailure')}</>}</p>}
      {exportSnapshot?.result&&<div className="notice notice--info stack stack--tight">
        <WorkspaceExportPreview key={`${identity}:${exportSnapshot.id}`} identity={identity} snapshot={exportSnapshot} summary={exportSummary}/>
        {exporting&&<p role="status">{t('projects.exportSnapshot.refreshing')}</p>}
        <p className="field-hint">{t('projects.blueprintFind.exportAll')}</p>
        <div className="page-actions"><button type="button" className="button button--secondary button--compact" disabled={exporting||savePending||blueprintCopy.busy} onClick={()=>{navigation.cancel();setError('');void emitExport('copy',exportSnapshot.id)}}>{t(blueprintCopy.busy?'common.loading':'projects.transferHistory.copyReceived')}</button>{fileTransport.data==='native'&&<button type="button" className="button button--secondary button--compact" disabled={exporting||savePending||nativeOversize} onClick={()=>saveReceived(exportSnapshot.id)}>{t('projects.fileExport.saveReceived')}</button>}{!isTauri()&&<button type="button" className="button button--secondary button--compact" disabled={exporting||savePending} onClick={()=>{navigation.cancel();void emitExport('download',exportSnapshot.id)}}>{t('projects.transfer.exportAction')}</button>}</div>
        {blueprintCopy.result?.status==='failed'&&<p role="alert" className="field-hint">{t('projects.exportSnapshot.copyFailed')}</p>}
        {blueprintCopy.manual&&<ClipboardTextFallback text={blueprintCopy.manual.text} label={t('projects.exportSnapshot.manualCopy')}
          caption={t('chat.clipboard.manualHelp')} canUse={blueprintCopy.manualAllowed} onClose={blueprintCopy.closeManualAndRestore}
          onInteract={blueprintCopy.touchManual} busy={blueprintCopy.busy} onRetry={()=>{if(blueprintCopy.manual)void blueprintCopy.copy(blueprintCopy.manual)}}/>}
      </div>}
      <div className="workspace-transfer__import stack stack--tight"><label className="field">{t('projects.transfer.file')}
        <input type="file" disabled={applying} accept="application/json,.json" onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ''; void readFile(file) }} /></label>
        <label className="field">{t('projects.transfer.paste')}<textarea disabled={applying} rows={7} value={raw} onChange={(event) => edit(event.target.value)}
          placeholder={t('projects.transfer.placeholder')} /></label>
        <button type="button" className="button button--secondary" disabled={!raw.trim() || previewing || applying || readingFile} onClick={() => { void preview() }}>
          <Upload size={16} aria-hidden="true" />{previewing ? t('projects.transfer.previewing') : t('projects.transfer.previewAction')}</button></div>
      <WorkspaceImportSource source={workspace.inputSource}/>
      {readingFile && <p role="status" className="metadata">{t('common.loading')}</p>}
      {applying && <p role="status" className="notice notice--info">{t('projects.transfer.applyingHelp')}</p>}
      {uncertain && <p role="alert" className="notice notice--warning">{t('projects.transferHistory.unknown')}</p>}
      {error && <p role="alert" className="notice notice--danger">{message}</p>}
      {plan && <section className="workspace-transfer__preview stack stack--tight" aria-label={t('projects.transfer.previewHeading')}>
        <div><h3 className="section-title">{t('projects.transfer.previewHeading')}</h3><p className="metadata">{t('projects.transfer.previewHelp')}</p></div>
        <div className="workspace-transfer__counts">
          <span>{t('projects.transfer.connections', { count: plan.preview.counts.connections })}</span>
          <span>{t('projects.transfer.agents', { count: plan.preview.counts.agents })}</span>
          <span>{t('projects.transfer.projects', { count: plan.preview.counts.projects })}</span>
          <span>{t('projects.transfer.templates', { count: plan.preview.counts.room_templates })}</span>
          <span>{t('projects.transfer.assignments', { count: plan.preview.counts.project_assignments })}</span>
          <span>{t('projects.transfer.members', { count: plan.preview.counts.template_members })}</span>
        </div>
        <WorkspaceImportPreview key={plan.id} identity={identity} plan={plan} fullPlanHint={false}/>
        {renamed.length > 0 && <details><summary>{t('projects.transfer.renamed', { count: renamed.length })}</summary><ul className="workspace-transfer__names">
          {renamed.map((item) => <li key={`${item.ref}:${item.target_name}`}><span>{item.source_name}</span><span aria-hidden="true">→</span><strong>{item.target_name}</strong></li>)}</ul></details>}
        {plan.preview.warnings.some((warning) => warning.code === 'connections_need_credentials' && warning.count > 0)
          && <p className="notice notice--warning">{t('projects.transfer.credentials', { count: plan.preview.warnings.find((item) => item.code === 'connections_need_credentials')!.count })}</p>}
        <p className="notice notice--info">{t('projects.blueprintFind.importAll')}</p>
        <button type="button" className="button button--primary" disabled={applying || transferBlocked(identity)} onClick={() => { apply() }}>
          {applying ? t('common.saving') : t('projects.transfer.applyAction')}</button>
      </section>}
      {workspace.plan?.phase === 'rejected' && <p role="alert">{t(workspace.plan.code === 'tooLarge' ? 'projects.transfer.tooLarge' : 'projects.transfer.preview')}</p>}
      {workspace.plan && (workspace.plan.raw !== raw || workspace.plan.version !== workspace.version) && <p className="field-hint">{t('projects.transferHistory.changed')}</p>}
      {workspace.plan?.consumed && <p className="field-hint">{t('projects.transferHistory.consumed')}</p>}
    </div></details><div ref={resultRef} tabIndex={-1}><WorkspaceSaveHistory identity={identity}/><TransferHistory identity={identity}/></div></div>
}
