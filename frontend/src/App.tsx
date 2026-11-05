import { skipAutoRestoreOnStartup } from './lib/startupLoginMode'
import { clearSessionOnly, suppressAutomaticRestore } from './lib/sessionOnlyLogin'
import { VoiceCallPanel } from './components/VoiceCall'
import { voiceCallController } from './lib/voiceCallController'
import { Routes, Route, Navigate, Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuthStore } from './stores/authStore'
import { Component, lazy, Suspense, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { getLocalEndpoint } from './lib/localHost'
import api from './lib/api'
import { recordFrontendRenderFailure } from './lib/reporting'
import { useI18n } from './i18n'
import QuickRoomSwitcher from './components/QuickRoomSwitcher'
import LegacyAuthStorageNotice from './components/LegacyAuthStorageNotice'
import AttentionInbox from './components/AttentionInbox'
import KeyboardHelp from './components/KeyboardHelp'
import { AppHeaderFrame } from './components/AppHeaderFrame'
import AppMenu from './components/AppMenu'
import type { MessageKey } from './i18n/locales/en'
import { startBotNotifications, notificationHref } from './lib/botNotificationController'
import { botNotificationsApi, type NotificationItem } from './api/botNotifications'
import { NotificationClickNotice } from './components/NotificationClickNotice'
import { subscribeTemplateLinks } from './lib/templateLinks'
import { hasPendingTemplateImport, templatePreviewUrl } from './api/botTemplates'
import type { TemplateLinkRequest } from './pages/Agents'

const Teams = lazy(() => import('./pages/Teams'))

function autoRestoreNotice(error: unknown): MessageKey | '' {
  const failure = error as { config?: { url?: unknown }; response?: { status?: unknown; data?: { detail?: unknown } } }
  if (failure.config?.url !== '/auth/restore') return ''
  const { status, data } = failure.response ?? {}
  const detail = data?.detail
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return ''
  const fields = detail as { action?: unknown; code?: unknown; correlation_id?: unknown }
  if (status === 503
    && (fields.code === 'credential_locked' || fields.code === 'secret_store_unavailable')
    && typeof fields.correlation_id === 'string'
    && /^[0-9a-f]{32}$/.test(fields.correlation_id))
    return 'app.savedLoginRecovery'
  return status === 401 && fields.action === 'sign_in_again' ? 'app.savedLoginExpired' : ''
}

function ErrorFallback({ reportScreen, onReport, onRetry, onHome, toolName }: { reportScreen?: boolean; onReport?: () => void; onRetry?: () => void; onHome?: () => void; toolName?: string }) {
  const { t } = useI18n()
  if (toolName) return <div role="alert" className="header-control-recovery">
    <span>{t('app.controlFailure', { name: toolName })}</span>
    {onRetry && <button type="button" className="text-link" onClick={onRetry}>{t('app.retryControl', { name: toolName })}</button>}
    {onReport && <button type="button" className="text-link" onClick={onReport}>{t('app.reportProblem')}</button>}
  </div>
  return <main role="alert" className="page-frame page-frame--narrow page-content notice notice--danger">
    <p>{t('app.renderFailure')}</p>
    <div className="page-actions">
      {onRetry && <button type="button" className="button button--secondary" onClick={onRetry}>{t('app.retryScreen')}</button>}
      {onHome && <button type="button" className="text-link" onClick={onHome}>{t('app.goHome')}</button>}
    </div>
    <p className="field-hint">{t('app.retryScreenHelp')}</p>
    {reportScreen ? <p>{t('app.reportsUnavailable')}</p>
      : onReport ? <button onClick={onReport} className="text-link">{t('app.reportProblem')}</button>
      : <Link to="/reports" className="text-link">{t('app.reportProblem')}</Link>}
  </main>
}

type ReportingBoundaryProps = { children: ReactNode; onReport?: () => void; onHome?: () => void; reportScreen?: boolean; resetKey?: string; toolName?: string }

export class ReportingErrorBoundary extends Component<ReportingBoundaryProps, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() { return { failed: true } }

  componentDidCatch() { void recordFrontendRenderFailure().catch(() => {}) }

  // A deliberate route change or Owner change can leave a failed screen. Keep
  // healthy children mounted through ordinary navigation and token renewal.
  componentDidUpdate(previous: Readonly<ReportingBoundaryProps>) {
    if (this.state.failed && previous.resetKey !== this.props.resetKey) this.setState({ failed: false })
  }

  render() {
    if (this.state.failed) return <ErrorFallback reportScreen={this.props.reportScreen} onReport={this.props.onReport} onHome={this.props.onHome} toolName={this.props.toolName} onRetry={() => this.setState({ failed: false })} />
    return this.props.children
  }
}

