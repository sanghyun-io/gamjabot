import { BotNotificationSettingsDetails } from '../components/BotNotificationSettings'
import AvatarEditor from '../components/AvatarEditor'
import { randomAvatar, type AvatarConfig } from '../lib/avatarRecipe'
import AgentFilterPresets from '../components/AgentFilterPresets'
import { presetFilterLabel } from '../lib/agentFilterPresets'
import AgentModelBinding, { agentBindingPresentation } from '../components/AgentModelBinding'
import DirectoryDescription from '../components/DirectoryDescription'
import AgentDraftComparison from '../components/AgentDraftComparison'
import AgentCreationDraftChoice from '../components/AgentCreationDraftChoice'
import {readAgentConnectionFilter} from '../lib/agentConnectionFilter'
import { useDirectoryValue, useDirectoryPosition } from '../lib/useDirectoryView'
import { useDialogFocus } from '../lib/useDialogFocus'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useQuery, useInfiniteQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { agentWorkloadsApi, type WorkloadState } from '../api/agentWorkloads'
import { agentsApi } from '../api/agents'
import { botDirectoryApi, matchesBotDirectoryCommand, validBotDirectory, type BotDirectory, type BotDirectoryWrite } from '../api/botDirectory'
import type { Agent, AgentInput } from '../api/agents'
import { connectionsApi } from '../api/connections'
import type { Connection } from '../api/connections'
import { Plus, Edit2, Trash2, Bot, MoreHorizontal, RefreshCw, SlidersHorizontal } from 'lucide-react'
import { AppShell, PageHeader } from '../components/ui'
import { useI18n } from '../i18n'
import AgentWorkloads from '../components/AgentWorkloads'
import AgentRemoveDialog, { removalTarget, sameRemovalTarget, type AgentRemovalTarget } from '../components/AgentRemoveDialog'
import { AgentCharacter } from '../components/AgentCharacter'
import AgentConversationButton from '../components/AgentConversationButton'
import { PrimaryAgentAction } from '../components/PrimaryAgent'
import AgentMemoryDialog from '../components/AgentMemoryDialog'
import BotDuplicateDialog from '../components/BotDuplicateDialog'
import BotTemplateDialog from '../components/BotTemplateDialog'
import BotTemplatePublishDialog from '../components/BotTemplatePublishDialog'
import { hasPendingTemplateImport } from '../api/botTemplates'
import SkillAssignments from '../components/SkillAssignments'
import McpAssignments from '../components/McpAssignments'
import BotRoutines from '../components/BotRoutines'
import RoutineAgenda from '../components/RoutineAgenda'
import { SavedModelPicker } from '../components/connections/SavedModelPicker'
import ModelFeatures from '../components/connections/ModelFeatures'
import ListLoadNotice from '../components/ListLoadNotice'
import DirectorySearch, { directoryMatcher, type DirectorySearchHandle } from '../components/DirectorySearch'
import EditorSavedNotice, { focusSavedEditorCard, type SavedEditorResult } from '../components/EditorSavedNotice'
import EditorDraftNotice from '../components/EditorDraftNotice'
import { editorSaveFailure } from '../components/EditorSaveNotice'
import AgentCommandHistory from '../components/AgentCommandHistory'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { agentCommandBlocked, agentDraftKey, agentWorkspaceVersion, beginAgentCommand, discardAgentDraft, dismissAgentSaved, keepAgentDraft, readAgentWorkspace, settleAgentCommand, subscribeAgentWorkspace, type AgentCommand, type AgentDraft } from '../lib/agentEditorWorkspace'
import { editorTextLength, validEditorModelId, validEditorTemperature, focusEditorField } from '../lib/editorValidation'
import { useAuthStore } from '../stores/authStore'
import { readAgentCreation } from '../lib/agentCreationRoute'

