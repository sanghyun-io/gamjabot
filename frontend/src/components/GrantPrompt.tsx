import { ShieldQuestion } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { toolsApi } from '../api/tools'
import { eventsApi } from '../api/events'
import { validTransport, type McpTransport } from '../api/mcp'
import { actionPreviewMatches, executionContext, routineMcpRootMatches, slackToolRootMatches, toolActionIdentity, toolApprovalAvailable } from '../lib/toolExecution'
import ToolExecutionProvenance from './ToolExecutionProvenance'
import type { ActionPreview, ToolActivity } from '../api/tools'
import { useI18n, type Translate } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import { useAuthStore } from '../stores/authStore'
import ActionReviewDetails from './ActionReviewDetails'
import McpDraftEditor from './McpDraftEditor'

const actionLabels: Record<string, MessageKey> = {
  mcp_tool_call: 'mcp.call', request_vault_login: 'vaultHandoff.title',
  browser_navigate: 'approval.action.browserNavigate', browser_inspect: 'approval.action.browserInspect',
  browser_interact: 'approval.action.browserInteract', screenshot: 'approval.action.screenshot', browser_screenshot: 'approval.image.browserAction',
  write_file: 'approval.action.fileWrite', read_file: 'approval.action.fileRead',
  delete_file: 'approval.action.fileDelete', move_file: 'approval.action.fileMove',
  start_process: 'approval.action.processRun', cancel_process: 'approval.action.processCancel',
  keyboard_text: 'approval.action.keyboardType', keyboard_chord: 'approval.action.keyboardChord',
  pointer_move: 'approval.action.pointerMove', pointer_click: 'approval.action.pointerClick',
  pointer_scroll: 'approval.action.pointerScroll',
}

export function toolActionName(family: string, t: Translate) {
  return t(actionLabels[family] || 'approval.localAction')
}

const exactEffects: Record<string, MessageKey> = {
  'Navigate browser': 'approval.effect.browserNavigate', 'Inspect page': 'approval.effect.browserInspect',
  'Capture screenshot': 'approval.effect.screenshot', 'Capture browser page': 'approval.image.browserEffect', 'Read file': 'approval.effect.fileRead',
  'Delete file': 'approval.effect.fileDelete', 'Move file': 'approval.effect.fileMove',
  'Cancel process': 'approval.effect.processCancel', 'Press keyboard chord': 'approval.effect.keyboardChord',
  'Pointer move': 'approval.effect.pointerMove', 'Pointer click': 'approval.effect.pointerClick',
  'Pointer scroll': 'approval.effect.pointerScroll',
}

const repeatedActionEffects: Record<string, string> = { browser_navigate: 'Navigate browser', browser_inspect: 'Inspect page', screenshot: 'Capture screenshot', read_file: 'Read file', delete_file: 'Delete file', move_file: 'Move file', cancel_process: 'Cancel process', keyboard_chord: 'Press keyboard chord', pointer_move: 'Pointer move', pointer_click: 'Pointer click', pointer_scroll: 'Pointer scroll' }

export function displayTarget(target: string, t: Translate) {
  if (target === 'desktop:screen' || target === 'desktop') return t('approval.target.currentScreen')
  if (target === 'desktop:pointer') return t('approval.target.pointer')
  if (target === 'desktop:keyboard') return t('approval.target.keyboard')
  const visible = target.replace(/\s+\[(?:payload|invocation|text|chord|interaction) sha256:[0-9a-f]+\]$/, '')
  const pointer = /^desktop:pointer@(-?\d+),(-?\d+)$/.exec(visible)
  return pointer ? t('approval.target.pointerAt', { x: pointer[1], y: pointer[2] }) : visible
}