export function ReportingRoot() {
  const location = useLocation()
  const navigate = useNavigate()
  const { t, locale } = useI18n()
  const token = useAuthStore((state) => state.token)
  const identity = useAuthStore((state) => `${state.user?.id || 0}:${state.epoch}`)
  const ownerId = useAuthStore((state) => state.user?.id || 0)
  const [templateLink,setTemplateLink]=useState<{id:string;url:string;identity:string|null;requested:boolean}|null>(null)
  const [linkBusyNotice,setLinkBusyNotice]=useState<'modal'|'pending'|null>(null)
  const [notificationProblem,setNotificationProblem]=useState(false),[notificationRetry,setNotificationRetry]=useState(0),[notificationClick,setNotificationClick]=useState<{id:string;epoch:string|null;identity:string;reason:'modal'|'unresolved'}|null>(null)
  const openingNotification=useRef(false),notificationClickRevision=useRef(0)
  useEffect(()=>{
    let active=true,dispose:(()=>void)|null=null
    void subscribeTemplateLinks(url=>{if(!active)return;const normalized=templatePreviewUrl(url);if(!normalized)return;const auth=useAuthStore.getState();setTemplateLink({id:crypto.randomUUID(),url:normalized,identity:auth.token&&auth.user?.id?`${auth.user.id}:${auth.epoch}`:null,requested:false});setLinkBusyNotice(null)}).then(stop=>{if(active)dispose=stop;else stop()}).catch(()=>{})
    return()=>{active=false;dispose?.()}
  },[])
  useEffect(()=>{setTemplateLink(previous=>{if(!previous)return null;if(previous.identity===null)return token&&ownerId?{...previous,identity}:previous;return token&&ownerId&&previous.identity===identity?previous:null})},[identity,token,ownerId])
  const openTemplateLink=()=>{
    const auth=useAuthStore.getState();if(!templateLink||!templateLink.identity||!auth.token||!auth.user?.id||templateLink.identity!==`${auth.user.id}:${auth.epoch}`)return
    if(document.querySelector('[aria-modal="true"], dialog[open]')){setLinkBusyNotice('modal');return}
    if(hasPendingTemplateImport(templateLink.identity)){setLinkBusyNotice('pending');if(location.pathname!=='/agents')navigate('/agents');return}
    setLinkBusyNotice(null);setTemplateLink({...templateLink,id:crypto.randomUUID(),requested:true});if(location.pathname!=='/agents')navigate('/agents')
  }
  useEffect(()=>{notificationClickRevision.current++;setNotificationClick(null)},[identity,token])
  useEffect(()=>{setNotificationProblem(false);if(!token||!ownerId)return
    return startBotNotifications(ownerId,useAuthStore.getState().epoch,(item:NotificationItem,epoch:string)=>{notificationClickRevision.current++;if(document.querySelector('[aria-modal="true"], dialog[open]'))setNotificationClick({id:item.delivery_id,epoch,identity,reason:'modal'});else{setNotificationClick(null);navigate(notificationHref(item))}},()=>setNotificationProblem(true),(id,epoch)=>{notificationClickRevision.current++;setNotificationClick({id,epoch,identity,reason:'unresolved'})})
  },[identity,token,ownerId,notificationRetry])
  const openNotification=async()=>{const click=notificationClick,a=useAuthStore.getState();if(openingNotification.current||!click||click.identity!==identity||!a.token||a.user?.id!==ownerId||document.querySelector('[aria-modal="true"], dialog[open]'))return
    openingNotification.current=true;const clickRevision=notificationClickRevision.current
    try{const context=await botNotificationsApi.status(ownerId);if((click.epoch&&context.owner_epoch!==click.epoch)||`${useAuthStore.getState().user?.id||0}:${useAuthStore.getState().epoch}`!==identity)throw Error('changed');const item=await botNotificationsApi.resolve(click.id,context.owner_epoch);if(notificationClickRevision.current!==clickRevision||`${useAuthStore.getState().user?.id||0}:${useAuthStore.getState().epoch}`!==identity||document.querySelector('[aria-modal="true"], dialog[open]'))return;notificationClickRevision.current++;setNotificationClick(value=>value?.id===click.id&&value.identity===identity?null:value);navigate(notificationHref(item))}catch{if(notificationClickRevision.current===clickRevision&&`${useAuthStore.getState().user?.id||0}:${useAuthStore.getState().epoch}`===identity){setNotificationClick(value=>value?.id===click.id&&value.identity===identity?{...value,reason:'unresolved'}:value);setNotificationProblem(true)}}finally{openingNotification.current=false}
  }
  const visibleTemplateLink=templateLink&&(templateLink.identity===null?!token:!!token&&templateLink.identity===identity)?templateLink:null
  const resetKey = JSON.stringify([identity, location.key, location.pathname, location.search, location.hash])
  const roomRoute = /^\/chat\/\d+\/?$/.test(location.pathname)
  useEffect(() => {
    const target = voiceCallController.snapshot().target
    if (target && (!token || target.ownerId !== ownerId || `${target.ownerId}:${target.epoch}` !== identity)) voiceCallController.resetOwner()
  }, [identity, token, ownerId])
  useEffect(() => {
    const pause = () => voiceCallController.suspend()
    const visibility = () => { if (document.visibilityState !== 'visible') pause() }
    window.addEventListener('blur', pause); document.addEventListener('visibilitychange', visibility)
    return () => { window.removeEventListener('blur', pause); document.removeEventListener('visibilitychange', visibility); voiceCallController.resetOwner() }
  }, [])
  return <AppHeaderFrame room={roomRoute} controls={<>
    <ReportingErrorBoundary toolName={t('auth.legacyCleanup.title')} onReport={() => navigate('/reports')}><LegacyAuthStorageNotice /></ReportingErrorBoundary>
    {token && <>
      <ReportingErrorBoundary resetKey={identity} toolName={t('attention.title')} onReport={() => navigate('/reports')}><AttentionInbox /></ReportingErrorBoundary>
      <ReportingErrorBoundary resetKey={identity} toolName={t('switcher.title')} onReport={() => navigate('/reports')}><QuickRoomSwitcher /></ReportingErrorBoundary>
      <ReportingErrorBoundary resetKey={identity} toolName={t('keyboardReading.title')} onReport={() => navigate('/reports')}><KeyboardHelp key={identity} identity={identity}/></ReportingErrorBoundary>
    </>}
    <ReportingErrorBoundary resetKey={identity} toolName={t('app.ui.settings')} onReport={() => navigate('/reports')}><AppMenu key={identity} /></ReportingErrorBoundary>
  </>}>
    {token && <VoiceCallPanel />}
    {token&&notificationProblem&&<div role="status" className="notice notice--warning page-frame page-frame--wide"><p>{locale==='ko'?'알림 상태를 확인할 수 없습니다. 앱의 알림 목록에서 확인하세요.':'Notification status could not be confirmed. Check the in-app attention list.'}</p><button type="button" className="button button--secondary button--compact" onClick={()=>{setNotificationProblem(false);setNotificationRetry(v=>v+1)}}>{t('common.retry')}</button><button type="button" className="button button--ghost button--compact" onClick={()=>setNotificationProblem(false)}>{t('common.close')}</button></div>}
    {token&&notificationClick?.identity===identity&&<NotificationClickNotice reason={notificationClick.reason} locale={locale} onOpen={()=>void openNotification()} onDismiss={()=>{notificationClickRevision.current++;setNotificationClick(null)}}/>}
    {visibleTemplateLink&&<div role="status" className="notice notice--info page-frame page-frame--wide"><p>{t('botTemplate.linkNotice')} · {t('botTemplate.linkHost',{host:new URL(visibleTemplateLink.url).host})}</p>{!token&&<p>{t('botTemplate.linkNoticeLogin')}</p>}{linkBusyNotice&&<p>{t(linkBusyNotice==='pending'?'botTemplate.linkPending':'botTemplate.linkCloseDialog')}</p>}<div className="page-actions">{token&&<button type="button" className="button button--secondary" onClick={openTemplateLink}>{t('botTemplate.linkOpen')}</button>}<button type="button" className="button button--ghost" onClick={()=>{setTemplateLink(null);setLinkBusyNotice(null)}}>{t('botTemplate.linkClose')}</button></div></div>}
    <ReportingErrorBoundary key={location.pathname === '/reports' ? 'reports' : 'app'} resetKey={resetKey} reportScreen={location.pathname === '/reports'} onHome={() => navigate('/')} onReport={() => navigate('/reports')}><App templateRequest={visibleTemplateLink?.requested&&visibleTemplateLink.identity?{id:visibleTemplateLink.id,url:visibleTemplateLink.url,identity:visibleTemplateLink.identity}:null} onTemplateOpened={id=>setTemplateLink(value=>value?.id===id?null:value)} /></ReportingErrorBoundary>
  </AppHeaderFrame>
}

