import KeyboardReadingGuide from '../components/KeyboardReadingGuide'
import LocalPermissionGuide from '../components/LocalPermissionGuide'
import GuiBuildInfo from '../components/GuiBuildInfo'
import ServerTime from '../components/ServerTime'
import { directoryMatcher } from '../components/DirectorySearch'
import { useDirectoryValue } from '../lib/useDirectoryView'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { dashboardApi, type DashboardNextAction, type DashboardRoom } from '../api/dashboard'
import { botDirectoryApi } from '../api/botDirectory'
import { useAuthStore } from '../stores/authStore'
import api from '../lib/api'
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { AlertCircle, Bot, Cable, ChevronRight, CircleHelp, Folder, MessageSquare, LogOut, Plus, RefreshCw, Search, UserRound, X } from 'lucide-react'
import tauriConfig from '../../src-tauri/tauri.conf.json'
import { AppShell, Brand, PageHeader } from '../components/ui'
import { useI18n } from '../i18n'
import FirstSuccessGuide from '../components/FirstSuccessGuide'
import PrimaryAgent from '../components/PrimaryAgent'
import { readStarterWorkspace, starterHasWork, subscribeStarterWorkspace } from '../lib/starterWorkspace'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { editorSaveFailure } from '../components/EditorSaveNotice'
import { assistantVisibleMarkdown } from '../lib/assistantContent'