export function mcpTargetParts(target:string, action:ToolActivity):{name:string;transport:McpTransport;tool:string}|null {
  if(action.family!=='mcp_tool_call' || !action.mcp_binding)return null
  const parts=/^(.+) \| (\{.*\}) \| (.+)$/.exec(target)
  if(!parts || parts[3]!==action.mcp_binding.tool_name)return null
  try {const transport:unknown=JSON.parse(parts[2]);if(!validTransport(transport)||transport.type!==action.mcp_binding.transport_type||transport.type==='stdio'&&transport.executable!==action.mcp_binding.executable)return null;return {name:parts[1],transport,tool:parts[3]}} catch{return null}
}

function mcpTransportDetail(transport: McpTransport): string | null {
  if (transport.type === 'stdio') return transport.executable
  try { const url = new URL(transport.url); return ['http:', 'https:'].includes(url.protocol) ? url.origin : null } catch { return null }
}

export function plainEffect(effect: string, t: Translate) {
  const bytes = /^Write (\d+) bytes to file$/.exec(effect)
  if (bytes) return t('approval.effect.writeBytes', { count: Number(bytes[1]) })
  const typed = /^Type (\d+) characters$/.exec(effect)
  if (typed) return t('approval.effect.typeCharacters', { count: Number(typed[1]) })
  const browser = /^Browser (.+)$/.exec(effect)
  if (browser) return t('approval.effect.browserAction', { action: browser[1] })
  const process = /^Run process with local user authority; (\d+) arguments(?:; owned background lease up to (\d+) seconds after start)?$/.exec(effect)
  if (process) return t(process[2] ? 'approval.effect.processRunBackground' : 'approval.effect.processRun', { count: Number(process[1]), seconds: Number(process[2] || 0) })
  return exactEffects[effect] ? t(exactEffects[effect]) : effect
}

function roomScopeMeaning(family: string, t: Translate) {
  if (family === 'write_file') return t('approval.scope.file')
  if (family.startsWith('browser_')) return t('approval.scope.browser')
  if (family === 'start_process') return t('approval.scope.process')
  if (family.startsWith('keyboard_')) return t('approval.scope.keyboard')
  if (family.startsWith('pointer_')) return t('approval.scope.pointer')
  if (family === 'screenshot') return t('approval.scope.screenshot')
  return t('approval.scope.generic')
}