function App({templateRequest=null,onTemplateOpened}:{templateRequest?:TemplateLinkRequest|null;onTemplateOpened?:(id:string)=>void}={}) {
  const { t } = useI18n()
  // Stable for this App mount: ordinary route/token changes never replace the
  // page component types. The outer page error boundary unmounts App on failure;
  // an explicit retry then creates fresh lazy objects instead of reusing a
  // React.lazy object with a cached rejected promise. No prefetch or timed retry.
  const [{ Login, Register, Dashboard, Agents, Projects, Chat, Devices, Connections, ReportSettings, Usage, Skills, McpServers, BotLibrary, MemoryImport, Vault }] = useState(() => ({
    Login: lazy(() => import('./pages/Login')),
    Register: lazy(() => import('./pages/Register')),
    Dashboard: lazy(() => import('./pages/Dashboard')),
    Agents: lazy(() => import('./pages/Agents')),
    Projects: lazy(() => import('./pages/Projects')),
    Chat: lazy(() => import('./pages/Chat')),
    Devices: lazy(() => import('./pages/Devices')),
    Connections: lazy(() => import('./pages/Connections')),
    ReportSettings: lazy(() => import('./pages/ReportSettings')),
    Usage: lazy(() => import('./pages/Usage')),
    Skills: lazy(() => import('./pages/Skills')),
    McpServers: lazy(() => import('./pages/McpServers')),
    BotLibrary: lazy(() => import('./pages/BotLibrary')),
    MemoryImport: lazy(() => import('./pages/MemoryImport')),
    Vault: lazy(() => import('./pages/Vault')),
  }))
  const { token, setAuth } = useAuthStore()
  const location = useLocation()
  const [host, setHost] = useState<'loading' | 'ready' | 'failed'>('loading')
  const [hostRetry, setHostRetry] = useState(0)
  const [loginNotice, setLoginNotice] = useState<MessageKey | ''>('')
  const autoRestoreAttempted = useRef(false)

  const restoreSavedLogin = async (isCallerCurrent: () => boolean = () => true) => {
    const epoch = useAuthStore.getState().epoch
    const isCurrentOwner = () => isCallerCurrent() && useAuthStore.getState().epoch === epoch
    if (!isCurrentOwner()) return
    const acceptRestoredLogin = async (accessToken: string) => {
      if (!isCurrentOwner()) return
      if (typeof accessToken !== 'string' || !accessToken) throw new Error(t('auth.savedLoginUnavailable'))
      const profile = await api.get('/auth/me', { headers: { Authorization: `Bearer ${accessToken}` } })
      if (!isCurrentOwner()) return
      if (!Number.isSafeInteger(profile.data?.id) || profile.data.id < 1) throw new Error(t('auth.savedLoginUnavailable'))
      clearSessionOnly()
      setAuth(accessToken, profile.data)
    }

    let restored
    try {
      restored = await api.post('/auth/restore')
    } catch (error) {
      if (!isCurrentOwner()) return
      const notice = autoRestoreNotice(error)
      if (notice) {
        setLoginNotice(notice)
        return
      }
      const failure = error as { config?: { url?: unknown }; response?: { status?: unknown } }
      if (failure.config?.url !== '/auth/restore' || failure.response?.status !== 503) throw error

      // Only legacy credential-store failures use the explicit interactive path.
      const started = await api.post('/auth/restore/interactive')
      if (!isCurrentOwner()) return
      if (started.data.state === 'reentry_required' && started.data.action === 'sign_in_again') {
        setLoginNotice('auth.savedLoginUnavailable')
        return
      }
      if (started.data.state === 'busy') throw new Error(t('app.keychainBusy'))
      if (!['pending', 'ready'].includes(started.data.state)
        || typeof started.data.unlock_id !== 'string'
        || !/^[A-Za-z0-9_-]{20,128}$/.test(started.data.unlock_id))
        throw new Error(t('app.keychainRequired'))
      const unlockId = started.data.unlock_id
      for (let attempts = 0; attempts < 120; attempts += 1) {
        if (!isCurrentOwner()) return
        await new Promise((resolve) => window.setTimeout(resolve, 500))
        if (!isCurrentOwner()) return
        const unlocked = await api.get('/auth/restore/interactive', { params: { unlock_id: unlockId } })
        if (!isCurrentOwner()) return
        if (unlocked.data.state === 'ready') {
          await acceptRestoredLogin(unlocked.data.access_token as string)
          return
        }
        if (unlocked.data.state === 'locked') throw new Error(t('app.keychainRequired'))
      }
      throw new Error(t('app.keychainPending'))
    }
    if (isCurrentOwner()) await acceptRestoredLogin(restored.data.access_token as string)
  }

  useEffect(() => {
    if (location.pathname === '/reports') return
    let active = true
    const epoch = useAuthStore.getState().epoch
    getLocalEndpoint().then(async () => {
      if (!active) return
      if (!token && !autoRestoreAttempted.current) {
        try {
          const skip = suppressAutomaticRestore() || await skipAutoRestoreOnStartup()
          if (!active || useAuthStore.getState().epoch !== epoch) return
          if (autoRestoreAttempted.current) return
          autoRestoreAttempted.current = true
          if (skip) { setHost('ready'); return }
          const restored = await api.post('/auth/restore')
          if (active && useAuthStore.getState().epoch === epoch) {
            const accessToken = restored.data.access_token
            if (typeof accessToken !== 'string' || !accessToken) throw new Error('Saved login response unavailable')
            const profile = await api.get('/auth/me', { headers: { Authorization: `Bearer ${accessToken}` } })
            if (active && useAuthStore.getState().epoch === epoch && Number.isSafeInteger(profile.data?.id) && profile.data.id > 0) setAuth(accessToken, profile.data)
          }
        } catch (error) {
          const notice = autoRestoreNotice(error)
          if (active && useAuthStore.getState().epoch === epoch && notice) setLoginNotice(notice)
        }
      }
      if (active) setHost('ready')
    }).catch(() => {
      if (active) setHost('failed')
    })
    return () => { active = false }
  }, [location.pathname, hostRetry])

  const loadingScreen = <div role="status" className="page-frame page-content stack stack--tight">
    <p>{t('app.loadingScreen')}</p>
    <p className="field-hint">{t('app.loadingScreenHelp')}</p>
  </div>

  if (location.pathname === '/reports') return <Suspense fallback={loadingScreen}><ReportSettings /></Suspense>

  if (host === 'loading') return <div role="status" className="auth-shell"><div className="auth-card">{t('app.startingService')} <Link className="text-link" to="/reports">{t('app.reportProblem')}</Link></div></div>
  if (host === 'failed') return <div role="alert" className="auth-shell"><div className="auth-card notice notice--danger">{t('app.serviceFailure')} <button type="button" className="button button--secondary" onClick={() => { if (host === 'failed') { setHost('loading'); setHostRetry(value => value + 1) } }}>{t('app.retryConnection')}</button> <Link className="text-link" to="/reports">{t('app.reportProblem')}</Link></div></div>

  if (!token) {
    return (
      <Suspense fallback={loadingScreen}><Routes>
        <Route path="/login" element={<Login onRestore={!loginNotice || loginNotice === 'app.savedLoginRecovery' ? restoreSavedLogin : undefined} notice={loginNotice ? t(loginNotice) : ''} />} />
        <Route path="/register" element={<Register />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes></Suspense>
    )
  }

  return (
    <Suspense fallback={loadingScreen}><Routes>
      <Route path="/" element={<Dashboard />} />
      <Route path="/agents" element={<Agents templateRequest={templateRequest} onTemplateOpened={onTemplateOpened} />} />
      <Route path="/memory-import" element={<MemoryImport />} />
      <Route path="/vault" element={<Vault />} />
      <Route path="/agents/:agentId/library" element={<BotLibrary />} />
      <Route path="/skills" element={<Skills />} />
      <Route path="/apps" element={<McpServers />} />
      <Route path="/projects" element={<Projects />} />
      <Route path="/devices" element={<Devices />} />
      <Route path="/connections" element={<Connections />} />
      <Route path="/teams" element={<Teams />} />
      <Route path="/usage" element={<Usage />} />
      <Route path="/chat/:conversationId" element={<Chat />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes></Suspense>
  )
}

export default App