export default function Dashboard() {
  const owner = useAuthStore(state => state.user?.id ?? 0), epoch = useAuthStore(state => state.epoch)
  return <DashboardView key={`${owner}:${epoch}`} />
}
function DashboardView() {
  const { user, logout, epoch: authEpoch } = useAuthStore()
  const [logoutPhase, setLogoutPhase] = useState<'idle' | 'sending' | 'rejected' | 'unknown'>('idle')
  const logoutLock = useRef(false), mounted = useRef(false)
  const [generation] = useState(directoryViewGeneration)
  const sameOwner = () => { const current = useAuthStore.getState(); return !!current.token && current.user?.id === user?.id
    && current.epoch === authEpoch && directoryViewGeneration() === generation }
  const [showStarter, setShowStarter] = useState(false)
  const roomFinder = useRef<HTMLInputElement>(null)
  const searchComposing = useRef(false), searchImeUntil = useRef(0)
  const navigate = useNavigate()
  const identity = `${user?.id ?? 0}:${authEpoch}`
  const [roomQuery, setRoomQuery] = useDirectoryValue<string>(identity, 'dashboard:room-query', '')
  const [savedRoomScope, setRoomScope] = useDirectoryValue<string>(identity, 'dashboard:room-scope', 'all')
  const roomScope = ['all', 'unread', 'attention', 'working'].includes(savedRoomScope) ? savedRoomScope : 'all'
  const changeRoomQuery = (value: string) => { if (sameOwner()) setRoomQuery(value) }
  const changeRoomScope = (value: string) => { if (sameOwner()) setRoomScope(value) }
  const resetRoomFinder = () => { if (mounted.current && sameOwner()) { setRoomQuery(''); setRoomScope('all'); roomFinder.current?.focus() } }

  const starter = useSyncExternalStore(subscribeStarterWorkspace, () => readStarterWorkspace(`${user?.id ?? 0}:${authEpoch}`))
  const { t, formatDateTime, formatNumber } = useI18n()
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const leave = async () => {
    if (logoutLock.current || !mounted.current || !sameOwner()) return
    logoutLock.current = true; setLogoutPhase('sending')
    try { await api.post('/auth/logout'); if (sameOwner()) logout() }
    catch (failure) { if (mounted.current && sameOwner()) setLogoutPhase(editorSaveFailure(failure) === 'unknown' ? 'unknown' : 'rejected') }
    finally { logoutLock.current = false }
  }
  const dashboard = useQuery({
    queryKey: ['dashboard', user?.id, authEpoch], enabled: !!user, retry: false,
    refetchInterval: 2000, refetchIntervalInBackground: false, refetchOnWindowFocus: true,
    queryFn: async () => {
      if (!sameOwner()) throw new Error('Owner changed')
      const summary = await dashboardApi.get(12)
      if (!sameOwner()) throw new Error('Owner changed')
      return summary
    },
  })
  const directory = useQuery({queryKey:['bot-directory',identity],enabled:!!user,retry:false,refetchOnWindowFocus:true,queryFn:async()=>{const value=await botDirectoryApi.read();if(!sameOwner())throw Error('Owner changed');return value}})
  const botItem=(room:DashboardRoom)=>Number.isSafeInteger(room.personal_agent_id) && (room.personal_agent_id||0)>0 ? directory.data?.items.find(item=>item.agent_id===room.personal_agent_id) : undefined
  const setupAction = dashboard.data?.setup.next_action
  const hasRooms = (dashboard.data?.counts.active_rooms || 0) > 0
  const setup: Record<Exclude<DashboardNextAction, null>, { path: string; title: Parameters<typeof t>[0]; help: Parameters<typeof t>[0] }> = {
    connection: { path: '/connections', title: 'dashboard.setup.connection', help: 'dashboard.setup.connectionHelp' },
    agent: { path: '/agents', title: 'dashboard.setup.agent', help: 'dashboard.setup.agentHelp' },
    project: { path: '/projects', title: 'dashboard.setup.project', help: 'dashboard.setup.projectHelp' },
    room: { path: '/projects', title: 'dashboard.setup.room', help: 'dashboard.setup.roomHelp' },
  }
  const rooms = dashboard.data?.rooms || []
  const needsAttention = (room: DashboardRoom) => room.waiting_approvals > 0 || room.active_failures > 0
  const activeWork = (room: DashboardRoom) => !needsAttention(room) && ['running', 'waiting'].includes(room.work_status)
  const matchesRoom = directoryMatcher(roomQuery)
  const previewText = (room: DashboardRoom) => room.last_authored_preview ? assistantVisibleMarkdown(room.last_authored_preview).trim() : ''
  const filteredRooms = rooms.filter(room => (!botItem(room)?.hidden || needsAttention(room) || activeWork(room) || room.unread_authored>0) && matchesRoom([room.room_title, room.project_name, String(room.room_id), `#${room.room_id}`, previewText(room)], room.room_id)
    && (roomScope === 'unread' ? room.unread_authored > 0 : roomScope === 'attention' ? needsAttention(room)
      : roomScope === 'working' ? ['running', 'waiting'].includes(room.work_status) : true))
  const finderActive = Boolean(roomQuery.trim()) || roomScope !== 'all'

  const pinned=(room:DashboardRoom)=>!!botItem(room)?.pinned && !botItem(room)?.hidden
  const groups = [
    { title: t('botDirectory.pinned'), rooms: filteredRooms.filter(pinned) },
    { title: t('dashboard.needsYourInput'), rooms: filteredRooms.filter(room=>!pinned(room)&&needsAttention(room)) },
    { title: t('dashboard.workingNow'), rooms: filteredRooms.filter(room=>!pinned(room)&&activeWork(room)) },
    { title: t('dashboard.recentWork'), rooms: filteredRooms.filter((room) => !pinned(room)&&!needsAttention(room) && !activeWork(room)) },
  ]
  // Match the rendered group priority, rather than the API's ungrouped order.
  const firstRoom = groups.find(group => group.rooms.length)?.rooms[0]
  const openFirstRoom = () => {
    if (!mounted.current || !sameOwner() || document.visibilityState !== 'visible'
        || !roomQuery.trim() || !firstRoom || !Number.isSafeInteger(firstRoom.room_id) || firstRoom.room_id < 1) return
    navigate(`/chat/${firstRoom.room_id}`)
  }
  const roomCard = (room: DashboardRoom) => {
    const preview = previewText(room)
    const status = room.waiting_approvals > 0 ? t('dashboard.awaitingApproval', { count: room.waiting_approvals })
      : room.active_failures > 0 ? t('dashboard.status.attention') : t(`dashboard.status.${room.work_status}`)
    return <article className="dashboard-work dashboard-room-row" key={room.room_id}>
      <Link className="dashboard-work__body dashboard-room-row__link" to={`/chat/${room.room_id}`} aria-label={t('dashboard.openRoomNamed', { room: room.room_title })}>
        <div className="dashboard-work__title-row">
          <div><h3 className="section-title dashboard-room-title">{room.room_title}</h3><p className="metadata">{room.project_name}{botItem(room)?.hidden ? ` · ${t('botDirectory.hiddenActivity')}` : ''}</p></div>
          <span className={`dashboard-status dashboard-status--${room.active_failures > 0 ? 'attention' : room.work_status}`}>{status}</span>
        </div>
        {preview && <p className="dashboard-work__preview">{preview}</p>}
        <div className="dashboard-work__meta"><ServerTime value={room.last_activity_at} fallback={t('projects.activityUnavailable')} />
          {room.unread_authored > 0 && <span className="dashboard-unread">{t('dashboard.unread', { count: room.unread_authored })}</span>}
          {room.waiting_approvals > 0 && room.active_failures > 0 && <span className="dashboard-status--attention">{t('dashboard.status.attention')}</span>}
        </div>
        <ChevronRight className="dashboard-room-row__open" size={18} aria-hidden="true" />
      </Link>
    </article>
  }
  return <AppShell>
    <PageHeader className="dashboard-header" title={<Brand />} actions={<details className="dashboard-account-menu">
      <summary className="button button--ghost" aria-label={`${t('dashboard.accountMenu')}${user?.username ? ` · ${user.username}` : ''}`}><UserRound size={17} aria-hidden="true" /><span className="dashboard-account-menu__name">{user?.username || t('dashboard.accountMenu')}</span></summary>
      <div className="dashboard-account-menu__items"><button type="button" disabled={logoutPhase === 'sending'} onClick={() => { void leave() }} className="button button--ghost"><LogOut aria-hidden="true" size={16} /><span>{t(logoutPhase === 'sending' ? 'dashboard.logoutSending' : 'dashboard.logout')}</span></button></div>
    </details>} />
    <main className="page-frame page-frame--wide page-content stack dashboard-home dashboard-home--focused">
      {(logoutPhase === 'unknown' || logoutPhase === 'rejected') && <p role="alert" className="notice notice--danger">{t(logoutPhase === 'unknown' ? 'dashboard.logoutUnknown' : 'dashboard.logoutFailure')}</p>}
      {directory.data?.items.some(item=>item.hidden) && <Link className="text-link" to="/agents?hidden=1">{t('botDirectory.showHidden')}</Link>}
      <section className="dashboard-home__heading dashboard-home__toolbar page-heading">
        <h1 className="page-title">{t('dashboard.conversationTitle')}</h1>
        <div className="page-actions">
          {dashboard.data && <button type="button" className="icon-button" title={t('dashboard.readAgain')} aria-label={t('dashboard.readAgain')} disabled={dashboard.isFetching} onClick={() => { void dashboard.refetch({ cancelRefetch: false }) }}><RefreshCw size={17} aria-hidden="true" /></button>}
          {dashboard.data && (hasRooms ? <Link className="button button--primary" to="/projects"><Plus size={18} aria-hidden="true" />{t('dashboard.newConversation')}</Link>
            : !showStarter && <button className="button button--primary" onClick={() => setShowStarter(true)}><Plus size={18} aria-hidden="true" />{t('dashboard.newConversation')}</button>)}
        </div>
      </section>
      <PrimaryAgent />
      {dashboard.isPending && <p role="status" className="section-copy">{t('dashboard.loadingActivity')}</p>}
      {dashboard.isError && <section className="notice notice--danger dashboard-state dashboard-state--error inline-feedback" role="alert">
        <AlertCircle aria-hidden="true" size={20} /><div><h2 className="section-title">{t('dashboard.activityUnavailable')}</h2>
          <details className="compact-details"><summary aria-label={t('dashboard.listDetails')} title={t('dashboard.listDetails')}><CircleHelp size={16} aria-hidden="true"/></summary><p className="section-copy">{t('dashboard.activityUnavailableHelp')}</p>{dashboard.data && <p className="field-hint">{t('dashboard.retainedActivity')}</p>}</details>
        </div>
        <button type="button" className="button button--secondary" disabled={dashboard.isFetching} onClick={() => { void dashboard.refetch({ cancelRefetch: false }) }}>{t(dashboard.isFetching ? 'dashboard.readingAgain' : 'dashboard.readAgain')}</button>
      </section>}
      {showStarter && <section className="dashboard-starter" aria-label={t('firstSuccess.title')}>
        <div className="page-actions"><button type="button" className="button button--ghost button--compact" onClick={() => setShowStarter(false)}><X size={16} aria-hidden="true" />{t('common.close')}</button></div>
        <FirstSuccessGuide activeRoomCount={dashboard.data?.counts.active_rooms || 0} />
      </section>}
      {directory.isError && <p role="alert" className="notice notice--warning">{t('botDirectory.readFailed')} <button className="button button--secondary" onClick={()=>void directory.refetch()}>{t('common.retry')}</button></p>}
      {dashboard.data && !directory.isPending && <>
        {(rooms.length > 0 || finderActive) && <section className="dashboard-directory page-tools" aria-label={t('dashboard.searchConversations')}>
          <div className="dashboard-directory__search">
            <Search size={18} aria-hidden="true" />
            <label className="sr-only" htmlFor="dashboard-conversation-search">{t('dashboard.searchConversations')}</label>
            <input id="dashboard-conversation-search" ref={roomFinder} type="search" value={roomQuery} placeholder={t('dashboard.searchConversations')} aria-controls="dashboard-received-rooms" data-dialog-search-clear
              onChange={event => changeRoomQuery(event.target.value)}
              onCompositionStart={() => { searchComposing.current = true; searchImeUntil.current = 0 }}
              onCompositionEnd={() => { searchComposing.current = false; searchImeUntil.current = performance.now() + 120 }}
              onBlur={() => { searchComposing.current = false; searchImeUntil.current = 0 }}
              onKeyDown={event => {
                const composing = searchComposing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229
                if (composing) searchImeUntil.current = performance.now() + 120
                if (composing || performance.now() < searchImeUntil.current) { if (['Enter', 'Escape'].includes(event.key)) event.stopPropagation(); return }
                if (event.key === 'Enter' && !event.shiftKey && !event.altKey && !event.ctrlKey && !event.metaKey) { event.preventDefault(); event.stopPropagation(); if (!event.repeat) openFirstRoom() }
                if (event.key === 'Escape' && roomQuery) { event.preventDefault(); event.stopPropagation(); changeRoomQuery('') }
              }} />
            {roomQuery && <button type="button" className="icon-button" aria-label={t('directory.clear')} onClick={() => { changeRoomQuery(''); roomFinder.current?.focus() }}><X size={16} aria-hidden="true" /></button>}
          </div>
          <div role="group" aria-label={t('dashboard.find.filter')} className="dashboard-filter-tabs button-group">
            {(['all', 'unread', 'attention', 'working'] as const).map(scope => <button key={scope} type="button" className={`button button--compact ${roomScope === scope ? 'button--secondary is-selected' : 'button--ghost'}`}
              aria-pressed={roomScope === scope} onClick={() => changeRoomScope(scope)}>{t(({ all: 'dashboard.filterAll', unread: 'dashboard.filterUnread', attention: 'dashboard.filterAttention', working: 'dashboard.filterWorking' } as const)[scope])}</button>)}
          </div>
          <p role="status" className="sr-only">{t('directory.matches', { shown: filteredRooms.length, total: rooms.length })}</p>
        </section>}
        {rooms.length > 0 && !filteredRooms.length && <div role="status" className="dashboard-empty-search"><p className="section-copy">{t('dashboard.find.noMatch')}</p>{finderActive && <button type="button" className="button button--secondary button--compact" onClick={resetRoomFinder}>{t('dashboard.find.reset')}</button>}</div>}
        <div id="dashboard-received-rooms" className="stack dashboard-room-list">
          {groups.filter(group => group.rooms.length).map(group => <section className="stack dashboard-home__group" key={group.title} aria-label={group.title}>
            <h2 className="section-title">{group.title}</h2><div className="dashboard-work-list">{group.rooms.map(roomCard)}</div>
          </section>)}
        </div>
        {!rooms.length && !showStarter && <section className="dashboard-empty-state">
          <MessageSquare size={28} aria-hidden="true" /><h2 className="section-title">{t(dashboard.isError ? 'dashboard.lastListEmpty' : 'dashboard.emptyTitle')}</h2>
          {!dashboard.isError && <p className="sr-only">{t('dashboard.emptyHelp')}</p>}
        </section>}
        {rooms.length > 0 && <div className="dashboard-home__more"><Link className="text-link" to="/projects">{t('dashboard.allConversations')}<ChevronRight size={15} aria-hidden="true" /></Link></div>}
      </>}
      <details className="dashboard-home__utilities"><summary>{t('dashboard.moreOptions')}</summary><div className="stack">
        {!showStarter && starterHasWork(starter) && <div><button type="button" className="button button--secondary" onClick={() => setShowStarter(true)}>{t('firstSuccess.resumeSession')}</button></div>}
        {dashboard.data && <details className="compact-details"><summary aria-label={t('dashboard.listDetails')} title={t('dashboard.listDetails')}><CircleHelp size={16} aria-hidden="true"/></summary><div className="stack stack--tight">
          <p className="metadata">{t('dashboard.lastReceived')} <time dateTime={new Date(dashboard.dataUpdatedAt).toISOString()}>{formatDateTime(dashboard.dataUpdatedAt, { dateStyle: 'short', timeStyle: 'medium' })}</time></p>
          <p className="field-hint">{t('dashboard.receivedActivityHelp')}</p><p className="field-hint">{t('dashboard.recentLimit')}</p><p className="field-hint">{t('dashboard.find.help')}</p><p className="field-hint">{t('directory.exactIdTitle')} · <kbd>id:12</kbd> — {t('directory.exactIdHelp')}</p>
          <p className="metadata">{t('directory.matches', { shown: filteredRooms.length, total: rooms.length })}</p>
        </div></details>}
        {hasRooms && setupAction && !dashboard.isError && <aside className="notice notice--info dashboard-home__setup"><div><strong>{t(setup[setupAction].title)}</strong><p className="section-copy">{t(setup[setupAction].help)}</p></div><Link className="button button--secondary" to={setup[setupAction].path}>{t(setup[setupAction].title)}</Link></aside>}
        <section className="stat-grid" aria-label={t('dashboard.summary')}>
          <div className="surface stat"><div><p className="metadata">{t('dashboard.readyAgents')}</p><p className="stat__value">{dashboard.data ? formatNumber(dashboard.data.counts.ready_agents) : '—'}</p></div><Bot color="var(--primary)" size={24} /></div>
          <div className="surface stat"><div><p className="metadata">{t('dashboard.readyProjects')}</p><p className="stat__value">{dashboard.data ? formatNumber(dashboard.data.counts.ready_projects) : '—'}</p></div><Folder color="var(--teal)" size={24} /></div>
          <div className="surface stat"><div><p className="metadata">{t('dashboard.conversations')}</p><p className="stat__value">{dashboard.data ? formatNumber(dashboard.data.counts.active_rooms) : '—'}</p></div><MessageSquare color="var(--primary)" size={24} /></div>
        </section>
        <section><h2 className="section-title">{t('dashboard.workspace')}</h2><div className="card-grid">
          <Link to="/agents" className="surface action-card"><Bot color="var(--primary)" size={24} /><h3 className="section-title">{t('dashboard.manageAgents')}</h3></Link>
          <Link to="/projects" className="surface action-card"><Folder color="var(--teal)" size={24} /><h3 className="section-title">{t('dashboard.manageProjects')}</h3></Link>
          <Link to="/connections" className="surface action-card"><Cable aria-hidden="true" color="var(--primary)" size={24} /><h3 className="section-title">{t('dashboard.manageConnections')}</h3></Link>
          <Link to="/reports" className="surface action-card"><CircleHelp aria-hidden="true" color="var(--teal)" size={24} /><h3 className="section-title">{t('dashboard.help')}</h3></Link>
        </div></section>
        <KeyboardReadingGuide /><LocalPermissionGuide key={`permissions:${identity}`} identity={identity}/><GuiBuildInfo version={tauriConfig.version} />
        <p className="metadata">{t('dashboard.version', { version: tauriConfig.version })} · {t('dashboard.manualDownloads')}</p>
      </div></details>
    </main>
  </AppShell>
}