export default function GrantPrompt({ roomId, roomName, agentName, action, onRefresh, onStop, stopPending = false, interactive = true }:
  { roomId: number; roomName: string; agentName: string; action: ToolActivity; onRefresh: () => void; onStop: () => void; stopPending?: boolean; interactive?: boolean }) {
  const authEpoch = useAuthStore((state) => state.epoch)
  const owner = useAuthStore((state) => state.user?.id ?? null)
  const [revision, setRevision] = useState(0)
  const identity = JSON.stringify([roomId, toolActionIdentity(action), authEpoch, owner, revision])
  const scopeRef = useRef({ identity, generation: 0 })
  if (scopeRef.current.identity !== identity) {
    scopeRef.current = { identity, generation: scopeRef.current.generation + 1 }
  }
  const generation = scopeRef.current.generation
  const key = JSON.stringify([identity, generation])
  const alive = useRef(true)
  const inFlight = useRef(new Set<string>())
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [loaded, setLoaded] = useState<{ key: string; value: ActionPreview } | null>(null)
  const [failure, setFailure] = useState<{ key: string; code: string } | null>(null)
  const [mcpRoot, setMcpRoot] = useState<{key:string;ok:boolean;slackContent?:string}|null>(null)
  const [imageConsent, setImageConsent] = useState<{ key: string; review: string; checked: boolean } | null>(null)
  const [draftGate, setDraftGate] = useState<{ key: string; blocked: boolean } | null>(null)
  const [consent, setConsent] = useState<{ key: string; review: string; checked: boolean } | null>(null)
  const { t, locale } = useI18n()
  const current = () => alive.current && scopeRef.current.identity === identity
    && scopeRef.current.generation === generation
    && useAuthStore.getState().epoch === authEpoch
    && (useAuthStore.getState().user?.id ?? null) === owner
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])
  useEffect(() => {
    let active = true
    setLoaded(null)
    setFailure(null)
    setConsent(null)
    setImageConsent(null)
    toolsApi.preview(roomId, action.action_id).then((value) => {
      if (!active || !current()) return
      if (!actionPreviewMatches(action, value, owner || 0)) {
        setFailure({ key, code: 'stale' })
        return
      }
      setLoaded({ key, value })
    }).catch(() => {
      if (active && current()) { setFailure({ key, code: 'stale' }); onRefresh() }
    })
    return () => { active = false }
  }, [roomId, action.action_id, onRefresh, revision, authEpoch, owner, generation])
  useEffect(() => {
    if (loaded?.key !== key || !owner || !(action.family === 'mcp_tool_call' && action.mcp_binding || action.slack_binding)) return
    let active=true
    const scoped=executionContext(action,owner,loaded.value)
    const rootRoom=scoped.kind==='scoped'||scoped.kind==='slack'?scoped.root.room_id:roomId
    const verify=async()=>{let after=0
      for(let page=0;page<20;page++) {const rows=await eventsApi.list(rootRoom,after,100);if(!active||!current())return
        const root=rows.find(row=>row.event_id===action.trigger_event_id)
        if(root){const ok=scoped.kind==='slack'?slackToolRootMatches(root,scoped,action):scoped.kind==='routine'?routineMcpRootMatches(root,scoped,action):root.room_id===rootRoom&&root.event_id===action.trigger_event_id&&root.actor_type==='user'&&root.actor_id===owner&&root.kind==='message'&&(scoped.kind!=='scoped'||root.seq===scoped.root.seq);setMcpRoot({key,ok,...(ok&&scoped.kind==='slack'&&typeof root.payload.content==='string'?{slackContent:root.payload.content}:{})});return}
        if(rows.length<100 || rows.at(-1)!.seq<=after)break;after=rows.at(-1)!.seq
      }
      if(active&&current())setMcpRoot({key,ok:false})
    }
    setMcpRoot(null);void verify().catch(()=>{if(active&&current())setMcpRoot({key,ok:false})})
    return()=>{active=false}
  }, [action.family, action.action_id, action.trigger_event_id, action.mcp_binding?.root_event_id, roomId, owner, loaded?.key, key, generation])
  // Scope the rendered data as well as effects: no old preview is painted
  // between a new render and the next useEffect cleanup.
  const preview = loaded?.key === key ? loaded.value : null
  const error = failure?.key === key ? failure.code : ''
  const busy = busyKey === key
  const execution = executionContext(action, owner || 0, preview || action)
  const draftBlocked = interactive && action.family === 'mcp_tool_call' && (draftGate?.key !== key || draftGate.blocked)
  const baseApprovalAvailable = toolApprovalAvailable(execution) && (action.family !== 'mcp_tool_call' && !action.slack_binding || mcpRoot?.key === key && mcpRoot.ok)
  const memoArgs = preview?.family === 'synthesize_voice_memo' ? preview.arguments : null
  const memoReview = preview?.review && typeof preview.review === 'object' && !Array.isArray(preview.review) ? preview.review as Record<string,unknown> : null
  const memoAction = memoReview?.action && typeof memoReview.action === 'object' ? memoReview.action as Record<string,unknown> : null
  const memoAuthority = memoReview?.authority && typeof memoReview.authority === 'object' ? memoReview.authority as Record<string,unknown> : null
  const memoTargets = Array.isArray(memoReview?.targets) ? memoReview.targets : []
  const memoTarget = memoTargets.length === 1 && memoTargets[0] && typeof memoTargets[0] === 'object' ? memoTargets[0] as Record<string,unknown> : null
  const memoValid = action.family !== 'synthesize_voice_memo' || !!(preview && memoArgs && owner && action.review_id === preview.review_id && preview.family === action.family && preview.target === action.target && preview.expected_effect === action.expected_effect && preview.approval_policy.scopes.length === 1 && preview.approval_policy.scopes[0] === 'once' && Object.keys(memoArgs).sort().join(',') === 'locale,rate_milli,text,voice_id' && typeof memoArgs.text === 'string' && !!memoArgs.text.trim() && new TextEncoder().encode(memoArgs.text).length <= 4000 && typeof memoArgs.voice_id === 'string' && memoArgs.voice_id.length > 0 && memoArgs.voice_id.length <= 200 && typeof memoArgs.locale === 'string' && memoArgs.locale.length > 0 && memoArgs.locale.length <= 64 && Number.isSafeInteger(memoArgs.rate_milli) && Number(memoArgs.rate_milli) > 0 && Number(memoArgs.rate_milli) <= 1000 && memoAction?.action_id === action.action_id && memoAction.room_id === roomId && memoAction.agent_id === action.agent_id && memoAction.trigger_event_id === action.trigger_event_id && memoAction.family === action.family && memoAction.target === preview.target && memoAction.scope_target === preview.scope_target && memoAction.expected_effect === preview.expected_effect && memoAction.payload_digest === preview.payload_digest && memoAuthority?.owner_id === owner && memoAuthority.room_id === roomId && memoAuthority.agent_id === action.agent_id && memoTarget?.role === 'voice_memo' && memoTarget.status === 'configured')
  const canApprove = baseApprovalAvailable && !draftBlocked && memoValid
  const requiresContext = preview?.approval_policy.unverified_context_required === true
  const checked = consent?.key === key && consent.review === preview?.review_id && consent.checked
  const capture = action.family === 'screenshot' || action.family === 'browser_screenshot'
  const imageChecked = capture && imageConsent?.key === key && imageConsent.review === preview?.review_id && imageConsent.checked
  const actionLabel = action.family === 'synthesize_voice_memo' ? (locale === 'ko' ? '음성 메모 만들기' : 'Create voice memo') : toolActionName(preview?.family || action.family, t)
  const mcpTarget = preview ? mcpTargetParts(preview.target, action) : null
  const decide = async (decision: 'approve' | 'deny', scope?: 'once' | 'room_session') => {
    if (!interactive || !current() || inFlight.current.has(key)) return
    if (decision === 'approve' && (!preview || !canApprove || !scope || action.family === 'mcp_tool_call' && scope !== 'once' || !preview.approval_policy.scopes.includes(scope)
      || (requiresContext && !checked))) return
    inFlight.current.add(key)
    setBusyKey(key)
    try {
      await toolsApi.decide(roomId, action.action_id, decision, scope,
        decision === 'approve' ? preview!.review_id : undefined,
        decision === 'approve' && requiresContext && !!checked,
        decision === 'approve' && !!imageChecked)
      if (!current()) return
      setLoaded(null)
      setFailure(null)
      setConsent(null)
      setImageConsent(null)
      onRefresh()
    } catch (cause) {
      if (!current()) return
      const status = (cause as { response?: { status?: number } }).response?.status
      setLoaded(null)
      setConsent(null)
      setImageConsent(null)
      setFailure({ key, code: status === 404 || status === 409 ? 'changed' : 'failure' })
      onRefresh()
    } finally {
      inFlight.current.delete(key)
      if (current()) setBusyKey(null)
    }
  }
  return <article className="approval stack stack--tight" style={action.family === 'mcp_tool_call' && interactive ? { width: 'min(100%, 32rem)' } : undefined} aria-label={t('approval.aria.article', { agent: agentName })}>
    <header className="approval__header"><ShieldQuestion size={19} aria-hidden="true"/><h3 className="section-title">{agentName} · {actionLabel}</h3></header>
    {!preview && <ToolExecutionProvenance context={execution} roomName={roomName} canUse={current} />}
    {!preview && !error && <p role="status" className="section-copy">{t('approval.loading')}</p>}
    {error && <><p role="alert" className="notice notice--danger">{t(`approval.${error}` as MessageKey)}</p><button className="button button--secondary" onClick={() => setRevision((value) => value + 1)}>{t('approval.reload')}</button></>}
    {preview && <div className="approval__preview stack stack--tight">
      {capture && <p>{t(action.family === 'screenshot' ? 'approval.image.desktopTarget' : 'approval.image.browserTarget')}</p>}
      {action.family === 'request_vault_login' ? <>
        <ActionReviewDetails review={preview.review} reviewId={preview.review_id} actionId={action.action_id}
          roomId={roomId} agentId={action.agent_id} ownerId={owner} executionBinding={execution.kind === 'scoped' ? execution.binding : null}
          vaultSummary={{target:preview.target,scopeTarget:preview.scope_target,effect:preview.expected_effect,digest:preview.payload_digest,rootId:action.trigger_event_id}} />
        <p className="field-hint">{t('vaultHandoff.returnHelp')}</p>
      </> : <>
        {mcpTarget ? <div className="stack stack--tight"><p>{t('mcp.targetApp')} · {mcpTarget.name}</p><p>{t('mcp.targetTool')} · {mcpTarget.tool}</p></div> : <p>{action.family === 'mcp_tool_call' ? t('review.unavailable') : t('approval.target', { target: displayTarget(preview.target, t) })}</p>}
        {repeatedActionEffects[preview.family] !== preview.expected_effect && <p>{plainEffect(preview.expected_effect, t)}</p>}
      </>}
      {action.family === 'synthesize_voice_memo' && (memoValid && memoArgs ? <section className="notice stack stack--tight" aria-label={locale === 'ko' ? '음성 메모 승인 내용' : 'Voice memo approval'}><strong>{locale === 'ko' ? '읽을 텍스트' : 'Text to speak'}</strong><p style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{memoArgs.text as string}</p><p>{locale === 'ko' ? '목소리' : 'Voice'}: {memoArgs.voice_id as string} · {locale === 'ko' ? '언어' : 'Language'}: {memoArgs.locale as string} · {locale === 'ko' ? '속도' : 'Rate'}: {Number(memoArgs.rate_milli)/1000}</p><p className="field-hint">{locale === 'ko' ? '기기에서 음성 파일 1개를 만듭니다. 최대 10 MiB, 5분이며 자동 재생하지 않습니다.' : 'Creates one local audio file, up to 10 MiB and 5 minutes. It will not play automatically.'}</p></section> : <p role="alert">{locale === 'ko' ? '승인 내용을 확인할 수 없습니다. 다시 검토해 주세요.' : 'The approval details could not be confirmed. Review again.'}</p>)}
      {action.family !== 'mcp_tool_call' && preview.approval_policy.scopes.includes('room_session') && displayTarget(preview.scope_target, t) !== displayTarget(preview.target, t) && <p className="section-copy">{t('approval.scope', { scope: displayTarget(preview.scope_target, t) })}</p>}
      <ToolExecutionProvenance context={execution} roomName={roomName} canUse={current} />
      {execution.kind==='slack'&&mcpRoot?.key===key&&mcpRoot.ok&&mcpRoot.slackContent&&<p className="notice notice--info text-prewrap"><strong>{locale==='ko'?'Slack 원문 요청':'Original Slack request'}</strong><br/>{mcpRoot.slackContent}</p>}
      {action.family === 'mcp_tool_call' && <>{interactive && <McpDraftEditor key={key} roomId={roomId} actionId={action.action_id} reviewId={preview.review_id} scopeKey={key} onBlocked={blocked => setDraftGate({ key, blocked })} onApplied={() => { setConsent(null); setImageConsent(null); setLoaded(null); setDraftGate({ key, blocked: true }); onRefresh() }} />}{!baseApprovalAvailable && <p role="status">{t('mcp.approvalUnavailable')}</p>}</>}
      {requiresContext && <div className="stack stack--tight">
        <label><input type="checkbox" checked={!!checked} disabled={busy}
          onChange={(event) => setConsent({ key, review: preview.review_id, checked: event.target.checked })} /> {t(action.family === 'mcp_tool_call' ? 'mcp.confirmRequest' : 'approval.confirmDirect')}</label>
      </div>}
      {interactive && capture && <div className="stack stack--tight">
        <label><input type="checkbox" checked={!!imageChecked} disabled={busy}
          onChange={(event) => setImageConsent({ key, review: preview.review_id, checked: event.target.checked })} /> {t('approval.image.send')}</label>
        <p className="section-copy">{t('approval.image.explanation')}</p>
      </div>}
      <div className="approval__options">
      {interactive && action.family !== 'mcp_tool_call' && preview.approval_policy.scopes.includes('room_session') && <details className="compact-details approval__continued"><summary>{t('approval.continueChoice')}</summary>
        <div className="stack stack--tight"><p>{roomScopeMeaning(preview.family, t)}</p><p>{t('approval.sessionLimitShort')}</p>
        <button aria-label={t('approval.aria.room', { family: actionLabel, agent: agentName })} disabled={busy || !canApprove || (requiresContext && !checked)} className="button button--secondary button--compact" onClick={() => decide('approve', 'room_session')}>{t('approval.session')}</button></div>
      </details>}
      <details className="compact-details"><summary>{t('common.details')}</summary>
        <ActionReviewDetails review={preview.review} reviewId={preview.review_id} actionId={action.action_id}
          roomId={roomId} agentId={action.agent_id} ownerId={owner} executionBinding={execution.kind === 'scoped' ? execution.binding : null} />
        <dl className="technical approval__technical"><dt>{t('approval.actionKind')}</dt><dd>{preview.family}</dd>{action.family !== 'mcp_tool_call' && <><dt>{t('approval.rawTarget')}</dt><dd>{preview.target}</dd></>}
          <dt>{t('approval.rawEffect')}</dt><dd>{preview.expected_effect}</dd><dt>{t('approval.reuseScope')}</dt><dd>{preview.scope_description}</dd>{action.family !== 'mcp_tool_call' && <><dt>{t('approval.scopeTarget')}</dt><dd>{preview.scope_target}</dd></>}</dl>
        {mcpTarget && mcpTransportDetail(mcpTarget.transport) && <dl className="technical approval__technical"><dt>{t(mcpTarget.transport.type === 'http' ? 'mcp.url' : 'mcp.command')}</dt><dd>{mcpTransportDetail(mcpTarget.transport)}</dd></dl>}
        <pre className="technical">{JSON.stringify(preview.arguments, null, 2)}</pre>
      </details>
      </div>
    </div>}
    {interactive ? <div className="page-actions approval__actions">
      {preview && <>
        <button aria-label={t('approval.aria.once', { family: actionLabel, agent: agentName })} disabled={busy || !canApprove || (requiresContext && !checked)} className="button button--primary" onClick={() => decide('approve', 'once')}>{t(requiresContext ? 'approval.onceTimed' : 'approval.once')}</button>

      </>}
      <button aria-label={t('approval.aria.deny', { family: actionLabel, agent: agentName })} disabled={busy} className="button button--secondary" onClick={() => decide('deny')}>{t('approval.deny')}</button>
      <button type="button" aria-label={stopPending ? t('chat.stopSending') : t('approval.aria.stop', { room: roomName })} className="button button--danger" disabled={stopPending} onClick={onStop}>{t(stopPending ? 'chat.stopSending' : 'approval.stop')}</button>
    </div> : <p className="section-copy">{t('approval.decideInChat')}</p>}
  </article>
}