export type TemplateLinkRequest={id:string;url:string;identity:string}
export default function Agents({templateRequest,onTemplateOpened}:{templateRequest?:TemplateLinkRequest|null;onTemplateOpened?:(id:string)=>void}={}) {
  const identity = useAuthStore((state) => `${state.user?.id || 0}:${state.epoch}`)
  return <AgentDirectory key={`${identity}:${directoryViewGeneration()}`} identity={identity} templateRequest={templateRequest} onTemplateOpened={onTemplateOpened}/>
}
function AgentDirectory({ identity,templateRequest,onTemplateOpened }: { identity: string;templateRequest?:TemplateLinkRequest|null;onTemplateOpened?:(id:string)=>void }) {
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [editingAgent, setEditingAgent] = useState<Agent | null>(null)
  const [duplicateAgent, setDuplicateAgent] = useState<Agent | null>(null)
  const [publishAgent,setPublishAgent]=useState<Agent|null>(null)
  const [templateDialog,setTemplateDialog]=useState<{mode:'export'|'import';source?:Agent;initialUrl?:string}|null>(null)
  const [removingAgent, setRemovingAgent] = useState<AgentRemovalTarget | null>(null)
  const [memoryAgent, setMemoryAgent] = useState<{ id: number; name: string } | null>(null)
  const generation = useRef(directoryViewGeneration()).current
  const mounted=useRef(true)
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[])
  const location = useLocation()
  const [,setAgentSearchParams]=useSearchParams()
  const connectionFilter=useMemo(()=>readAgentConnectionFilter(location.search),[location.search])
  const creation = readAgentCreation(location.state, identity, generation)
  const [dismissedCreation, setDismissedCreation] = useState<string | null>(null)
  const submittedHere = useRef<string | null>(null)
  useSyncExternalStore(subscribeAgentWorkspace, agentWorkspaceVersion)
  const workspace = readAgentWorkspace(identity)
  const draftKey = agentDraftKey
  const saving = !!workspace.pending, blocked = agentCommandBlocked(identity)
  const latest = workspace.commands[workspace.commands.length - 1]
  const savedResult: SavedEditorResult | null = latest?.phase === 'succeeded' && latest.result && latest.kind !== 'delete' && !latest.dismissed
    ? { id: latest.result.id, name: latest.result.name, kind: latest.kind === 'create' ? 'created' : 'updated', snapshotVersion: latest.snapshotVersion, identity } : null
  const queryClient = useQueryClient()
  const { t, formatNumber, formatDateTime, locale } = useI18n()
  const isCurrent = (sent: string) => { const state = useAuthStore.getState(); return mounted.current && !!state.token && generation === directoryViewGeneration() && sent === `${state.user?.id || 0}:${state.epoch}` }
  useEffect(()=>{
    if(!templateRequest||templateRequest.identity!==identity||!isCurrent(identity)||hasPendingTemplateImport(identity)||showCreateModal||editingAgent||duplicateAgent||publishAgent||templateDialog||removingAgent||memoryAgent||blocked||document.querySelector('[aria-modal="true"], dialog[open]'))return
    setTemplateDialog({mode:'import',initialUrl:templateRequest.url});onTemplateOpened?.(templateRequest.id)
  },[templateRequest,identity,showCreateModal,editingAgent,duplicateAgent,publishAgent,templateDialog,removingAgent,memoryAgent,blocked,onTemplateOpened])
  const [directoryQuery, setDirectoryQuery] = useDirectoryValue<string>(identity, 'agent.search', '')
  const [providerFilter, setProviderFilter] = useDirectoryValue<string>(identity, 'agent.provider', '')
  const [authFilter, setAuthFilter] = useDirectoryValue<string>(identity, 'agent.auth', '')
  const [modelFilter, setModelFilter] = useDirectoryValue<string>(identity, 'agent.model', '')
  const directoryRoot = useRef<HTMLElement>(null)
  const directoryFinder = useRef<DirectorySearchHandle>(null)
  const [pendingSavedFocus, setPendingSavedFocus] = useState<{ identity: string; id: number } | null>(null)
  const [routineFocus,setRoutineFocus]=useState<{identity:string;id:number}|null>(null)
  const [routineSwitch,setRoutineSwitch]=useState<number|null>(null)
  const changeDirectoryQuery = (value: string) => {
    if (!isCurrent(identity)) return
    setPendingSavedFocus(null); setDirectoryQuery(value)
  }
  const clearDirectorySearch = () => {
    if (!(isCurrent(identity)) || !directoryRoot.current?.isConnected) return
    directoryFinder.current?.focus()
    changeDirectoryQuery('')
  }



  const changeConnectionFilter=(value:number|null)=>{
    if(!isCurrent(identity)||value!==null&&(!Number.isSafeInteger(value)||value<1))return
    const next=new URLSearchParams(location.search)
    if(value===null)next.delete('connection');else next.set('connection',String(value))
    setPendingSavedFocus(null)
    setAgentSearchParams(next,{replace:true,state:location.state,preventScrollReset:true})
  }
  const widenConnectionFilter = () => {
    if (!isCurrent(identity) || !directoryRoot.current?.isConnected) return
    directoryFinder.current?.focus()
    changeConnectionFilter(null)
  }
  const agentsQuery = useQuery({
    retry: false,
    queryKey: ['agents', identity],
    queryFn: async () => { if (!isCurrent(identity)) throw new Error('Owner changed'); const value = await agentsApi.list(); if (!isCurrent(identity)) throw new Error('Owner changed'); return value },
  })
  const readAgents = () => { if (!isCurrent(identity) || readAgentWorkspace(identity).pending || queryClient.getQueryState(['agents', identity])?.fetchStatus === 'fetching') return; void agentsQuery.refetch({cancelRefetch: false}) }
  const agents = agentsQuery.data || []
  const directory = useQuery({ queryKey:['bot-directory',identity], retry:false, refetchOnWindowFocus:false, queryFn:async()=>{ const value=await botDirectoryApi.read(); if(!isCurrent(identity))throw Error('Owner changed'); return value } })
  const [showHidden,setShowHidden]=useState(()=>new URLSearchParams(location.search).get('hidden')==='1')
  const sectionIme=useImeFormGuard(`${identity}:bot-directory-sections`)
  const [sectionName,setSectionName]=useState('')
  const [editingSection,setEditingSection]=useState<{id:string;name:string}|null>(null)
  const pendingStorageKey=`bot-directory-pending:${identity}`
  const [directoryPending,setDirectoryPending]=useState<{key:string;body:BotDirectoryWrite}|null>(()=>{try {const value=JSON.parse(sessionStorage.getItem(pendingStorageKey)||'null');return value && typeof value.key==='string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value.key) && value.body && validBotDirectory({version:1,revision:value.body.expected_revision,sections:value.body.sections,items:value.body.items}) ? value : null}catch{return null}})
  const clearDirectoryPending=()=>{try{sessionStorage.removeItem(pendingStorageKey)}catch{}setDirectoryPending(null)}
  const [directoryBusy,setDirectoryBusy]=useState(false)
  const directoryLock=useRef(false)
  const [directoryError,setDirectoryError]=useState<'unknown'|'missing'|'conflict'|'rejected'|null>(()=>directoryPending?'unknown':null)
  const layout=directory.data
  const acceptDirectoryReceipt=async()=>{
    clearDirectoryPending();setDirectoryError(null)
    // Receipt proves the old write; only a fresh Owner-scoped read describes the current layout.
    try{await directory.refetch({cancelRefetch:false})}catch{/* The query's read-error notice remains separate from write recovery. */}
  }
  const itemFor=(id:number)=>layout?.items.find(item=>item.agent_id===id) || {agent_id:id,pinned:false,hidden:false,section_id:null}
  const commitLayout=async(sections:BotDirectory['sections'],items:BotDirectory['items'])=>{
    if(!layout||directoryLock.current||directoryBusy||directoryPending||directory.isFetching||directory.isError||directoryError==='conflict'||!isCurrent(identity))return
    const body={expected_revision:layout.revision,sections,items};const key=crypto.randomUUID();try{sessionStorage.setItem(pendingStorageKey,JSON.stringify({key,body}))}catch{setDirectoryError('rejected');return}setDirectoryPending({key,body});directoryLock.current=true;setDirectoryBusy(true);setDirectoryError(null)
    try {const result=await botDirectoryApi.save(body,key);if(!isCurrent(identity))return;if(!matchesBotDirectoryCommand(result,body,key))throw Error('Receipt mismatch');await acceptDirectoryReceipt()}
    catch(error){if(!isCurrent(identity))return;const status=(error as {response?:{status?:number;data?:{detail?:{code?:string}}}})?.response?.status;const code=(error as {response?:{data?:{detail?:{code?:string}}}})?.response?.data?.detail?.code;if(status===409&&code==='bot_directory_changed'){clearDirectoryPending();setDirectoryError('conflict')}else if(status===422){clearDirectoryPending();setDirectoryError('rejected')}else setDirectoryError('unknown')}
    finally {directoryLock.current=false;if(isCurrent(identity))setDirectoryBusy(false)}
  }
  const updateItem=(id:number,patch:Partial<ReturnType<typeof itemFor>>)=>{if(!layout)return;const item={...itemFor(id),...patch};void commitLayout(layout.sections,[...layout.items.filter(x=>x.agent_id!==id),item])}
  const checkDirectoryReceipt=async()=>{
    if(!directoryPending||directoryLock.current||directoryBusy||!isCurrent(identity))return
    directoryLock.current=true;setDirectoryBusy(true)
    try{const result=await botDirectoryApi.receipt(directoryPending.key);if(!isCurrent(identity))return;if(!matchesBotDirectoryCommand(result,directoryPending.body,directoryPending.key))throw Error('Receipt mismatch');await acceptDirectoryReceipt()}
    catch(error){if(isCurrent(identity)){const response=(error as {response?:{status?:number;data?:{detail?:{code?:string}}}})?.response;setDirectoryError(response?.status===404&&response.data?.detail?.code==='bot_directory_command_not_found'?'missing':'unknown')}}
    finally{directoryLock.current=false;if(isCurrent(identity))setDirectoryBusy(false)}
  }
  const resendMissingDirectory=async()=>{
    if(!directoryPending||directoryError!=='missing'||directoryLock.current||directoryBusy||!isCurrent(identity))return
    const original=directoryPending;directoryLock.current=true;setDirectoryBusy(true);setDirectoryError('unknown')
    try{const result=await botDirectoryApi.save(original.body,original.key);if(!isCurrent(identity))return;if(!matchesBotDirectoryCommand(result,original.body,original.key))throw Error('Receipt mismatch');await acceptDirectoryReceipt()}
    catch(error){if(isCurrent(identity)){const response=(error as {response?:{status?:number;data?:{detail?:{code?:string}}}})?.response;if(response?.status===409&&response.data?.detail?.code==='bot_directory_changed'){clearDirectoryPending();setDirectoryError('conflict')}else setDirectoryError('unknown')}}
    finally{directoryLock.current=false;if(isCurrent(identity))setDirectoryBusy(false)}
  }
  const connectionsQuery = useQuery({ queryKey: ['connections', identity], queryFn: async () => { const value = await connectionsApi.list(); if (!isCurrent(identity)) throw new Error('Owner changed'); return value } })
  const retryConnections=()=>{if(!isCurrent(identity)||readAgentWorkspace(identity).pending||queryClient.getQueryState(['connections',identity])?.fetchStatus==='fetching')return;void connectionsQuery.refetch({cancelRefetch:false})}
  const connections = connectionsQuery.data || []
  const connectionStatus = connectionsQuery.isError ? 'error' : connectionsQuery.isSuccess ? 'ready' : 'loading'
  const connectionById = useMemo(() => new Map(connections.map(row => [row.id, row])), [connections])
  const bindingOptions = useMemo(() => {
    const providers = new Map<string, string>(), methods = new Map<string, string>(), models = new Map<string, string>()
    for (const agent of agents) {
      const connection = agent.connection_id ? connectionById.get(agent.connection_id) : undefined
      const binding = agentBindingPresentation(connection, t)
      providers.set(binding.providerValue, connection ? binding.provider : t(connectionStatus === 'ready' ? 'agents.noConnection' : connectionStatus === 'loading' ? 'common.loading' : 'common.error'))
      methods.set(binding.authGroup, binding.authLabel)
      const model = agent.model_id || agent.model || ''
      models.set(JSON.stringify(model), model || t('connections.reviewUnset'))
    }
    const sorted = (rows: Map<string, string>) => [...rows].sort((a, b) => a[1].localeCompare(b[1]))
    return { providers: sorted(providers), methods: sorted(methods), models: sorted(models) }
  }, [agents, connectionById, connectionStatus, t])
  const visibleItems = useMemo(() => {
    const matches = directoryMatcher(directoryQuery)
    return agents.filter(item => {
      const binding = agentBindingPresentation(item.connection_id ? connectionById.get(item.connection_id) : undefined, t)
      return (connectionFilter.kind === 'all' || connectionFilter.kind === 'one' && item.connection_id === connectionFilter.id)
        && (!providerFilter || binding.providerValue === providerFilter)
        && (!authFilter || binding.authGroup === authFilter)
        && (!modelFilter || JSON.stringify(item.model_id || item.model || '') === modelFilter)
        && matches([item.name, item.description, item.model_id, item.model, String(item.id), `#${item.id}`, ...binding.searchTerms], item.id)
    })
  }, [agents, directoryQuery, connectionFilter, providerFilter, authFilter, modelFilter, connectionById, t])
  const arrangedItems=useMemo(()=>visibleItems.filter(item=>showHidden ? itemFor(item.id).hidden : !itemFor(item.id).hidden).sort((a,b)=>{const x=itemFor(a.id),y=itemFor(b.id);if(x.pinned!==y.pinned)return x.pinned?-1:1;const sectionIndex=(id:string|null)=>id===null ? (layout?.sections.length||0) : Math.max(0,layout?.sections.findIndex(s=>s.id===id)??0);return sectionIndex(x.section_id)-sectionIndex(y.section_id)}),[visibleItems,layout,showHidden])
  const visibleIds = useMemo(() => new Set(arrangedItems.map(item => item.id)), [arrangedItems])
  const filtersActive = Boolean(directoryQuery || providerFilter || authFilter || modelFilter || connectionFilter.kind !== 'all')
  const resetDirectoryFilters = (focus = true) => {
    if (!isCurrent(identity)) return
    if (focus) directoryFinder.current?.focus()
    changeConnectionFilter(null); setDirectoryQuery(''); setProviderFilter(''); setAuthFilter(''); setModelFilter('')
    setPendingSavedFocus(null)
  }
  useDirectoryPosition(identity, 'agent', JSON.stringify([directoryQuery, connectionFilter, providerFilter, authFilter, modelFilter]), directoryRoot, agentsQuery.data !== undefined)
  const workloads = useInfiniteQuery({
    queryKey: ['agent-list-workloads', identity],
    retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => { if (!isCurrent(identity)) throw new Error('Owner changed'); const value = await agentWorkloadsApi.list(pageParam, undefined, 100); if (!isCurrent(identity)) throw new Error('Owner changed'); return value },
    getNextPageParam: (page) => page.next_cursor || undefined,
  })
  const activity = new Map(workloads.data?.pages.flatMap((page) => page.items).map((item) => [item.agent_id, item]))
  const creationConnection = creation ? connections.find(item => item.id === creation.connectionId) : undefined
  const newDraft = workspace.drafts.get(draftKey())
  const hasNewDraft = !!newDraft
  const prepareCreation = () => {
    if (!creation || !isCurrent(identity) || showCreateModal || editingAgent) return
    // Read the shared workspace at the click, so a newly retained draft wins.
    if (!readAgentWorkspace(identity).drafts.has(draftKey())) {
      if (agentCommandBlocked(identity) || connectionStatus !== 'ready' || !creationConnection?.enabled) return
      const empty = defaultAgentDraft(null)
      keepAgentDraft(identity, generation, undefined, { ...empty, connectionId: creation.connectionId, modelId: creation.modelId }, null, empty)
    }
    setEditingAgent(null); setShowCreateModal(true)
  }

  const inspectCommand = async (command: AgentCommand) => {
    if (!isCurrent(identity)) throw new Error('Owner changed')
    const result = await agentsQuery.refetch({ cancelRefetch: false })
    if (result.isError || !isCurrent(identity)) throw new Error('List read failed')
    return (result.data || []).filter(item => command.target.id !== undefined ? item.id === command.target.id : item.name === command.target.name)
  }
  const sendCommand = async (original: AgentCommand) => {
    if (!isCurrent(identity)) return
    try {
      const result = original.kind === 'delete' ? await agentsApi.delete(original.target.id!)
        : original.kind === 'create' ? await agentsApi.create(original.body!) : await agentsApi.update(original.target.id!, original.body!)
      if (!isCurrent(identity)) return
      if (original.kind !== 'delete' && (!result || result.user_id !== useAuthStore.getState().user?.id || !Number.isSafeInteger(result.id) || result.id <= 0 || original.kind === 'update' && result.id !== original.target.id)) throw new Error('Agent response target mismatch')
      settleAgentCommand(identity, generation, original, 'succeeded', result || undefined)
      void queryClient.invalidateQueries({ queryKey: ['agents', identity] })
      void queryClient.invalidateQueries({ queryKey: ['agent-avatars', identity] })
      void queryClient.invalidateQueries({ queryKey: ['agent-list-workloads', identity] })
      void queryClient.invalidateQueries({ queryKey: ['workload-browser', identity] })
      void queryClient.invalidateQueries({ queryKey: ['primary-bot'] })
    } catch (error) {
      if (isCurrent(identity)) settleAgentCommand(identity, generation, original, editorSaveFailure(error) === 'rejected' ? 'rejected' : 'unknown')
    }
  }
  useEffect(() => {
    const command = workspace.commands.find(value => value.id === submittedHere.current)
    if (!command || command.phase === 'pending' || command.phase === 'unknown' && command.waitStopped && !command.finishedAt) return
    submittedHere.current = null
    if (command.phase === 'succeeded' && command.kind !== 'delete' && (showCreateModal || editingAgent) && draftKey(editingAgent?.id) === draftKey(command.target.id)) {
      setShowCreateModal(false); setEditingAgent(null)
    }
  }, [workspace.commands, showCreateModal, editingAgent])
  const savedReady = Boolean(savedResult && savedResult.identity === identity && agentsQuery.isSuccess && !agentsQuery.isFetching && agentsQuery.dataUpdatedAt !== savedResult.snapshotVersion && agentsQuery.data?.some(item => item.id === savedResult.id))
  useEffect(() => {
    if (!pendingSavedFocus) return
    if (pendingSavedFocus.identity !== identity || savedResult?.id !== pendingSavedFocus.id) { setPendingSavedFocus(null); return }
    if (directoryQuery || !savedReady || !visibleIds.has(pendingSavedFocus.id) || !isCurrent(identity)) return
    focusSavedEditorCard('agent', pendingSavedFocus.id); setPendingSavedFocus(null)
  }, [pendingSavedFocus, identity, savedResult, directoryQuery, savedReady, visibleIds])

  useEffect(()=>{
    if(!routineFocus)return
    if(routineFocus.identity!==identity||!isCurrent(identity)){setRoutineFocus(null);return}
    if(!visibleIds.has(routineFocus.id))return
    const card=directoryRoot.current?.querySelector<HTMLElement>(`#agent-card-${routineFocus.id}`)
    const details=card?.querySelector<HTMLDetailsElement>('details.bot-routines')
    if(!details)return
    details.open=true;details.querySelector<HTMLElement>('summary')?.focus();details.scrollIntoView({block:'nearest'});setRoutineFocus(null)
  },[routineFocus,identity,visibleIds])
  const moveToAgendaBot=(id:number)=>{
    if(!isCurrent(identity)||!agents.some(agent=>agent.id===id))return
    resetDirectoryFilters(false);setShowHidden(!!itemFor(id).hidden);setRoutineFocus({identity,id});setRoutineSwitch(null)
  }
  const manageAgendaBot=(id:number)=>{
    if(!isCurrent(identity)||!agents.some(agent=>agent.id===id))return
    const switchesView=!!itemFor(id).hidden!==showHidden
    if(switchesView){setRoutineSwitch(id);return}
    moveToAgendaBot(id)
  }

  const handleDelete = (agent: Agent) => {
    if (!isCurrent(identity) || agentCommandBlocked(identity)) return
    setRemovingAgent(removalTarget(agent))
  }
  const confirmRemoval = () => {
    if (!removingAgent || !isCurrent(identity) || agentCommandBlocked(identity)) return
    // Re-read the existing query cache at the explicit confirmation click.
    // This is not a server CAS guarantee or an additional list request.
    const state = queryClient.getQueryState(['agents', identity])
    const current = queryClient.getQueryData<Agent[]>(['agents', identity])?.find(item => item.id === removingAgent.id)
    if (state?.status !== 'success' || state.fetchStatus !== 'idle' || !sameRemovalTarget(removingAgent, current) || current?.user_id !== useAuthStore.getState().user?.id) return
    const original = beginAgentCommand(identity, generation, { kind: 'delete', target: { id: removingAgent.id, name: removingAgent.name }, snapshotVersion: state.dataUpdatedAt })
    if (original) { setRemovingAgent(null); void sendCommand(original) }
  }

  return (
    <AppShell>
      <PageHeader title={t('agents.title')} backTo="/" actions={
            <><button type="button" data-bot-template-import className="button button--secondary" disabled={blocked||showCreateModal||!!editingAgent||!!duplicateAgent||!!templateDialog} onClick={()=>{if(isCurrent(identity))setTemplateDialog({mode:'import'})}}>{t('botTemplate.importAction')}</button><button onClick={() => { if (isCurrent(identity)) setShowCreateModal(true) }} className="button button--primary">
              <Plus size={20} />
              <span>{t(workspace.drafts.has(draftKey()) ? 'editor.continueDraft' : 'agents.new')}</span>
            </button></>} />

      <main ref={directoryRoot} className="page-frame page-frame--wide page-content stack agent-directory-page">
        {agentsQuery.data&&<RoutineAgenda key={identity} agents={agents} onManage={manageAgendaBot} listStale={agentsQuery.isError} managementReady={!!layout&&!directory.isError&&!directory.isFetching} managementLoading={directory.isPending||directory.isFetching} onRetryManagement={()=>{if(isCurrent(identity))void directory.refetch({cancelRefetch:false})}}/> }
        {routineSwitch!==null&&<div role="alert" className="notice notice--warning"><p>{t('routine.title')} · {locale==='ko'?'다른 Bot 목록으로 바꾸면 이전 목록에서 저장하지 않은 입력이 사라질 수 있습니다. 계속할까요?':'Switching Bot lists can discard unsaved inputs in the previous list. Continue?'}</p><button type="button" onClick={()=>moveToAgendaBot(routineSwitch)}>{locale==='ko'?'목록 바꾸기':'Switch list'}</button> <button type="button" onClick={()=>setRoutineSwitch(null)}>{t('common.cancel')}</button></div>}
        {directory.isError && <p role="alert">{t('botDirectory.readFailed')} <button className="button button--secondary" onClick={()=>void directory.refetch()}>{t('common.retry')}</button></p>}
        {directoryError && <div role="alert" className="surface stack"><p>{t(`botDirectory.${directoryError}`)}</p>{(directoryError==='unknown'||directoryError==='missing')&&<button className="button button--secondary" disabled={directoryBusy} onClick={()=>void checkDirectoryReceipt()}>{t('botDirectory.check')}</button>}{directoryError==='missing'&&<button className="button button--secondary" disabled={directoryBusy} onClick={()=>void resendMissingDirectory()}>{t('botDirectory.resend')}</button>}{directoryError==='conflict'&&<button className="button button--secondary" disabled={directoryBusy} onClick={()=>{setDirectoryBusy(true);void directory.refetch({cancelRefetch:false}).then(result=>{if(isCurrent(identity)&&!result.isError)setDirectoryError(null)}).finally(()=>{if(isCurrent(identity))setDirectoryBusy(false)})}}>{t('botDirectory.reload')}</button>}</div>}
        {layout && <><p className="field-hint">{t('botDirectory.hideHelp')}</p><div className="page-actions"><button className="button button--secondary" onClick={()=>setShowHidden(!showHidden)}>{t(showHidden?'botDirectory.showVisible':'botDirectory.showHidden')}</button><details className="compact-details"><summary>{t('botDirectory.sections')}</summary><div className="stack stack--tight"><form {...sectionIme.handlers} onSubmit={e=>{e.preventDefault();if(sectionIme.isSubmitBlocked(e)||!sectionName.trim()||layout.sections.length>=100||directoryBusy||directoryPending)return;void commitLayout([...layout.sections,{id:crypto.randomUUID(),name:sectionName.trim()}],layout.items);setSectionName('')}}><label>{t('botDirectory.sectionName')} <input value={sectionName} maxLength={80} onChange={e=>setSectionName(e.target.value)} /></label> <button className="button button--secondary" disabled={!sectionName.trim()||directoryBusy||!!directoryPending}>{t('botDirectory.add')}</button></form>{layout.sections.map(section=><div key={section.id} className="page-actions">{editingSection?.id===section.id ? <form {...sectionIme.handlers} onSubmit={e=>{e.preventDefault();const name=editingSection.name.trim();if(sectionIme.isSubmitBlocked(e)||!name||directoryBusy||directoryPending)return;void commitLayout(layout.sections.map(s=>s.id===section.id?{...s,name}:s),layout.items);setEditingSection(null)}}><label>{t('botDirectory.sectionName')} <input autoFocus value={editingSection.name} maxLength={80} onChange={e=>setEditingSection({...editingSection,name:e.target.value})}/></label> <button className="button button--secondary" disabled={!editingSection.name.trim()||directoryBusy||!!directoryPending}>{t('common.save')}</button> <button type="button" className="button button--ghost" onClick={()=>setEditingSection(null)}>{t('common.cancel')}</button></form> : <><span>{section.name}</span><button type="button" className="button button--ghost" disabled={directoryBusy||directory.isFetching||directory.isError||!!directoryPending} onClick={()=>setEditingSection({id:section.id,name:section.name})}>{t('common.edit')}</button><button type="button" className="button button--ghost" disabled={directoryBusy||directory.isFetching||directory.isError||!!directoryPending} onClick={()=>void commitLayout(layout.sections.filter(s=>s.id!==section.id),layout.items.map(i=>i.section_id===section.id?{...i,section_id:null}:i))}>{t('botDirectory.removeSection')}</button></>}</div>)}</div></details></div></>}
        {creation && dismissedCreation !== location.key && <section className="notice notice--info stack stack--tight" aria-label={t('agents.fromConnection.title')}>
          <strong>{t('agents.fromConnection.title')}</strong>
          <p>{creationConnection?.label || t('agents.savedConnectionNumber', { id: String(creation.connectionId) })} · #{creation.connectionId} · <span className="technical">{creation.modelId}</span></p>
          <p className="field-hint">{t('agents.fromConnection.help')}</p>
          {hasNewDraft ? <p role="status">{t('agents.fromConnection.existing')}</p> : connectionStatus !== 'ready' || !creationConnection?.enabled ? <p role="status">{t(connectionStatus === 'loading' ? 'common.loading' : 'agents.fromConnection.unavailable')}</p> : null}
          {newDraft&&<AgentCreationDraftChoice key={`${identity}:${location.key}:${creation.connectionId}:${creation.modelId}`} identity={identity} generation={generation} selection={creation} record={newDraft} initial={defaultAgentDraft(null)} connections={connections}
            ready={connectionsQuery.isSuccess&&!connectionsQuery.isFetching&&!queryClient.getQueryState(['connections',identity])?.isInvalidated} reading={connectionsQuery.isFetching} blocked={blocked||showCreateModal||!!editingAgent} canUse={()=>isCurrent(identity)} onRead={retryConnections} onOpen={()=>{if(isCurrent(identity)){setEditingAgent(null);setShowCreateModal(true)}}}/>}
          <div className="page-actions"><button type="button" className="button button--primary" disabled={showCreateModal || !!editingAgent || !hasNewDraft && (blocked || connectionStatus !== 'ready' || !creationConnection?.enabled)} onClick={prepareCreation}>{t(hasNewDraft ? 'agents.draftChoice.keep' : 'agents.fromConnection.open')}</button>
            <button type="button" className="text-link" onClick={() => { if (isCurrent(identity)) setDismissedCreation(location.key) }}>{t('common.close')}</button></div>
        </section>}
        <EditorSavedNotice result={savedResult?.identity === identity ? savedResult : null} fetching={agentsQuery.isFetching}
          ready={savedReady}
          onShow={() => { if (savedResult && isCurrent(identity)) { resetDirectoryFilters(false); setPendingSavedFocus({ identity, id: savedResult.id }) } }}
          onReload={() => { if (isCurrent(identity)) void agentsQuery.refetch() }} onDismiss={() => { if (latest && isCurrent(identity)) dismissAgentSaved(identity, generation, latest.id) }} />
        {workspace.commands.some(command => command.phase === 'pending' || command.phase === 'unknown' && !command.acknowledged) && <AgentCommandHistory identity={identity} onInspect={inspectCommand}/>}
        {connectionStatus === 'error' && <p role="alert" className="notice notice--danger">{t('agents.connectionLoadFailure')} <button className="danger-link" disabled={connectionsQuery.isFetching} onClick={retryConnections}>{t(connectionsQuery.isFetching?'common.loading':'agents.retryConnections')}</button></p>}
        <div className="directory-controls page-tools">
        <DirectorySearch compact exactId label={t('agents.searchCompact')} ref={directoryFinder} value={directoryQuery} onChange={changeDirectoryQuery} hint={t('directory.agentHint')}
          shown={arrangedItems.length} total={agents.length} loading={agentsQuery.isPending} known={agentsQuery.data !== undefined && !!layout}
          controls={agents.length ? 'agent-directory-results' : undefined} />
          <details className="directory-filter-panel">
            <summary className="button button--secondary"><SlidersHorizontal size={16} aria-hidden="true" />{t('agents.filterOptions')}{[connectionFilter.kind !== 'all', !!providerFilter, !!authFilter, !!modelFilter].filter(Boolean).length > 0 && <span className="metadata">{[connectionFilter.kind !== 'all', !!providerFilter, !!authFilter, !!modelFilter].filter(Boolean).length}</span>}</summary>
            <div className="directory-filter-panel__body stack">
        <section className="stack stack--tight" aria-label={t('agents.connectionFilter.title')}>
          <label className="field">{t('agents.connectionFilter.title')}<select value={connectionFilter.kind==='all'?'':connectionFilter.kind==='one'?String(connectionFilter.id):'invalid'} onChange={event=>{
            if(!isCurrent(identity))return
            if(!event.target.value)changeConnectionFilter(null)
            else{const id=Number(event.target.value);if(String(id)===event.target.value)changeConnectionFilter(id)}
          }}>
            <option value="">{t('agents.connectionFilter.all')}</option>
            {connectionFilter.kind==='invalid'&&<option value="invalid" disabled>{t('agents.connectionFilter.invalid')}</option>}
            {connectionFilter.kind==='one'&&!connections.some(row=>row.id===connectionFilter.id)&&<option value={connectionFilter.id}>{t('agents.savedConnectionNumber',{id:String(connectionFilter.id)})} · {t('agents.connectionFilter.unlisted')}</option>}
            {connections.map(row=><option key={row.id} value={row.id}>#{row.id} · {row.label}</option>)}
          </select></label>
          <p className="field-hint">{t('agents.connectionFilter.help')}</p>
          {connectionFilter.kind!=='all'&&<><p className="notice notice--info">{t('agents.connectionFilter.scope')}</p><button type="button" className="text-link" onClick={widenConnectionFilter}>{t('agents.connectionFilter.clear')}</button></>}
        </section>
        <div className="page-actions" style={{ alignItems: 'start' }}>
          {([
            { label: t('agents.filters.provider'), value: providerFilter, set: setProviderFilter, options: bindingOptions.providers },
            { label: t('agents.filters.method'), value: authFilter, set: setAuthFilter, options: bindingOptions.methods },
            { label: t('agents.filters.model'), value: modelFilter, set: setModelFilter, options: bindingOptions.models },
          ]).map(filter => <label className="field" style={{ flex: '1 1 12rem', minWidth: 0 }} key={filter.label}>
            {filter.label}<select value={filter.value} aria-controls={agents.length ? 'agent-directory-results' : undefined} onChange={event => {
              if (!isCurrent(identity)) return
              setPendingSavedFocus(null); filter.set(event.target.value)
            }}>
              <option value="">{t('agents.filters.all')}</option>
              {filter.value && !filter.options.some(([value]) => value === filter.value) && <option value={filter.value} disabled>{t('agents.connectionFilter.unlisted')}</option>}
              {filter.options.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>)}
        </div>
        <p className="field-hint">{t('agents.filters.help')}</p>
            </div>
          </details>
          <details className="directory-saved-searches">
            <summary className="button button--ghost">{t('agents.savedSearches')}</summary>
            <div className="directory-saved-searches__body">
        <AgentFilterPresets identity={identity}
          current={{ query: directoryQuery, provider: providerFilter, auth: authFilter, model: modelFilter, connectionId: connectionFilter.kind === 'one' ? connectionFilter.id : null }}
          canSave={connectionFilter.kind !== 'invalid'}
          onApply={filters => {
            if (!isCurrent(identity)) return
            changeConnectionFilter(filters.connectionId); setDirectoryQuery(filters.query)
            setProviderFilter(filters.provider); setAuthFilter(filters.auth); setModelFilter(filters.model)
            setPendingSavedFocus(null)
          }}
          describe={filters => [
            { label: t('directory.search'), value: filters.query || t('agents.filters.all') },
            { label: t('agents.connectionFilter.title'), value: filters.connectionId === null ? t('agents.connectionFilter.all') : connections.find(row => row.id === filters.connectionId)?.label || t('agents.savedConnectionNumber', { id: String(filters.connectionId) }) },
            { label: t('agents.filters.provider'), value: bindingOptions.providers.find(([value]) => value === filters.provider)?.[1] || presetFilterLabel(filters.provider, t('agents.filters.all'), t('agents.noConnection')) },
            { label: t('agents.filters.method'), value: bindingOptions.methods.find(([value]) => value === filters.auth)?.[1] || filters.auth || t('agents.filters.all') },
            { label: t('agents.filters.model'), value: presetFilterLabel(filters.model, t('agents.filters.all'), t('connections.reviewUnset')) },
          ]} />
            </div>
          </details>
        </div>
        {filtersActive && <div className="directory-active-filter">{layout && <span className="metadata">{t('directory.matches', { shown: arrangedItems.length, total: agents.length })}</span>}<button type="button" className="button button--ghost button--compact" onClick={() => resetDirectoryFilters()}>{t('agents.filters.reset')}</button></div>}
        {connectionFilter.kind === 'invalid' && <p role="alert" className="notice notice--warning">{t('agents.connectionFilter.invalid')}</p>}
        {workspace.drafts.size > 0 && <details className="compact-details"><summary>{t('agents.workspace.drafts')}</summary><div className="stack stack--tight">{[...workspace.drafts.entries()].map(([key, record]) => <button key={key} type="button" className="button button--secondary" onClick={() => { if (!isCurrent(identity)) return; setEditingAgent(record.baseline); setShowCreateModal(!record.baseline) }}>{key === 'new' ? t('agents.workspace.newAgent') : `#${key}`} · {record.value.name || t('editor.continueDraft')}</button>)}</div></details>}
        {(workloads.isFetching || workloads.isError) && workloads.data && <p className="notice notice--info">{t('workloads.browse.retained')}</p>}
        {workloads.isError && <p role="alert" className="notice notice--warning">{t(workloads.isFetchNextPageError ? 'workloads.browse.moreFailure' : 'workloads.browse.refreshFailure')}</p>}
        <ListLoadNotice failed={agentsQuery.isError} fetching={agentsQuery.isFetching} hasSnapshot={agentsQuery.data !== undefined}
          message={t('agents.listFailure')} onRetry={() => { if (isCurrent(identity)) void agentsQuery.refetch() }} />
        {directory.isPending ? <p>{t('common.loading')}</p> : !layout ? null : agentsQuery.isPending ? (
          <div className="surface">
            <p role="status" className="section-copy">{t('common.loading')}</p>
          </div>
        ) : agents.length === 0 && agentsQuery.isError ? null : agents.length === 0 ? (
          <div className="surface stack">
            <Bot color="var(--text-muted)" size={40} />
            <p className="section-copy">{t('agents.empty')}</p>
            <button
              onClick={() => { if (isCurrent(identity)) setShowCreateModal(true) }}
              className="button button--primary"
            >
              {t('agents.first')}
            </button>
          </div>
        ) : (
          <div className="agent-directory" id="agent-directory-results">
            {arrangedItems.length === 0 && showHidden && !filtersActive ? <div className="surface stack" style={{ gridColumn: '1 / -1' }}><p className="section-copy">{t('botDirectory.noneHidden')}</p><div className="page-actions"><button type="button" className="button button--secondary" onClick={()=>setShowHidden(false)}>{t('botDirectory.showVisible')}</button></div></div> : arrangedItems.length === 0 && !showHidden && !filtersActive && agents.some(agent=>itemFor(agent.id).hidden) ? <div className="surface stack" style={{ gridColumn: '1 / -1' }}><p className="section-copy">{t('botDirectory.allHidden')}</p><div className="page-actions"><button type="button" className="button button--secondary" onClick={()=>setShowHidden(true)}>{t('botDirectory.showHidden')}</button></div></div> : arrangedItems.length === 0 && <div className="surface stack" style={{ gridColumn: '1 / -1' }}>
              <p className="section-copy">{t('directory.noMatches')}</p>
              <div className="page-actions">
                <button type="button" className="button button--secondary" onClick={() => resetDirectoryFilters()}>{t('agents.filters.reset')}</button>
                {connectionFilter.kind !== 'all' && <button type="button" className="button button--secondary" onClick={widenConnectionFilter}>{t('agents.connectionFilter.widen')}</button>}
                {!!directoryQuery && <button type="button" className="button button--secondary" onClick={clearDirectorySearch}>{t('directory.clear')}</button>}
              </div>
              {connectionFilter.kind !== 'all' && <p className="field-hint">{t('agents.connectionFilter.widenHelp')}</p>}
            </div>}
            {arrangedItems.map((agent, index) => {
              const item=itemFor(agent.id), previous=index?itemFor(arrangedItems[index-1].id):null
              const group=item.pinned ? t('botDirectory.pinned') : (layout?.sections.find(section=>section.id===item.section_id)?.name || t('botDirectory.ungrouped'))
              const showGroup=!previous || previous.pinned!==item.pinned || !item.pinned && previous.section_id!==item.section_id
              const workload = activity.get(agent.id)
              const state: WorkloadState = workload?.overall_state || 'unknown'
              return (
              <Fragment key={agent.id}>{showGroup && <h2 className="section-title" style={{gridColumn:'1 / -1'}}>{group}</h2>}<div
                id={`agent-card-${agent.id}`} tabIndex={-1} hidden={!visibleIds.has(agent.id)} style={{ display: visibleIds.has(agent.id) ? undefined : 'none' }}
                className="surface stack agent-directory-card"
              >
                <div className="agent-directory-card__header">
                  <div className="agent-directory-card__identity">
                    <h3 className="section-title heading-with-icon">
                      <AgentCharacter agentId={agent.id} avatar={agent.avatar} />{agent.name}<span className="metadata"> · #{agent.id}</span>{savedReady && savedResult?.id === agent.id && <span className="metadata"> · {t('editor.justSaved')}</span>}
                    </h3>
                    <p className="agent-directory-card__role section-copy" title={agent.description || t('agents.roleMissing')}>{agent.description || t('agents.roleMissing')}</p>
                  </div>
                  <span className={`workload-state workload-state--${state}`}>{workload ? t(`workloads.state.${state}`) : t(workloads.isPending ? 'common.loading' : 'workloads.state.unknown')}</span>
                  <details className="agent-directory-menu"><summary className="icon-button" aria-label={`${t('agents.moreActions')} ${agent.name}`}><MoreHorizontal size={20} /></summary>
                    <div className="agent-directory-menu__items">
                      {layout && <><button type="button" aria-label={`${agent.name} · ${t(item.pinned?'botDirectory.unpin':'botDirectory.pin')}`} className="button button--ghost button--compact" disabled={directoryBusy||directory.isFetching||directory.isError||!!directoryPending} onClick={()=>updateItem(agent.id,{pinned:!item.pinned})}>{t(item.pinned?'botDirectory.unpin':'botDirectory.pin')}</button><button type="button" aria-label={`${agent.name} · ${t(item.hidden?'botDirectory.restore':'botDirectory.hide')}`} className="button button--ghost button--compact" disabled={directoryBusy||directory.isFetching||directory.isError||!!directoryPending} onClick={()=>updateItem(agent.id,{hidden:!item.hidden})}>{t(item.hidden?'botDirectory.restore':'botDirectory.hide')}</button><label>{t('botDirectory.move')} <select aria-label={`${agent.name} · ${t('botDirectory.move')}`} value={item.section_id||''} disabled={directoryBusy||directory.isFetching||directory.isError||!!directoryPending} onChange={e=>updateItem(agent.id,{section_id:e.target.value||null})}><option value="">{t('botDirectory.ungrouped')}</option>{layout.sections.map(section=><option key={section.id} value={section.id}>{section.name}</option>)}</select></label></>}
                      <button type="button" data-bot-publish className="button button--ghost button--compact" aria-label={`${agent.name} · ${t('botPublish.action')}`} disabled={blocked||!agent.is_active||!!duplicateAgent||!!templateDialog||!!publishAgent||showCreateModal||!!editingAgent} onClick={()=>{if(isCurrent(identity))setPublishAgent(agent)}}>{t('botPublish.action')}</button>
                      <button type="button" className="button button--ghost button--compact" aria-label={`${agent.name} · ${t('botTemplate.exportAction')}`} disabled={blocked||!agent.is_active||!!duplicateAgent||!!templateDialog||showCreateModal||!!editingAgent} onClick={()=>{if(isCurrent(identity))setTemplateDialog({mode:'export',source:agent})}}>{t('botTemplate.exportAction')}</button>
                      <button type="button" className="button button--ghost button--compact" aria-label={`${agent.name} · ${t('botDuplicate.action')}`} disabled={blocked||!agent.is_active||!!templateDialog||!!duplicateAgent||showCreateModal||!!editingAgent} onClick={()=>{if(isCurrent(identity))setDuplicateAgent(agent)}}>{t('botDuplicate.action')}</button>
                      <PrimaryAgentAction agent={agent} />
                      <Link className="button button--ghost button--compact" to={`/agents/${agent.id}/library`}>{t('library.title')}</Link>
                      <button type="button" className="button button--ghost button--compact" aria-label={`${agent.name} · ${t('botMemory.title')}`} onClick={() => { if (isCurrent(identity)) setMemoryAgent({ id: agent.id, name: agent.name }) }}>{t('botMemory.title')}</button>
                      <button aria-label={`${agent.name} · ${t(workspace.drafts.has(draftKey(agent.id)) ? 'editor.continueDraft' : 'common.edit')}`} onClick={() => { if (isCurrent(identity)) setEditingAgent(agent) }} className="button button--secondary"><Edit2 size={16} />{t(workspace.drafts.has(draftKey(agent.id)) ? 'editor.continueDraft' : 'common.edit')}</button>
                      <button aria-label={`${agent.name} · ${t('common.delete')}`} onClick={() => handleDelete(agent)} disabled={blocked} className="button button--secondary danger-link"><Trash2 size={16} />{t('common.delete')}</button>
                    </div>
                  </details>
                </div>
                <div className="agent-directory-card__binding"><AgentModelBinding agent={agent} connection={connections.find(item => item.id === agent.connection_id)} status={connectionStatus} /></div>
                <AgentConversationButton agentId={agent.id} name={agent.name} disabled={blocked || !agent.is_active} />
                {agent.is_active && <SkillAssignments agentId={agent.id} />}
                {agent.is_active && <McpAssignments agentId={agent.id} />}
                {agent.is_active && <BotRoutines agentId={agent.id} />}
                {agent.is_active && <BotNotificationSettingsDetails agentId={agent.id} agentName={agent.name}/>}
                <div className="agent-directory-card__details">
                  {workload && !workload.rooms.length && <span className="metadata">{t('agents.participatingRooms', { count: workload.counts.total_rooms })}</span>}
                {workload && workload.rooms.length > 0 && <details className="compact-details agent-directory-room-details"><summary>{t('agents.participatingRooms', { count: workload.counts.total_rooms })}</summary><div className="agent-directory-rooms">
                  {workload.rooms.slice(0, 3).map((room) => <Link key={room.room_id} to={room.href} className="workload-room"><span><strong>{room.room_title}</strong><small>{room.project_name}</small></span><span className={`workload-state workload-state--${room.state}`}>{t(`workloads.state.${room.state}`)}</span></Link>)}
                  {workload.rooms.length > 3 && <details className="compact-details"><summary>{t('agents.otherRooms', { count: workload.rooms.length - 3 })}</summary>{workload.rooms.slice(3).map((room) => <Link key={room.room_id} to={room.href} className="workload-room"><span><strong>{room.room_title}</strong><small>{room.project_name}</small></span><span>{t(`workloads.state.${room.state}`)}</span></Link>)}</details>}
                  {workload.rooms_truncated && <p className="metadata">{t('workloads.roomsTruncated', { count: workload.counts.total_rooms })}</p>}
                </div></details>}
                <details className="compact-details agent-directory-card__settings"><summary>{t('agents.settingsDetails')}</summary>
                <div className="metadata stack stack--tight">
                  <DirectoryDescription text={agent.description} fallback={t('agents.roleMissing')} />
                  <p>{t('agents.connection', { value: (() => {
                    if (connectionStatus !== 'ready') return `${t('agents.savedConnectionNumber', { id: String(agent.connection_id ?? 0) })} · ${connectionStatus === 'error' ? t('common.error') : t('common.loading')}`
                    const connection = connections.find((item) => item.id === agent.connection_id)
                    return connection ? `${connection.label} (#${connection.id}) · ${t(connection.enabled ? 'connections.enabled' : 'connections.disabled')}` : t('agents.noConnection')
                  })() })}</p>
                  <p>{t('agents.model', { value: agent.model_id || agent.model })}</p>
                  <p>{t('agents.temperature', { value: agent.temperature === 'auto' ? t('agents.providerDefault') : String(agent.temperature ?? '') })}</p>
                  <p>{t('agents.maxTokens', { value: agent.max_tokens === 0 ? t('agents.providerDefault') : formatNumber(agent.max_tokens) })}</p>
                </div></details>
                </div>
              </div></Fragment>
            )})}
          </div>
        )}
        {workloads.hasNextPage && <button className="button button--secondary" disabled={workloads.isFetching} onClick={() => { if (isCurrent(identity)) void workloads.fetchNextPage({ cancelRefetch: false }) }}>{t(workloads.isFetchNextPageError ? 'workloads.browse.retryMore' : 'workloads.more')}</button>}
        <details className="directory-supporting-details"><summary>{t('agents.listOptions')}</summary><div className="stack">
          <div className="page-actions"><AgentWorkloads/></div>
          <p className="field-hint">{t('agents.subtitle')}</p>
        <div className="split agent-list-toolbar"><p className="section-copy">{t('agents.activitySnapshot')}</p><button className="button button--secondary" disabled={workloads.isFetching} onClick={() => { if (isCurrent(identity)) void workloads.refetch({ cancelRefetch: false }) }}><RefreshCw size={16} />{t('agents.refreshActivity')}</button></div>
        {workloads.dataUpdatedAt > 0 && <p className="metadata">{t('workloads.browse.lastReceived', { time: formatDateTime(new Date(workloads.dataUpdatedAt).toISOString(), { dateStyle: 'medium', timeStyle: 'short' }) })}</p>}
          <p className="field-hint">{t('agents.workspace.draftScope')}</p>
          {!(workspace.commands.some(command => command.phase === 'pending' || command.phase === 'unknown' && !command.acknowledged)) && <AgentCommandHistory identity={identity} onInspect={inspectCommand}/>}
        </div></details>
      </main>
      {publishAgent && <BotTemplatePublishDialog key={`${identity}:${publishAgent.id}`} agent={publishAgent} canUse={()=>isCurrent(identity)} onClose={()=>setPublishAgent(null)} />}
      {templateDialog && <BotTemplateDialog key={`${identity}:${templateDialog.mode}:${templateDialog.source?.id||0}`} mode={templateDialog.mode} source={templateDialog.source} initialUrl={templateDialog.initialUrl} identity={identity} connections={connections} canUse={()=>isCurrent(identity)} onClose={()=>setTemplateDialog(null)} onCreated={()=>{void queryClient.invalidateQueries({queryKey:['agents',identity]});void queryClient.invalidateQueries({queryKey:['dashboard']})}} />}
      {duplicateAgent && <BotDuplicateDialog key={`${identity}:${duplicateAgent.id}`} agent={duplicateAgent} identity={identity} canUse={()=>isCurrent(identity)} onClose={()=>setDuplicateAgent(null)} onCreated={()=>{void queryClient.invalidateQueries({queryKey:['agents',identity]});void queryClient.invalidateQueries({queryKey:['dashboard']})}} />}

      {memoryAgent && <AgentMemoryDialog key={`${identity}:${memoryAgent.id}`} agentId={memoryAgent.id} name={memoryAgent.name} onClose={() => { if (isCurrent(identity)) setMemoryAgent(null) }} />}
      {removingAgent && <AgentRemoveDialog target={removingAgent}
        current={agents.find(item => item.id === removingAgent.id)}
        connectionLabel={connectionStatus === 'ready' ? connections.find(item => item.id === removingAgent.connection_id)?.label : undefined}
        ready={agentsQuery.isSuccess && !agentsQuery.isFetching} blocked={blocked} pending={saving} canUse={() => isCurrent(identity)}
        onClose={() => { if (isCurrent(identity)) setRemovingAgent(null) }}
        onAdopt={() => {
          if (!isCurrent(identity)) return
          const state = queryClient.getQueryState(['agents', identity])
          const current = queryClient.getQueryData<Agent[]>(['agents', identity])?.find(item => item.id === removingAgent.id)
          if (state?.status === 'success' && state.fetchStatus === 'idle' && current?.is_active && current.user_id === useAuthStore.getState().user?.id) setRemovingAgent(removalTarget(current))
        }} onConfirm={confirmRemoval}/>} 
      {/* Create/Edit Modal */}
      {(showCreateModal || (editingAgent && editingAgent.user_id === useAuthStore.getState().user?.id)) && (
        <AgentModal key={`${identity}:${editingAgent?.id ?? 'new'}`}
          agent={workspace.drafts.get(draftKey(editingAgent?.id))?.baseline ?? editingAgent}
          currentAgent={editingAgent ? agents.find(item => item.id === editingAgent.id) || null : null}
          draft={workspace.drafts.get(draftKey(editingAgent?.id))?.value}
          agentsFetching={agentsQuery.isFetching}
          agentsReadError={agentsQuery.isError}
          agentsReceivedAt={agentsQuery.dataUpdatedAt}
          onReadAgents={readAgents}
          onKeepDraft={(value) => { if (!isCurrent(identity)) return
            const sent = readAgentWorkspace(identity).commands.find(command => command.id === submittedHere.current)
            if (sent?.phase === 'succeeded' && JSON.stringify(value) === JSON.stringify(sent.draft)) return
            const baseline = workspace.drafts.get(draftKey(editingAgent?.id))?.baseline ?? editingAgent
            keepAgentDraft(identity, generation, editingAgent?.id, value, baseline, defaultAgentDraft(baseline))
          }}
          onDiscardDraft={() => { if (isCurrent(identity)) { discardAgentDraft(identity, generation, editingAgent?.id); if (editingAgent) setEditingAgent(agents.find(item => item.id === editingAgent.id) || editingAgent) } }}
          busy={saving}
          blocked={blocked}
          onInspect={inspectCommand}
          connections={connections}
          connectionStatus={connectionStatus}
          connectionsFetching={connectionsQuery.isFetching}
          connectionsReceivedAt={connectionsQuery.dataUpdatedAt}
          onRetryConnections={retryConnections}
          t={t}
          onClose={() => { setShowCreateModal(false); setEditingAgent(null) }}
          onSubmit={(data, draft) => {
            if (!isCurrent(identity)) return
            const currentConnections = queryClient.getQueryState<Connection[]>(['connections', identity])
            if (currentConnections?.status !== 'success' || currentConnections.fetchStatus !== 'idle') return
            const currentList = queryClient.getQueryState<Agent[]>(['agents', identity])
            if (editingAgent && (currentList?.status !== 'success' || currentList.fetchStatus !== 'idle' || !currentList.data?.some(item => item.id === editingAgent.id && item.is_active && item.user_id === useAuthStore.getState().user?.id))) return
            const original = beginAgentCommand(identity, generation, { kind: editingAgent ? 'update' : 'create', target: { id: editingAgent?.id, name: data.name }, body: data, draft, snapshotVersion: agentsQuery.dataUpdatedAt })
            if (original) { submittedHere.current = original.id; void sendCommand(original) }
          }}
        />
      )}
    </AppShell>
  )
}

function defaultAgentDraft(agent: Agent | null): AgentDraft {
  return { avatar: agent?.avatar, name: agent?.name || '', description: agent?.description || '', systemPrompt: agent?.system_prompt || '',
    connectionId: agent?.connection_id || 0, modelId: agent?.model_id || agent?.model || '', temperature: agent?.temperature || 'auto', maxTokens: agent?.max_tokens ?? 0 }
}

function AgentModal({
  agent,
  currentAgent,
  draft,
  agentsFetching,
  agentsReadError,
  agentsReceivedAt,
  onReadAgents,
  onKeepDraft,
  onDiscardDraft,
  busy,
  blocked,
  onInspect,
  connections,
  connectionStatus,
  connectionsFetching,
  connectionsReceivedAt,
  onRetryConnections,
  t,
  onClose,
  onSubmit,
}: {
  agent: Agent | null
  currentAgent: Agent | null
  agentsFetching: boolean
  agentsReadError: boolean
  agentsReceivedAt: number
  onReadAgents: () => void
  draft?: AgentDraft
  onKeepDraft: (value: AgentDraft) => void
  onDiscardDraft: () => void
  busy: boolean
  blocked: boolean
  onInspect: (command: AgentCommand) => Promise<Agent[]>
  connections: Connection[]
  connectionStatus: 'error' | 'loading' | 'ready'
  connectionsFetching: boolean
  connectionsReceivedAt: number
  onRetryConnections: () => void
  t: ReturnType<typeof useI18n>['t']
  onClose: () => void
  onSubmit: (data: AgentInput, draft: AgentDraft) => void
}) {
  const {formatDateTime} = useI18n()
  const targetUnavailable = !!agent && (agentsFetching || agentsReadError || !currentAgent?.is_active)
  const initialDraft = draft ?? defaultAgentDraft(agent)
  const randomFace = useRef<AvatarConfig | null>(null)
  if (!randomFace.current) randomFace.current = randomAvatar()
  const [avatar, setAvatar] = useState<AvatarConfig | undefined>(initialDraft.avatar)
  const [name, setName] = useState(initialDraft.name)
  const [description, setDescription] = useState(initialDraft.description)
  const [systemPrompt, setSystemPrompt] = useState(initialDraft.systemPrompt)
  const [connectionId, setConnectionId] = useState(initialDraft.connectionId)
  const [modelId, setModelId] = useState(initialDraft.modelId)
  const [temperature, setTemperature] = useState(initialDraft.temperature)
  const [maxTokens, setMaxTokens] = useState(initialDraft.maxTokens)
  const [showErrors, setShowErrors] = useState(false)
  const nameLength = editorTextLength(name), modelLength = editorTextLength(modelId.trim())
  const dialogRef = useRef<HTMLDivElement>(null)
  const returnFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null)
  const focusGeneration = useRef(directoryViewGeneration()).current
  const closeRef = useRef(onClose)
  const [restoredDraft, setRestoredDraft] = useState(Boolean(draft))
  const basisChanged = !!draft && !!agent && (!currentAgent || JSON.stringify(defaultAgentDraft(agent)) !== JSON.stringify(defaultAgentDraft(currentAgent)))
  const currentBasis = JSON.stringify(currentAgent ? defaultAgentDraft(currentAgent) : null)
  const [basisAcknowledged, setBasisAcknowledged] = useState(false)
  useEffect(() => { setBasisAcknowledged(false) }, [currentBasis, agentsReceivedAt, name, description, systemPrompt, connectionId, modelId, temperature, maxTokens, avatar])
  const selectedConnection = connections.find((connection) => connection.id === connectionId)
  const ownerIdentity = useAuthStore((state) => `${state.user?.id || 0}:${state.epoch}`)
  const identity = useRef(ownerIdentity).current
  const imeGuard = useImeFormGuard(`${identity}:agent:${agent?.id ?? 'new'}`)
  const keepDraft = useRef(onKeepDraft)
  keepDraft.current = onKeepDraft
  useLayoutEffect(() => { keepDraft.current({ name, description, systemPrompt, connectionId, modelId, temperature, maxTokens, avatar }) }, [name, description, systemPrompt, connectionId, modelId, temperature, maxTokens, avatar])
  const closeEditor = () => {
    if (ownerIdentity !== identity) return
    onKeepDraft({ name, description, systemPrompt, connectionId, modelId, temperature, maxTokens, avatar })
    onClose()
  }
  closeRef.current = closeEditor
  const discardDraft = () => {
    if (busy || ownerIdentity !== identity) return
    const initial = defaultAgentDraft(currentAgent || agent)
    setAvatar(initial.avatar); setName(initial.name); setDescription(initial.description); setSystemPrompt(initial.systemPrompt)
    setConnectionId(initial.connectionId); setModelId(initial.modelId); setTemperature(initial.temperature); setMaxTokens(initial.maxTokens)
    setRestoredDraft(false); onDiscardDraft(); focusEditorField(dialogRef.current, 'name')
  }
  const [loadedFeatureScope, setLoadedFeatureScope] = useState<string | null>(null)
  const featureScope = `${identity}:${connectionId}:${modelId.trim()}:${selectedConnection?.config_revision ?? 0}:${selectedConnection?.auth_grant_revision ?? 0}`

  useDialogFocus(dialogRef, () => closeRef.current(), { restoreFocus: false })
  useEffect(() => () => {
    const owner = useAuthStore.getState(), target = returnFocus.current
    const modal = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).some(node => node.getClientRects().length > 0
      && !node.closest('[hidden], [inert], [aria-hidden="true"]'))
    if (owner.token && identity === `${owner.user?.id || 0}:${owner.epoch}` && focusGeneration === directoryViewGeneration()
      && !modal && document.visibilityState === 'visible' && document.hasFocus() && target?.isConnected
      && target.getClientRects().length > 0 && !target.matches(':disabled') && !target.closest('[hidden], [inert], [aria-hidden="true"]')) target.focus({ preventScroll: true })
  }, [])

  const errors = {
    name: !name.trim() ? 'editor.requiredField' : nameLength > 100 ? 'editor.nameLimit' : null,
    description: !description.trim() ? 'editor.requiredField' : null,
    connection: !connectionId || !selectedConnection ? 'agents.chooseConnection' : null,
    model: !validEditorModelId(modelId.trim()) ? 'editor.modelFormat' : null,
    temperature: !validEditorTemperature(temperature) ? 'editor.temperatureFormat' : null,
    tokens: !Number.isInteger(maxTokens) || maxTokens < 0 || maxTokens > 100000 ? 'editor.tokensFormat' : null,
  } as const
  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (imeGuard.isSubmitBlocked(e) || busy || blocked || targetUnavailable || basisChanged && !basisAcknowledged || connectionsFetching || connectionStatus !== 'ready') return
    const invalid = (Object.keys(errors) as Array<keyof typeof errors>).find(key => errors[key])
    if (invalid) { setShowErrors(true); focusEditorField(dialogRef.current, invalid); return }
    onSubmit({
      name,
      description,
      system_prompt: systemPrompt.trim() ? systemPrompt : description,
      connection_id: connectionId,
      model_id: modelId.trim(),
      temperature,
      max_tokens: maxTokens,
      avatar: avatar ?? randomFace.current!,
    }, { name, description, systemPrompt, connectionId, modelId, temperature, maxTokens, avatar })
  }

  return (
    <div className="modal-backdrop">
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="agent-dialog-title" className="modal-panel">
          <h2 className="page-title">
            <span id="agent-dialog-title">
            {t(agent ? 'agents.edit' : 'agents.create')}
            </span>
          </h2>

          <p className="sr-only">{t('editor.keyboardHelp')}</p>
          <form {...imeGuard.handlers} onSubmit={handleSubmit} noValidate className="form-grid" aria-busy={busy}>
            {showErrors && Object.values(errors).some(Boolean) && <p role="alert" className="notice notice--warning">{t('editor.inputIssues')}</p>}
            <EditorDraftNotice restored={restoredDraft} busy={busy} revision={JSON.stringify({name, description, systemPrompt, connectionId, modelId, temperature, maxTokens, avatar, currentBasis, agentsReceivedAt})} onDiscard={discardDraft} />
            <AgentCommandHistory identity={identity} onInspect={onInspect}/>
            {blocked && !busy && <p role="alert" className="notice notice--warning">{t('agents.workspace.blocked')}</p>}
            {agent && <section className="notice notice--info stack stack--tight" aria-label={t('agents.readTarget.title')}>
              <button type="button" className="button button--secondary button--compact" disabled={busy || agentsFetching} onClick={() => {if (ownerIdentity === identity && !busy) onReadAgents()}}>{t('agents.readTarget.button')}</button>
              <p className="field-hint">{t('agents.readTarget.scope')}</p>
              {agentsFetching && <p role="status">{t('common.loading')}</p>}
              {agentsReadError && <p role="alert">{t('agents.readTarget.error')}</p>}
              {!agentsFetching && !agentsReadError && !currentAgent?.is_active && <p role="alert">{t('agents.workspace.missingTarget')}</p>}
              {agentsReceivedAt > 0 && <p className="metadata">{t('agents.readTarget.received', {time: formatDateTime(new Date(agentsReceivedAt).toISOString(), {dateStyle: 'medium', timeStyle: 'short'})})}</p>}
            </section>}
            {basisChanged && <details className="notice notice--warning compact-details"><summary>{t('agents.workspace.changedBasis')}</summary><p>{t('agents.workspace.basisHelp')}</p><AgentDraftComparison baseline={defaultAgentDraft(agent)} received={currentAgent ? defaultAgentDraft(currentAgent) : null} draft={{name, description, systemPrompt, connectionId, modelId, temperature, maxTokens, avatar}}/><label className="checkbox-row"><input type="checkbox" checked={basisAcknowledged} disabled={busy} onChange={event => setBasisAcknowledged(event.target.checked)}/>{t('agents.workspace.basisAcknowledge')}</label></details>}
            {connectionStatus==='error'&&<div style={{gridColumn:'1 / -1'}}><ListLoadNotice failed fetching={connectionsFetching||busy} hasSnapshot={connections.length>0} message={t('agents.connectionLoadFailure')} onRetry={()=>{if(ownerIdentity===identity&&!busy)onRetryConnections()}}/><p className="field-hint">{t('agents.retryKeepsInput')}</p></div>}
            <fieldset disabled={busy} className="form-grid" style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
            {connectionStatus === 'ready' && connections.length === 0 && <p className="notice notice--warning"><Link to="/connections" className="text-link">{t('agents.saveConnectionFirst')}</Link></p>}
            {connectionStatus === 'loading' && <p role="status" className="notice notice--warning">{t('common.loading')} · {t('agents.bindingLocked')}</p>}
            <AvatarEditor value={avatar ?? randomFace.current!} onChange={setAvatar} disabled={busy || blocked || targetUnavailable}/>
            <label className="field">
                {t('agents.name')}
              <input
                data-dialog-initial data-editor-field="name" aria-invalid={Boolean(errors.name && (showErrors || nameLength > 100))} aria-describedby="agent-name-help"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
              <span id="agent-name-help" className={errors.name && (showErrors || nameLength > 100) ? 'metadata metadata--danger' : 'sr-only'}>{t(showErrors && errors.name ? errors.name : 'editor.nameLimit', { count: nameLength })}</span>
            </label>

            <label className="field">
                {t('agents.description')}
              <textarea
                data-editor-field="description" aria-invalid={showErrors && Boolean(errors.description)} aria-describedby={showErrors && errors.description ? 'agent-description-error' : undefined}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={2}
                required
              />
              {showErrors && errors.description && <span id="agent-description-error" className="metadata metadata--danger">{t(errors.description)}</span>}
            </label>

            <details className="compact-details"><summary>{t('agents.systemPrompt')}</summary><div className="form-grid">
              <p className="field-hint">{t('agents.defaultPromptHelp')}</p>
              <label className="field">{t('agents.systemPrompt')}<textarea value={systemPrompt} onChange={(e) => setSystemPrompt(e.target.value)} rows={6} /></label>
            </div></details>

            <div className="stack stack--tight">
              {connectionStatus !== 'error' && <button type="button" className="button button--secondary button--compact" disabled={busy || connectionsFetching} onClick={() => {if (ownerIdentity === identity && !busy) onRetryConnections()}}>{t(connectionsFetching ? 'common.loading' : 'agents.retryConnections')}</button>}
              <p className="field-hint">{t('agents.connectionRead.scope')}</p>
              {connectionsReceivedAt > 0 && <p className="metadata">{t('agents.connectionRead.received', {time: formatDateTime(new Date(connectionsReceivedAt).toISOString(), {dateStyle: 'medium', timeStyle: 'short'})})}</p>}
              {connectionId > 0 && !selectedConnection && <p className="notice notice--warning" role="status">{t('agents.savedConnectionNumber', {id: String(connectionId)})} · {t('agents.connectionFilter.unlisted')} · {t('agents.connectionRead.missing')}</p>}
            </div>
            <div className="form-grid form-grid--two">
                <label htmlFor="agent-connection" className="field">
                  {t('agents.savedConnection')}
                <select
                  id="agent-connection" data-editor-field="connection" aria-invalid={showErrors && Boolean(errors.connection)} aria-describedby={showErrors && errors.connection ? 'agent-connection-error' : undefined}
                  value={connectionId}
                  disabled={connectionStatus !== 'ready'}
                  onChange={(e) => {
                    const id = Number(e.target.value)
                    setConnectionId(id)
                    setModelId('')
                  }}
                  required
                >
                  <option value={0}>{t('agents.chooseConnection')}</option>
                  {connectionId > 0 && !connections.some(row => row.id === connectionId) && <option value={connectionId}>{t('agents.savedConnectionNumber', {id: String(connectionId)})} · {t('agents.connectionFilter.unlisted')}</option>}
                  {connections.map((connection) => <option key={connection.id} value={connection.id}>
                    {connection.label}{connection.enabled ? '' : ` · ${t('connections.disabled')}`}
                  </option>)}
                </select>
                {showErrors && errors.connection && <span id="agent-connection-error" className="metadata metadata--danger">{t(errors.connection)}</span>}
                </label>

                <div data-editor-field="model">{selectedConnection && connectionStatus === 'ready' ? <SavedModelPicker
                  connection={selectedConnection} identity={identity} value={modelId} onChange={setModelId}
                  manualLabel={t('agents.exactModel')} invalid={showErrors && Boolean(errors.model)} describedBy="agent-model-help" /> : <label htmlFor="agent-model" className="field">
                  {t('agents.exactModel')}<input id="agent-model" aria-invalid={showErrors && Boolean(errors.model)} aria-describedby="agent-model-help" value={modelId} onChange={e=>setModelId(e.target.value)} required />
                </label>}
                <p id="agent-model-help" className={showErrors && errors.model ? 'metadata metadata--danger' : 'sr-only'}>{t(showErrors && errors.model ? errors.model : 'editor.modelLimit', { count: modelLength })}</p></div>
            </div>
            {selectedConnection && <div className="notice notice--info stack stack--tight">
              <p>{t('agents.selectedConnection', { id: String(selectedConnection.id), provider: selectedConnection.label, credential: t(!selectedConnection.auth_required ? 'agents.credentialNotRequired' : selectedConnection.has_secret ? 'agents.credentialSaved' : 'agents.credentialMissing') })}</p>
              {modelId.trim() && <p>{t('localModels.reportForModel', { model: modelId.trim() })}</p>}
              <button type="button" className="text-link" onClick={() => setModelId(selectedConnection.default_model_id)}>{t('agents.defaultModel', { model: selectedConnection.default_model_id })}</button>
            </div>}

            {selectedConnection && connectionStatus === 'ready' && modelId.trim() && <details
              key={`feature-details:${featureScope}`} className="compact-details"
              onToggle={event=>{if(event.currentTarget.open)setLoadedFeatureScope(featureScope)}}>
              <summary>{t('localModels.supportedFeatures')}</summary>
              {loadedFeatureScope===featureScope&&<ModelFeatures key={featureScope}
                identity={identity} connectionId={selectedConnection.id} modelId={modelId.trim()}
                revision={`${selectedConnection.config_revision??0}:${selectedConnection.auth_grant_revision??0}`}
                enabled={selectedConnection.enabled}/>}
            </details>}

            <details className="compact-details"><summary>{t('common.technicalDetails')}</summary><div className="form-grid">
            <label className="field">{t('connections.cap.temperature')} <span className="field-hint">{t('agents.temperatureHelp')}</span>
              <input data-editor-field="temperature" aria-invalid={showErrors && Boolean(errors.temperature)} aria-describedby="agent-temperature-help" value={temperature} onChange={(e) => setTemperature(e.target.value)}
                required />
              {showErrors && errors.temperature && <span id="agent-temperature-help" className="metadata metadata--danger">{t(errors.temperature)}</span>}
            </label>

              <label className="field">
                {t('connections.cap.maxTokens')} <span className="field-hint">{t('agents.maxTokensHelp')}</span>
              <input
                data-editor-field="tokens" aria-invalid={showErrors && Boolean(errors.tokens)} aria-describedby={showErrors && errors.tokens ? 'agent-tokens-error' : undefined}
                type="number"
                min="0"
                max="100000"
                value={maxTokens}
                onChange={(e) => setMaxTokens(Number(e.target.value))}
                required
              />
                {showErrors && errors.tokens && <span id="agent-tokens-error" className="metadata metadata--danger">{t(errors.tokens)}</span>}
              </label>
            </div></details>

            </fieldset>
            <div className="page-actions">
              <button
                type="button"
                onClick={closeEditor}
                className="button button--secondary"
              >
                {t(busy ? 'common.close' : 'common.cancel')}
              </button>
              <button
                type="submit"
                disabled={busy || blocked || targetUnavailable || basisChanged && !basisAcknowledged || !connectionId || connectionsFetching || connectionStatus !== 'ready'}
                className="button button--primary"
              >
                {t(busy ? 'common.saving' : agent ? 'common.edit' : 'common.create')}
              </button>
            </div>
          </form>
      </div>
    </div>
  )
}
