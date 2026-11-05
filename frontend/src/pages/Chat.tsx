import RoutineChatPicker from '../components/RoutineChatPicker'
import {botsApi} from '../api/bots'
import type {Routine} from '../api/routines'
import {saveRoutineHandoff,readRoutineHandoff,clearRoutineHandoff} from '../lib/routineChatHandoff'
import SkillRunComposer from '../components/SkillRunComposer'
import { SharedBotApps } from '../components/SharedBotApps'
import { sharedBotAppsApi, sharedAppsPendingStore } from '../api/sharedBotApps'
import { SharedPrivateMemory } from '../components/SharedPrivateMemory'
import { readSharedRoomBinding, readSharedPrivateMemory } from '../api/teamService'
import { SharedRoomStatus } from '../components/TeamService'
import MessageReactions from '../components/MessageReactions'
import { foldReactions, reactionActivity, botReactionActivity, foldBotReactions } from '../lib/messageReactions'
import TargetedRetry from '../components/TargetedRetry'
import FileQuestionComposer, { fileQuestionMetadata, fileReadCoverage, type FileQuestionHandle, type FileQuestionState } from '../components/FileQuestionComposer'
import { memoryImportMessage } from '../lib/memoryImportMessage'
import MemoryImportSource from '../components/MemoryImportSource'
import { editorTextLength } from '../lib/editorValidation'
import ComposerDraftConflict from '../components/ComposerDraftConflict'
import VoiceInput from '../components/VoiceInput'
import { VoiceCallStart, VoiceCallHistory } from '../components/VoiceCall'
import { voiceCallController } from '../lib/voiceCallController'
import { attachScopeStop, beginRoomStop, readRoomStop, roomSendAllowed, settleRoomStop, subscribeRoomStops } from '../lib/roomStopWorkspace'
import RoomStopNotice from '../components/RoomStopNotice'
import ScopeStopStatus from '../components/ScopeStopStatus'
import RoomViewTabs from '../components/RoomViewTabs'
import FailureReference from '../components/FailureReference'
import RoomCloseDialog, { roomCloseTarget,sameRoomCloseTarget,type RoomCloseTarget } from '../components/RoomCloseDialog'
import TranscriptExportOptions, { describeTranscriptExport } from '../components/TranscriptExportOptions'
import type { TranscriptExportScope } from '../lib/transcriptFileSave'
import TranscriptFileSave from '../components/TranscriptFileSave'
import ToolActivityRecord from '../components/ToolActivityRecord'
import PageResultCard, { pageResultMetadata } from '../components/PageResultCard'
import MessageThread from '../components/MessageThread'
import { useClipboardText } from '../lib/useClipboardText'
import { useFindShortcut } from '../lib/useFindShortcut'
import ServerTime from '../components/ServerTime'
import ClipboardTextFallback from '../components/ClipboardTextFallback'
import { readMemoryWorkspace, subscribeMemoryWorkspaces } from '../lib/memoryEditorWorkspace'
import MessageDeliveryNotice from '../components/MessageDeliveryNotice'
import { subscribePendingMessages, pendingRoomMessages, blocksNewMessage, freezePendingMessage, updatePendingMessage, consumeConfirmedMessage, rejectPendingMessage, matchesPendingMessage, type PendingRoomMessage } from '../lib/pendingRoomMessages'
import { readUnsavedComposerDraft, readUnsavedComposerEditId, keepUnsavedComposerDraft, acknowledgeComposerDraft } from '../lib/unsavedComposerDrafts'
import { captureRoomReadingPosition, restoreRoomReadingPosition, type RoomReadingPosition } from '../lib/roomReadingPosition'
import { directoryViewGeneration, readDirectoryView, writeDirectoryView } from '../lib/directoryViewMemory'
import { Fragment, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useParams, Link, useNavigate, useSearchParams, useLocation } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Copy, Download, Link as LinkIcon, ListChecks, MoreHorizontal, Quote, Search, Send, X } from 'lucide-react'
import { invoke, isTauri } from '@tauri-apps/api/core'
import { conversationsApi,type Conversation } from '../api/conversations'
import { eventsApi, sameReply, validReply, sameRoutineReference, type RoutineReference, type ReplyReference } from '../api/events'
import { teammatesApi } from '../api/teammates'
import { useBotConversationMetadata } from '../lib/useBotScope'
import type { RoomEvent } from '../api/events'
import { canonicalEventId } from '../lib/peerReply'
import { draftsApi, type RoomDraft } from '../api/drafts'
import { readStatesApi } from '../api/readStates'
import { agentsApi } from '../api/agents'
import { projectsApi,type Project } from '../api/projects'
import { toolsApi } from '../api/tools'
import { openRoomSocket } from '../lib/localHost'
import { roomEventCursor } from '../lib/eventCursor'
import { useAuthStore } from '../stores/authStore'
import BrowserProfiles from '../components/BrowserProfiles'
import RoomPlans from '../components/RoomPlans'
import RoomMembers from '../components/RoomMembers'
import { AgentCharacter } from '../components/AgentCharacter'
import ConversationAgents from '../components/ConversationAgents'
import RoomMembersSummary from '../components/RoomMembersSummary'
import RoomManagementDialog from '../components/RoomManagementDialog'
import RecoveryBanner from '../components/RecoveryBanner'
import RequestRetryHistory from '../components/RequestRetryHistory'
import FinalizeHistory from '../components/FinalizeHistory'
import Activity from './Activity'
import { broadModeKey, readBroadModeForOwner, readOwnerToolData, toolActivityKey, useBroadModeAction } from '../lib/useBroadModeAction'
import GrantPrompt from '../components/GrantPrompt'
import RootToolActions from '../components/RootToolActions'
import RootBotBatches from '../components/RootBotBatches'
import BotBatchEvent from '../components/BotBatchEvent'
import BotCreation from '../components/BotCreation'
import BotPersistence from '../components/BotPersistence'
import MemoryChangeEvent, { memoryChangeMetadata } from '../components/MemoryChangeEvent'
import RoutinePersistenceEvent, { routinePersistenceMetadata } from '../components/RoutinePersistenceEvent'
import SkillPersistenceEvent, { skillPersistenceMetadata } from '../components/SkillPersistenceEvent'
import { botPersistenceMetadata } from '../lib/botPersistence'
import { botCreationsApi } from '../api/botCreations'
import { botCreationMetadata, duplicateCreationRequest } from '../lib/botCreation'
import { botBatchesApi } from '../api/botBatches'
import { botBatchMetadata, summarizedBatchRequestRoot, isBatchMemberMessage } from '../lib/botBatch'
import { agentStatus, type ConnectionState } from '../components/agentStatus'
import ScreenshotArtifact from '../components/ScreenshotArtifact'
import VoiceMemoArtifact from '../components/VoiceMemoArtifact'
import { voiceMemoFromEvent } from '../api/voiceMemos'
import RoomResults from '../components/RoomResults'
import { rememberDraftRoomName } from '../lib/composerDraftIndex'
import { messageEventHref, readResultRoute, resultVersionHref } from '../lib/resultLink'
import { readResultWorkspace, subscribeResultWorkspaces, resultWorkspaceSending, resultWorkspaceUncertain } from '../lib/resultSaveWorkspace'
import RoomMemory from '../components/RoomMemory'
import { resultSource } from '../lib/resultSelection'
import ScreenshotGallery from '../components/ScreenshotGallery'
import MessageContent from '../components/MessageContent'
import PeerReply from '../components/PeerReply'
import TeammateHandoff from '../components/TeammateHandoff'
import BotMessage from '../components/BotMessage'
import { botMessagesApi, type BotMessageRef } from '../api/botMessages'
import { botMessageMetadata } from '../lib/botMessage'
import AgentMemoryDialog from '../components/AgentMemoryDialog'
import AnchoredPopover from '../components/AnchoredPopover'
import { HeaderControlsSlot } from '../components/AppHeaderFrame'
import { conversationTimeline, auxiliaryEvent, activityOutcome } from '../lib/conversationTimeline'
import { assistantVisibleMarkdown } from '../lib/assistantContent'
import RoomScheduleSettings from '../components/RoomScheduleSettings'
import ResponseComparison from '../components/ResponseComparison'
import HandoffNote from '../components/HandoffNote'
import ContextPreview from '../components/ContextPreview'
import RequestInspector from '../components/RequestInspector'
import type { RequestRetryResult } from '../api/requestRetries'
import type { FinalizeResult } from '../api/requestFinalize'
import { closeOtherRoomPanels } from '../lib/roomPanels'
import { Avatar, StatusText } from '../components/ui'
import { useI18n, type Translate } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import { buildRecipientGroups, filterRecipientGroups, filterTranscriptEvents, finalResultSource, mentionAtCaret, replaceMentionAtCaret,
  quoteAtCaret, quoteReplyToBot, routableMentionContent, transcriptFilename, transcriptMarkdown, type RecipientGroup, type TranscriptActorFilter } from '../lib/roomWorkflow'

const permissionHelp: Record<string, MessageKey> = {
  screen_recording_denied: 'chat.permission.screenHelp', accessibility_denied: 'chat.permission.accessibilityHelp',
  input_blocked: 'chat.permission.inputBlockedHelp', protected_surface: 'chat.permission.protectedHelp',
  desktop_busy: 'chat.permission.desktopBusyHelp',
}

const specificFailureHelp: Record<string, MessageKey> = {
  provider_invalid_response: 'chat.failure.invalidProviderResponse',
  teammate_result_incomplete: 'chat.failure.teammateResultIncomplete',
  teammate_result_changed: 'chat.failure.teammateResultIncomplete',
  unsupported_option: 'chat.failure.unsupportedOption',
  secret_store_unavailable: 'chat.failure.secretStoreUnavailable',
}

const sharedAppsAdapter = {...sharedBotAppsApi,store:sharedAppsPendingStore,confirm:(message:string)=>window.confirm(message)}

function eventSummary(event: RoomEvent, t: Translate): string {
  const value = event.payload
  if (event.kind === 'activity' && value.category === 'teammate_handoff') return t('teammates.activity')
  if (event.kind === 'activity' && value.category === 'public_watch') return t(value.status === 'paused' ? 'roomPlans.watchPaused' : 'roomPlans.watchChanged')
  if (event.kind === 'message' || event.kind === 'summary') return String(value.content || '')
  if (event.kind === 'failure') {
    if (event.actor_type === 'system' && value.category === 'reply_target_unavailable' && value.code === 'reply_target_unavailable') return t('inspector.replySystemFailure')
    if (value.code === 'bot_persistence_conflict') return t('botPersistence.conflict')
    if (value.code === 'bot_persistence_busy') return t('botPersistence.busy')
    if (typeof value.code === 'string' && value.code.startsWith('bot_persistence_')) return t('botPersistence.failed')
    if (value.code === 'bot_create_source_changed') return t('botCreation.sourceChanged')
    if (value.code === 'bot_create_budget_exhausted') return t('botCreation.budget')
    if (value.code === 'bot_create_forbidden') return t('botCreation.forbidden')
    if (typeof value.code === 'string' && value.code.startsWith('bot_create_')) return t('botCreation.failed')
    if (typeof value.code === 'string' && Object.prototype.hasOwnProperty.call(specificFailureHelp, value.code)) return t(specificFailureHelp[value.code])
    if (['model_feature_unavailable','local_model_unavailable','model_capabilities_changed'].includes(String(value.code))) return t('chat.ui.configurationNeeded')
    if (value.code === 'model_output_unavailable') return t('localModels.outputBlocked')
    if (value.code === 'missing_secret') return t('chat.failure.missingSecret')
    if (value.code === 'provider_unavailable') return t('chat.failure.providerUnavailable')
    return t('chat.failure.generic')
  }
  if (event.kind === 'tool_result') {
    if (value.error_code === 'screen_recording_denied') return t('chat.permission.screen')
    if (value.error_code === 'accessibility_denied') return t('chat.permission.accessibility')
    if (value.error_code === 'input_blocked') return t('chat.permission.inputBlocked')
    if (value.error_code === 'protected_surface') return t('chat.permission.protected')
    if (value.error_code === 'desktop_busy') return t('chat.permission.desktopBusy')
    if (value.state === 'completed' && typeof value.screenshot_ref === 'string') return t('chat.tool.screenshotReady')
    if (value.state === 'completed') return t('chat.tool.completed')
    if (value.state === 'started' || value.state === 'waiting') return t('chat.tool.progress')
    if (value.state === 'cancelled') return t('chat.activity.cancelled')
    if (value.state === 'partial') return t('chat.activity.partial')
    if (value.state === 'uncertain') return t('chat.tool.uncertain')
    if (value.state === 'failed' || value.state === 'denied') return t('chat.tool.failed')
    return t('chat.tool.updated')
  }
  return t('chat.system.activity')
}

function needsNewActionReview(event: RoomEvent) {
  return event.kind === 'tool_result' && (event.payload.state === 'failed' || event.payload.state === 'denied') && event.payload.error_code === 'review_changed'
}

function eventTechnicalDetails(event: RoomEvent, t: Translate, isFinalizeRequest = false): Array<[string, string]> {
  const details: Array<[string, string]> = [[t('chat.detail.sequence'), `#${event.seq}`], [t('chat.detail.kind'), event.kind], [t('chat.detail.event'), event.event_id]]
  const value = event.payload
  for (const [key, message] of [['action_id', 'review.action'], ['review_id', 'review.id']] as const) {
    if (typeof value[key] === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value[key])) {
      details.push([t(message), value[key]])
    }
  }
  if ((event.actor_type === 'system' || isFinalizeRequest) && (event.kind === 'message' || event.kind === 'summary') && typeof value.content === 'string') {
    details.push([t('chat.detail.content'), value.content])
  }
  if (event.kind === 'activity' && value.category === 'public_watch' && typeof value.content === 'string') details.push([t('chat.detail.content'), value.content.slice(0,3000)])
  if (event.kind === 'activity' && value.category === 'teammate_handoff') {
    if (typeof value.status === 'string') details.push([t('chat.detail.state'), value.status])
    if (typeof value.handoff_id === 'string') details.push([t('chat.detail.reference'), value.handoff_id])
  }
  if (event.kind === 'failure') {
    if (typeof value.code === 'string') details.push([t('chat.detail.code'), value.code])
    if (typeof value.message === 'string') details.push([t('chat.detail.message'), value.message])
    const diagnostic = value.diagnostic && typeof value.diagnostic === 'object' ? value.diagnostic as Record<string, unknown> : null
    if (diagnostic && typeof diagnostic.stage === 'string' && /^(context_build|provider_request|provider_result|tool_validation|tool_execution|turn_persist|turn_setup)$/.test(diagnostic.stage)) details.push([t('chat.detail.stage'), diagnostic.stage])
    if (diagnostic && typeof diagnostic.correlation_id === 'string' && /^[0-9a-f]{32}$/.test(diagnostic.correlation_id)) details.push([t('chat.detail.reference'), diagnostic.correlation_id])
  }
  if (event.kind === 'tool_result') {
    if (typeof value.state === 'string') details.push([t('chat.detail.state'), value.state])
    if (typeof value.text === 'string' && value.text) details.push([t('chat.detail.result'), value.text])
    if (typeof value.error_code === 'string') details.push([t('chat.detail.code'), value.error_code])
  }
  return details
}

function ownerPageRename(event:RoomEvent, owner:number):boolean {
 const p=event.payload
 return event.kind==='activity'&&event.actor_type==='user'&&event.actor_id===owner&&canonicalEventId(event.event_id)&&p.category==='page_owner_rename'&&p.version===1&&canonicalEventId(p.result_id)&&typeof p.base_version==='number'&&Number.isSafeInteger(p.base_version)&&p.base_version>0&&typeof p.page_version==='number'&&Number.isSafeInteger(p.page_version)&&p.page_version===p.base_version+1&&typeof p.document_sha256==='string'&&/^[a-f0-9]{64}$/.test(p.document_sha256)
}

function auxiliaryEventSummary(event: RoomEvent, t: Translate, isFinalizeRequest = false): string {
  if (isFinalizeRequest) return t('finalize.requested')
  if (event.actor_type === 'system' && event.kind === 'summary') {
    return event.payload.owner_checkpoint === true ? t('chat.handoff.timeline') : t('chat.system.summary')
  }
  if (event.actor_type === 'system' && event.kind === 'message') return t('chat.system.message')
  return eventSummary(event, t)
}

function missingSecretConnectionId(event: RoomEvent): number | null {
  if (event.kind !== 'failure' || event.payload.code !== 'missing_secret') return null
  const diagnostic = event.payload.diagnostic
  if (!diagnostic || typeof diagnostic !== 'object') return null
  const connectionId = (diagnostic as Record<string, unknown>).connection_id
  return Number.isSafeInteger(connectionId) && Number(connectionId) > 0 ? Number(connectionId) : null
}

function eventStatus(event: RoomEvent, t: Translate): { label: string; tone: 'neutral' | 'busy' | 'warning' | 'error' | 'success' } | null {
  if (event.kind === 'failure') return { label: t('chat.state.failed'), tone: 'error' }
  if (event.kind === 'summary') return { label: t('chat.state.summary'), tone: 'neutral' }
  if (event.kind !== 'tool_result') return null
  const state = String(event.payload.state || '')
  if (state === 'completed') return { label: t('chat.state.completed'), tone: 'success' }
  if (state === 'waiting' || state === 'started') return { label: t('chat.state.working'), tone: 'busy' }
  if (state === 'uncertain' || state === 'cancelled' || state === 'partial') return { label: t('chat.state.needsCheck'), tone: 'warning' }
  if (state === 'failed' || state === 'denied') return { label: t('chat.state.failed'), tone: 'error' }
  return state ? { label: t('chat.state.changed'), tone: 'neutral' } : null
}

function eventTone(event: RoomEvent): 'blue' | 'teal' | 'amber' | 'violet' | 'neutral' {
  if (event.actor_type === 'user') return 'blue'
  if (event.actor_type === 'tool' || event.actor_type === 'system') return 'neutral'
  return (['teal', 'amber', 'violet'] as const)[Math.abs(event.actor_id || 0) % 3]
}

function ScreenRecordingStatus() {
  const [result, setResult] = useState<{ code: MessageKey; checkedAt?: number } | null>(null)
  const [pending, setPending] = useState(false)
  const { t, formatDateTime } = useI18n()
  const check = async () => {
    setPending(true)
    try {
      const value = await invoke<string>('get_screen_recording_permission_status')
      const code: MessageKey = value === 'granted' ? 'chat.permission.granted' : value === 'not_granted' ?
        'chat.permission.notGranted' : value === 'unsupported' ? 'chat.permission.unsupported' : 'chat.permission.unknown'
      setResult({ code, checkedAt: Date.now() })
    } catch { setResult({ code: 'chat.permission.unknown' }) }
    finally { setPending(false) }
  }
  return <div className="stack stack--tight"><button type="button" className="button button--secondary button--compact" disabled={pending} onClick={() => { void check() }}>{t('chat.permission.recheck')}</button>
    {result && <p role="status" className="metadata">{t(result.code)}{result.checkedAt ? ` · ${formatDateTime(result.checkedAt, { hour: '2-digit', minute: '2-digit', second: '2-digit' })}` : ''}</p>}</div>
}

export default function Chat() {
  const { conversationId } = useParams<{ conversationId: string }>()
  const roomId = Number(conversationId)
  const ownerId = useAuthStore((state) => state.user?.id || 0)
  const authEpoch = useAuthStore((state) => state.epoch)
  return <RoomChat key={`${roomId}:${ownerId}:${authEpoch}`} roomId={roomId} ownerId={ownerId} authEpoch={authEpoch} />
}

type TranscriptReadingOrigin = { seq: number | null; eventId?: string; offset: number; top: number; bottom: boolean; high: number; location: string }
type TranscriptContextReturn = { query: string; actor: TranscriptActorFilter; seq: number; eventId: string; origin: TranscriptReadingOrigin | null; fromLocation: string; atLocation: string | null }
type RoomStopRequest={roomId:number;ownerId:number;epoch:number;generation:number;stopId:string;transportStarted:boolean}
type CloseRoomRequest={target:RoomCloseTarget;epoch:number;generation:number;locationKey:string;transportStarted:boolean}
type RoomCopySnapshot = { text:string; kind:'message'|'link'|'transcript'; seq?:number; eventId?:string; capturedAt:string; transcriptScope?:TranscriptExportScope }

function RoomChat({ roomId, ownerId, authEpoch }: { roomId: number; ownerId: number; authEpoch: number }) {
  const navigate = useNavigate()
  const location = useLocation()
  const callView = useSyncExternalStore(voiceCallController.subscribe, voiceCallController.snapshot)
  const [dictationActive, setDictationActive] = useState(false)
  const dictationActiveRef = useRef(false)
  const [searchParams] = useSearchParams()
  const attentionAction = searchParams.get('action')
  const attentionFailure = searchParams.get('failure')
  const resultRoute = readResultRoute(location.search)
  const resultRouteActive = !searchParams.has('seq') && !attentionAction && !attentionFailure && resultRoute.state !== 'none'
  const cache = useQueryClient()
  const { t, formatDateTime, formatNumber, locale } = useI18n()
  const [input, setInput] = useState(() => readUnsavedComposerDraft(`${ownerId}:${authEpoch}`, roomId) ?? '')
  const [routineSelection,setRoutineSelection]=useState<Routine|null>(null)
  const routineHandoffIntent=useRef(0)
  const [routineHandoffBusy,setRoutineHandoffBusy]=useState(false),[routineHandoffError,setRoutineHandoffError]=useState(false),[routineChanged,setRoutineChanged]=useState(false)
  const composerCharacters = editorTextLength(input), composerTooLong = composerCharacters > 65536
  const [composerExpanded, setComposerExpanded] = useState(false)
  const [composerOptionsOpen, setComposerOptionsOpen] = useState(false)
  const [outputKind,setOutputKind]=useState<'auto'|'text'|'image_generation'|'speech_generation'>('auto')
  const [events, setEvents] = useState<RoomEvent[]>(() => roomEventCursor.events(roomId))
  const reactions=useMemo(()=>foldReactions(events,roomId,ownerId),[events,roomId,ownerId])
  const presentationEvents=useMemo(()=>events.filter(event=>!reactionActivity(event,roomId,ownerId)&&!botReactionActivity(event,roomId)),[events,roomId,ownerId])
  // Event batches provide a new array; composer/menu changes do not. Reuse
  // presentation-only text in this RoomChat lifetime instead of repeatedly
  // scanning every received answer for reasoning/code markers while typing.
  const visibleMessageTexts = useMemo(() => {
    const values = new Map<RoomEvent, string>()
    for (const event of events) {
      if (event.kind !== 'message' || (event.actor_type !== 'user' && event.actor_type !== 'agent') || typeof event.payload.content !== 'string') continue
      values.set(event, event.actor_type === 'agent' ? assistantVisibleMarkdown(event.payload.content) : event.payload.content)
    }
    return values
  }, [events])
  const [feedError, setFeedError] = useState<MessageKey | ''>('')
  const [connectionState, setConnectionState] = useState<ConnectionState>('connecting')
  const [sendError, setSendError] = useState<MessageKey | ''>('')
  const [actionError, setActionError] = useState<MessageKey | ''>('')
  const [settingsError, setSettingsError] = useState<MessageKey | ''>('')
  const [settingsPending, setSettingsPending] = useState(false)
  const [historyState, setHistoryState] = useState<'loading' | 'complete' | 'error'>('loading')
  const historyRetryRef = useRef<(() => void) | null>(null)
  const [transcriptQuery, setTranscriptQuery] = useState('')
  const [transcriptActor, setTranscriptActor] = useState<TranscriptActorFilter>('all')
  const [transcriptExportRange,setTranscriptExportRange]=useState<TranscriptExportScope['range']>('all')
  const [transcriptExportBody,setTranscriptExportBody]=useState<TranscriptExportScope['body']>('visible')
  const [transcriptDownload,setTranscriptDownload]=useState<'requested'|'failed'|null>(null)
  const [transcriptSearchOpen, setTranscriptSearchOpen] = useState(false)
  const transcriptNavContext = useMemo(() => ({}), [transcriptQuery, transcriptActor, location.key])
  const [transcriptTarget, setTranscriptTarget] = useState<{ context: object; eventId: string; seq: number } | null>(null)
  const [transcriptMoveFailure, setTranscriptMoveFailure] = useState<object | null>(null)
  const transcriptInputRef = useRef<HTMLInputElement>(null)
  const transcriptButtonRef = useRef<HTMLButtonElement>(null)
  const transcriptFocusReturn = useRef(false)
  const transcriptImeUntil = useRef(0)
  const transcriptOrigin = useRef<TranscriptReadingOrigin | null>(null)
  const [transcriptContextReturn, setTranscriptContextReturn] = useState<TranscriptContextReturn | null>(null)
  const [transcriptResume, setTranscriptResume] = useState<TranscriptContextReturn | null>(null)
  const transcriptRestore = useRef(false)
  const [managementOpen, setManagementOpen] = useState(false)
  const [sharedAppsAvailable,setSharedAppsAvailable]=useState(false)
  const [closingTarget,setClosingTarget]=useState<RoomCloseTarget|null>(null),closingTargetRef=useRef<RoomCloseTarget|null>(null),closeSendLock=useRef(false)
  const [closeFailure,setCloseFailure]=useState<{target:RoomCloseTarget;phase:'not_sent'|'unknown'}|null>(null)
  const roomViewId = useId()
  const [roomView, setRoomView] = useState<'conversation' | 'results' | 'memory'>(() => resultRouteActive ? 'results' : 'conversation')
  const [memoryOpened, setMemoryOpened] = useState(false)
  const memoryWorkspace = useSyncExternalStore(subscribeMemoryWorkspaces, () => readMemoryWorkspace(`${ownerId}:${authEpoch}`, roomId))
  const memoryHint = memoryWorkspace.request?.status === 'sending' ? t('memory.requestSending') : memoryWorkspace.request?.status === 'unknown' ? t('memory.requestUnknownHint') : memoryWorkspace.editor ? t('memory.draftHint') : '' 
  const [resultsOpened, setResultsOpened] = useState(resultRouteActive)
  const [resultSourceEvent, setResultSourceEvent] = useState<RoomEvent | null>(null)
  const savedFeedPosition = useRef(0)
  const readingIdentity = `${ownerId}:${authEpoch}`
  const readingKey = `room.reading:${roomId}`
  const readingGeneration = useRef(directoryViewGeneration()).current
  const initialReadingPosition = useRef(readDirectoryView<RoomReadingPosition | null>(readingIdentity, readingKey, null))
  const readingPosition = useRef(initialReadingPosition.current)
  const readingRestoreAttempted = useRef(false)
  const readingRestoreInterrupted = useRef(false)
  useLayoutEffect(() => () => {
    // Save the last visible full transcript snapshot, not the next route's resized DOM.
    if (readingPosition.current) writeDirectoryView(readingIdentity, readingKey, readingPosition.current, readingGeneration)
  }, [readingIdentity, readingKey, readingGeneration])
  useEffect(() => {
    if (searchParams.has('seq') || attentionAction || attentionFailure) setRoomView('conversation')
    else if (resultRouteActive) switchRoomView('results')
    else if (searchParams.get('compose') === '1') switchRoomView('conversation')
  }, [location.key, attentionAction, attentionFailure, searchParams])
  const closeManagement = useCallback(() => setManagementOpen(false), [])
  const [messageAction, setMessageAction] = useState<{ seq: number; status: 'copied' | 'linkCopied' | 'copyError' | 'quoteError' | 'replyInserted' | 'replyUnavailable' | 'replyConflict' } | null>(null)
  const [openActionSeq, setOpenActionSeq] = useState<number | null>(null)
  const actionAnchor = useRef<HTMLButtonElement | null>(null)
  const [inspectedRequest, setInspectedRequest] = useState<string | null>(null)
  const [botMemoryOpen, setBotMemoryOpen] = useState(false)
  useEffect(() => { setBotMemoryOpen(false) }, [roomId, ownerId, authEpoch])
  const [highlightedSeq, setHighlightedSeq] = useState<number | null>(null)
  const [deepLinkMissing, setDeepLinkMissing] = useState(false)
  const [attentionMissing, setAttentionMissing] = useState(false)
  const [mentionCaret, setMentionCaret] = useState(0)
  const [mentionOpen, setMentionOpen] = useState(false)
  const [activeMention, setActiveMention] = useState(0)
  const [draftReady, setDraftReady] = useState(false)
  const [draftLoadError, setDraftLoadError] = useState(false)
  const [draftReloadAttempt, setDraftReloadAttempt] = useState(0)
  const [draftConflict, setDraftConflict] = useState<RoomDraft | null>(null)
  const [draftSaveError, setDraftSaveError] = useState(false)
  const [readStateReady, setReadStateReady] = useState(false)
  const [lastReadSeq, setLastReadSeq] = useState(0)
  const [readSaveError, setReadSaveError] = useState(false)
  const [acknowledged, setAcknowledged] = useState<string[]>([])
  const draft = useRef<{ content: string; key: string } | null>(null)
  const draftVersion = useRef(0)
  const draftQueue = useRef<Promise<void>>(Promise.resolve())
  const draftWriteId = useRef(0)
  const draftPendingTail = useRef<{ id: number; scope: string; content: string; replyTo: ReplyReference | null } | null>(null)
  const draftConflictRef = useRef<RoomDraft | null>(null)
  const latestInput = useRef(input)
  const inputEditRevision = useRef(readUnsavedComposerDraft(`${ownerId}:${authEpoch}`, roomId) === null ? 0 : 1)
  const sendLock = useRef(false)
  const roomStop = useSyncExternalStore(subscribeRoomStops, () => readRoomStop(readingIdentity, roomId))
  const stopState = roomStop.phase, roomStopPending = stopState === 'sending' 
  const resultWorkspace = useSyncExternalStore(subscribeResultWorkspaces, () => readResultWorkspace(readingIdentity, roomId))
  const resultHint = resultWorkspaceSending(resultWorkspace) ? t('results.savingShort')
    : resultWorkspaceUncertain(resultWorkspace) ? t('results.attentionShort')
    : Object.values(resultWorkspace.drafts).some(draft => draft.dirty) ? t('results.unsavedShort') : ''
  const deliveryMessages = useSyncExternalStore(subscribePendingMessages, () => pendingRoomMessages(readingIdentity, roomId))
  const replyDeliveryPending = deliveryMessages.some(item => !!item.replyTo && item.status !== 'confirmed' && !item.parked)
  const fileQuestionRef = useRef<FileQuestionHandle>(null)
  const [fileQuestion, setFileQuestion] = useState<FileQuestionState>({selected:false,pending:false,busy:false,preview:false,consentNeeded:false,filename:'',botName:''})
  const composerEditId = useRef(readUnsavedComposerEditId(readingIdentity, roomId) ?? crypto.randomUUID())
  const [clearDraftChoice, setClearDraftChoice] = useState<{ editId: string; locationKey: string } | null>(null)
  useEffect(() => setClearDraftChoice(null), [input, roomView, location.key])
  const settlingDeliveries = useRef(new Set<string>())
  const autoSettledDeliveries = useRef(new Set<string>())
  const [deliveryBusy, setDeliveryBusy] = useState(false)
  const changeComposerInput = (value: string) => {
    setClearDraftChoice(null)
    inputEditRevision.current += 1
    composerEditId.current = crypto.randomUUID()
    latestInput.current = value
    keepUnsavedComposerDraft(readingIdentity, roomId, value, readingGeneration, composerEditId.current)
    setInput(value)
  }
  const [replyTo, setReplyTo] = useState<ReplyReference | null>(null)
  const replyRef = useRef<ReplyReference | null>(null), lastPersistedReply = useRef<ReplyReference | null>(null)
  const replyUnavailable = !!replyTo && historyState === 'complete' && !events.some(event => event.room_id === roomId && event.event_id === replyTo.event_id && event.seq === replyTo.seq && event.kind === 'message' && (event.actor_type === 'user' || event.actor_type === 'agent'))
  const changeReply = (value: ReplyReference | null) => { replyRef.current = value; setReplyTo(value); inputEditRevision.current += 1; composerEditId.current = crypto.randomUUID() }
  const lastPersistedContent = useRef('')
  const mounted = useRef(true)

  const inputRef = useRef<HTMLTextAreaElement>(null)
  const composerComposing = useRef(false), composerImeUntil = useRef(0)
  const inspectorReturnSeq = useRef<number | null>(null)
  const activeMentionRef = useRef<HTMLLIElement>(null)
  const suppressedSelection = useRef<{ value: string; caret: number } | null>(null)
  const endRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const clipboardEvents=useRef(events);clipboardEvents.current=events
  const clipboardVisibleTexts=useRef(visibleMessageTexts);clipboardVisibleTexts.current=visibleMessageTexts
  const clipboardView=useRef({roomView,historyState,location:location.key});clipboardView.current={roomView,historyState,location:location.key}
  const transcriptCopyButton=useRef<HTMLButtonElement>(null)
  const clipboardContext=JSON.stringify([ownerId,authEpoch,roomId,readingGeneration,roomView,location.key])
  const clipboardOwner=()=>{const auth=useAuthStore.getState();return mounted.current&&auth.user?.id===ownerId&&auth.epoch===authEpoch&&directoryViewGeneration()===readingGeneration}
  const messageCopyAllowed=(value:RoomCopySnapshot)=>{
    if(!clipboardOwner()||clipboardView.current.roomView!=='conversation'||!value.seq||!value.eventId)return false
    const event=clipboardEvents.current.find(row=>row.seq===value.seq&&row.event_id===value.eventId&&row.room_id===roomId)
    if(!event||event.kind!=='message'||typeof event.payload.content!=='string')return false
    const visible=memoryImportMessage(event)?.content ?? clipboardVisibleTexts.current.get(event)
    const origin=scrollRef.current?.querySelector<HTMLElement>(`[data-authored-seq="${value.seq}"]`)
    return !!origin?.isConnected&&origin.getClientRects().length>0&&(value.kind==='link' && messageEventHref(roomId,event.seq,event.event_id)===value.text || value.kind==='message'&&visible===value.text)
  }
  const messageCopy=useClipboardText<RoomCopySnapshot>(clipboardContext,messageCopyAllowed)
  const transcriptCopy=useClipboardText<RoomCopySnapshot>(clipboardContext,value=>clipboardOwner()&&value.kind==='transcript'&&!!transcriptCopyButton.current?.isConnected&&transcriptCopyButton.current.getClientRects().length>0)

  const lastReadRef = useRef(0)
  const ackTargetRef = useRef(0)
  const ackPendingRef = useRef(false)
  const timelinePositioned = useRef(false)
  const positionedDeepLink = useRef<string | null>(null)
  const positionedAttention = useRef<string | null>(null)
  const authoredHighRef = useRef(0)
  const atBottomRef = useRef(true)
  const seenAuthoredSeqs = useRef(new Set<number>())
  const conversation = useQuery({ retry: false, queryKey: ['conversation', roomId, ownerId, authEpoch], queryFn: async () => {
    const value = await conversationsApi.get(roomId)
    const auth = useAuthStore.getState()
    if (auth.user?.id !== ownerId || auth.epoch !== authEpoch || value.user_id !== ownerId || value.id !== roomId) throw new Error('Owner changed or invalid room')
    return value
  }, enabled: roomId > 0 && ownerId > 0 })
  const projectId = conversation.data?.project_id
  const project = useQuery({ retry: false, queryKey: ['project', ownerId, authEpoch, projectId], queryFn: async () => {
    const value = await projectsApi.get(projectId!)
    const current = useAuthStore.getState()
    if ((current.user?.id || 0) !== ownerId || current.epoch !== authEpoch || value.user_id !== ownerId || value.id !== projectId) throw new Error('Owner changed or invalid project')
    return value
  }, enabled: typeof projectId === 'number' })
  const ownsRoom = conversation.data?.user_id === ownerId && conversation.data?.id === roomId
  const sharedBinding = useQuery({queryKey:['shared-room-binding',roomId,ownerId,authEpoch],queryFn:()=>readSharedRoomBinding(roomId),enabled:ownsRoom,retry:false,refetchInterval:15000,refetchOnWindowFocus:true})
  useEffect(() => { const changed=()=>{ void sharedBinding.refetch() }; window.addEventListener('gamja-team-session-changed',changed); return ()=>window.removeEventListener('gamja-team-session-changed',changed) }, [roomId,ownerId,authEpoch])
  const bindingConfirmed = sharedBinding.isSuccess && !sharedBinding.isError && (sharedBinding.data?.kind === 'ordinary' || !sharedBinding.isFetching)
  const ordinaryRoom = bindingConfirmed && sharedBinding.data?.kind === 'ordinary'
  const sharedReady = bindingConfirmed && sharedBinding.data?.kind === 'shared' && sharedBinding.data.state === 'ready'
  const roomMetadataReady = conversation.isSuccess && project.isSuccess
  const roomConfirmedInactive = conversation.isSuccess && conversation.data.is_active === false
  const projectConfirmedInactive = project.isSuccess && project.data.is_active === false
  const roomClosed = roomConfirmedInactive || projectConfirmedInactive
  const roomActive = ownsRoom && roomMetadataReady && (ordinaryRoom || sharedReady) && conversation.data?.is_active === true && project.data?.is_active === true
  useEffect(() => { if (bindingConfirmed && !ordinaryRoom) { setRoomView('conversation'); setManagementOpen(false); setOutputKind('auto') } }, [bindingConfirmed, ordinaryRoom])
  const showComposer = ownsRoom && conversation.data?.is_active === true && !roomClosed
  // Stop remains available for a last-known active owned room when metadata GETs fail.
  const roomCanStop = ownsRoom && conversation.data?.is_active === true
  const roomStatusConnection: ConnectionState = !roomMetadataReady && !roomClosed ? 'retrying' : connectionState
  const roomWriteReady = useRef(roomActive)
  roomWriteReady.current = roomActive
  const availabilityFailed = conversation.isError || project.isError
  const availabilityRetryLock = useRef(false)
  const [availabilityRetrying, setAvailabilityRetrying] = useState(false)
  const retryRoomAvailability = async () => {
    const currentOwner = () => { const auth = useAuthStore.getState(); return auth.user?.id === ownerId && auth.epoch === authEpoch }
    if (!currentOwner() || availabilityRetryLock.current) return
    availabilityRetryLock.current = true; setAvailabilityRetrying(true)
    try {
      const result = await conversation.refetch()
      if (!currentOwner()) return
      // A changed project ID starts its own keyed query; do not refetch the old project.
      if (typeof projectId === 'number' && result.data?.project_id === projectId) await project.refetch()
    } catch { /* Query state supplies the visible error; no write or service restart. */ }
    finally { availabilityRetryLock.current = false; if (mounted.current && currentOwner()) setAvailabilityRetrying(false) }
  }
  const agents = useQuery({ queryKey: ['agents', readingIdentity], queryFn: async () => {
    const current = () => { const auth = useAuthStore.getState(); return auth.user?.id === ownerId && auth.epoch === authEpoch && directoryViewGeneration() === readingGeneration }
    if (!current()) throw new Error('Owner changed')
    const value = await agentsApi.list()
    if (!current()) throw new Error('Owner changed')
    return value
  } })
  const botReactions=useMemo(()=>foldBotReactions(events,roomId,ownerId,new Set((agents.data||[]).filter(agent=>agent.user_id===ownerId).map(agent=>agent.id))),[events,roomId,ownerId,agents.data])
  const members = useQuery({ queryKey: ['members', roomId, ownerId, authEpoch], queryFn: () => conversationsApi.listMembers(roomId), enabled: roomId > 0 && !!conversation.data, refetchInterval: roomActive ? 2000 : false })
  const teammates = useQuery({ queryKey: ['teammates', roomId, ownerId, authEpoch], queryFn: () => teammatesApi.list(roomId),
    retry: false, enabled: ownsRoom, refetchInterval: roomActive ? 2000 : false })
  const botMessages = useQuery({ queryKey: ['bot-messages', roomId, ownerId, authEpoch], queryFn: () => botMessagesApi.list(roomId),
    retry: false, enabled: ownsRoom, refetchInterval: roomActive ? 2000 : false })
  const botBatches = useQuery({ queryKey: ['bot-batches', roomId, ownerId, authEpoch], queryFn: () => botBatchesApi.list(roomId),
    retry: false, enabled: ownsRoom, refetchInterval: roomActive ? 2000 : false })
  const botCreations = useQuery({ queryKey: ['bot-creations', roomId, ownerId, authEpoch], queryFn: () => botCreationsApi.list(roomId),
    retry: false, enabled: ownsRoom, refetchInterval: roomActive ? 2000 : false })
  const botConversation = useBotConversationMetadata(roomId, ownsRoom)
  const activity = useQuery({ queryKey: toolActivityKey(roomId, ownerId, authEpoch), queryFn: () => readOwnerToolData(ownerId, authEpoch, async () => { const rows = await toolsApi.activity(roomId); if (rows.some(row => row.room_id !== roomId)) throw new Error('Activity room changed'); return rows }), retry: false, enabled: roomActive, refetchInterval: roomActive ? 2000 : false })
  const broad = useQuery({ queryKey: broadModeKey(ownerId, authEpoch), queryFn: () => readBroadModeForOwner(ownerId, authEpoch), retry: false, enabled: roomActive })
  const broadAction = useBroadModeAction(ownerId, authEpoch)
  useEffect(()=>{routineHandoffIntent.current++;setRoutineHandoffBusy(false);setRoutineHandoffError(false)},[roomId,location.key])
  const draftScope = `${ownerId}:${authEpoch}:${roomId}`
  useEffect(() => { if (conversation.data?.title && ownsRoom) rememberDraftRoomName(readingIdentity, readingGeneration, roomId, conversation.data.title) }, [conversation.data?.title, ownsRoom, readingIdentity, readingGeneration, roomId])

  const persistDraft = useCallback((content: string, overwriteConflict = false, intendedReply: ReplyReference | null = replyRef.current) => {
    // Preserve over-limit input locally; do not queue a write the draft API cannot accept.
    if (editorTextLength(content) > 65536) return Promise.reject(new Error('Draft input exceeds character limit'))
    const pending = { id: ++draftWriteId.current, scope: draftScope, content, replyTo: intendedReply, editId: composerEditId.current, location:location.key }
    const draftViewCurrent=()=>mounted.current&&clipboardView.current.location===pending.location&&useAuthStore.getState().user?.id===ownerId&&useAuthStore.getState().epoch===authEpoch
    draftPendingTail.current = pending
    const operation = draftQueue.current.then(async () => {
      if (!draftViewCurrent()) throw new Error('Draft view changed')
      if (!roomWriteReady.current) throw new Error('Room status is not ready')
      if (draftConflictRef.current && !overwriteConflict) throw new Error('Draft conflict requires a choice')
      let saved: RoomDraft
      try {
        saved = await draftsApi.put(roomId, content, draftVersion.current, intendedReply)
      } catch (error) {
        const status = (error as { response?: { status?: number } }).response?.status
        if (status !== 409) throw error
        const current = await draftsApi.get(roomId)
        if (!draftViewCurrent()) throw new Error('Draft view changed')
        draftVersion.current = current.version
        draft.current = current.idempotency_key ? { content: current.content, key: current.idempotency_key } : null
        if (current.content === content && sameReply(current.reply_to, intendedReply)) saved = current
        else {
          draftConflictRef.current = current
          if (mounted.current) setDraftConflict(current)
          throw error
        }
      }
      if (!draftViewCurrent()) throw new Error('Draft view changed')
      if (saved.content !== content || !sameReply(saved.reply_to, intendedReply)) throw new Error('Draft reply changed')
      draftVersion.current = saved.version
      draft.current = saved.idempotency_key ? { content: saved.content, key: saved.idempotency_key } : null
      lastPersistedContent.current = saved.content
      lastPersistedReply.current = saved.reply_to ?? null
      acknowledgeComposerDraft(readingIdentity, roomId, saved.content, readingGeneration, pending.editId)
      draftConflictRef.current = null
      if (mounted.current) setDraftConflict(null)
      if (mounted.current) setDraftSaveError(false)
      return saved
    })
    const tracked = operation.finally(() => {
      const tail = draftPendingTail.current
      if (tail?.id === pending.id && tail.scope === pending.scope) draftPendingTail.current = null
    })
    draftQueue.current = tracked.then(() => undefined, () => undefined)
    return tracked
  }, [roomId, ownerId, authEpoch, draftScope, location.key])

  const clearSentDraft = useCallback((sentVersion: number) => {
    const originalLocation=location.key
    const draftViewCurrent=()=>mounted.current&&clipboardView.current.location===originalLocation&&useAuthStore.getState().user?.id===ownerId&&useAuthStore.getState().epoch===authEpoch
    const operation = draftQueue.current.then(async () => {
      if (!draftViewCurrent()) throw new Error('Draft view changed')
      if (!roomWriteReady.current) throw new Error('Room status is not ready')
      try {
        await draftsApi.clear(roomId, sentVersion)
        if (!draftViewCurrent()) throw new Error('Draft view changed')
        acknowledgeComposerDraft(readingIdentity, roomId, '', readingGeneration)
        if (draftVersion.current === sentVersion) {
          draftVersion.current = sentVersion + 1
          draft.current = null
          lastPersistedContent.current = ''
          lastPersistedReply.current = null
        }
      } catch (error) {
        if ((error as { response?: { status?: number } }).response?.status !== 409) throw error
      }
    })
    draftQueue.current = operation.then(() => undefined, () => undefined)
    return operation
  }, [roomId, ownerId, authEpoch, location.key])

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  useEffect(() => {
    if (!roomActive) {
      setDraftReady(!!conversation.data?.id && project.isSuccess)
      setDraftLoadError(false)
      return
    }
    let active = true
    const currentOwner = () => {
      const auth = useAuthStore.getState()
      return auth.epoch === authEpoch && auth.user?.id === ownerId
    }
    setDraftReady(false)
    setDraftLoadError(false)
    draftVersion.current = 0
    draft.current = null
    lastPersistedContent.current = ''
    lastPersistedReply.current = null
    replyRef.current = null; setReplyTo(null)
    draftConflictRef.current = null
    setDraftConflict(null)
    setDraftSaveError(false)
    void draftsApi.get(roomId).then((saved) => {
      if (!active || !currentOwner()) return
      if (saved.reply_to != null && !validReply(saved.reply_to)) throw new Error('Invalid draft reply')
      draftVersion.current = saved.version
      draft.current = saved.idempotency_key ? { content: saved.content, key: saved.idempotency_key } : null
      lastPersistedContent.current = saved.content
      lastPersistedReply.current = saved.reply_to ?? null
      const original = pendingRoomMessages(readingIdentity, roomId).find(item => item.key === saved.idempotency_key && item.content === saved.content && sameReply(item.replyTo, saved.reply_to))
      if (inputEditRevision.current === 0) {
        replyRef.current = saved.reply_to ?? null; setReplyTo(replyRef.current)
        latestInput.current = saved.content
        if (original) composerEditId.current = original.sourceEditId
        setInput(saved.content)
      } else if ((saved.content && saved.content !== latestInput.current) || !sameReply(saved.reply_to, replyRef.current)) {
        // Typing (including clearing) while loading/retrying is an explicit local edit.
        // Do not silently replace it or overwrite a different stored draft.
        draftConflictRef.current = saved
        setDraftConflict(saved)
      }
      if (!original || original.sourceEditId === composerEditId.current) acknowledgeComposerDraft(readingIdentity, roomId, saved.content, readingGeneration, composerEditId.current)
      setDraftReady(true)
    }).catch(() => {
      if (active && currentOwner()) { setDraftLoadError(true); setDraftReady(false) }
    })
    return () => { active = false }
  }, [roomId, authEpoch, ownerId, conversation.data?.id, project.isSuccess, roomActive, draftReloadAttempt])

  useEffect(() => {
    if (!draftReady || !roomActive || draftConflictRef.current || composerTooLong) return
    latestInput.current = input
    const tail = draftPendingTail.current
    const eventualContent = tail?.scope === draftScope ? tail.content : lastPersistedContent.current
    const eventualReply = tail?.scope === draftScope ? tail.replyTo : lastPersistedReply.current
    if (input === eventualContent && sameReply(replyTo, eventualReply)) return
    const timer = window.setTimeout(() => { void persistDraft(input).catch(() => {
      if (mounted.current && !draftConflictRef.current && useAuthStore.getState().epoch === authEpoch) setDraftSaveError(true)
    }) }, 500)
    return () => window.clearTimeout(timer)
  }, [draftReady, input, replyTo, persistDraft, roomActive, draftScope, draftConflict, composerTooLong])

  useEffect(() => () => {
    if (draftReady && roomActive && roomWriteReady.current && (latestInput.current !== lastPersistedContent.current || !sameReply(replyRef.current, lastPersistedReply.current)) && useAuthStore.getState().epoch === authEpoch)
      void persistDraft(latestInput.current).catch(() => {})
  }, [draftReady, persistDraft, authEpoch, roomActive])

  useLayoutEffect(() => {
    const field = inputRef.current
    if (!showComposer || roomView !== 'conversation' || !field?.isConnected || !field.getClientRects().length) return
    const resize = () => {
      if (!field.isConnected || !field.getClientRects().length) return
      const top = field.scrollTop, cap = Number.parseFloat(getComputedStyle(field).maxHeight)
      field.style.height = 'auto'
      field.style.height = `${Math.min(field.scrollHeight, Number.isFinite(cap) ? cap : 180)}px`
      field.scrollTop = top
    }
    resize()
    if (typeof ResizeObserver === 'undefined') return
    let width = field.getBoundingClientRect().width
    const observer = new ResizeObserver(() => {
      const next = field.getBoundingClientRect().width
      // Height writes must not trigger their own resize loop.
      if (next <= 0 || Math.abs(next - width) < 0.5) return
      width = next; resize()
    })
    observer.observe(field)
    return () => observer.disconnect()
  }, [input, composerExpanded, roomView, showComposer])

  useEffect(() => {
    if (!roomId || !conversation.data?.id || !ownsRoom) return
    let disposed = false
    let socket: WebSocket | undefined
    let timer: ReturnType<typeof setTimeout> | undefined
    let connecting = false
    let catchUpFlight: Promise<void> | null = null
    const currentOwner = () => {
      const auth = useAuthStore.getState()
      return auth.epoch === authEpoch && auth.user?.id === ownerId
    }
    setEvents(roomEventCursor.events(roomId))
    setFeedError('')
    setConnectionState('connecting')
    setHistoryState('loading')
    const apply = (batch: RoomEvent[]) => {
      if (disposed || !currentOwner()) return
      roomEventCursor.add(roomId, batch)
      setEvents(roomEventCursor.events(roomId))
    }
    // Socket gaps and connection recovery share one cursor reader for this lifecycle.
    const catchUp = (): Promise<void> => {
      if (disposed || !currentOwner()) return Promise.resolve()
      if (catchUpFlight) return catchUpFlight
      setHistoryState('loading')
      const flight = (async () => {
        try {
          let batch: RoomEvent[]
          do {
            if (disposed || !currentOwner()) return
            const after = roomEventCursor.afterSeq(roomId)
            batch = await eventsApi.list(roomId, after, 100)
            if (disposed || !currentOwner()) return
            if (batch.some((item, index) => item.room_id !== roomId || item.seq !== after + index + 1)) throw new Error('Non-contiguous room history')
            apply(batch)
            if (batch.length > 0 && roomEventCursor.afterSeq(roomId) <= after) throw new Error('Room history cursor did not advance')
          } while (batch.length === 100)
          if (!disposed && currentOwner()) { setFeedError(''); setHistoryState('complete') }
        } catch (error) {
          if (!disposed && currentOwner()) setHistoryState('error')
          throw error
        }
      })()
      catchUpFlight = flight
      // Both settlement paths release the same flight; no detached rejected finally promise.
      void flight.then(() => { if (catchUpFlight === flight) catchUpFlight = null }, () => { if (catchUpFlight === flight) catchUpFlight = null })
      return flight
    }
    const scheduleReconnect = (delay: number) => {
      if (disposed || !currentOwner()) return
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => { timer = undefined; void connect() }, delay)
    }
    const connect = async () => {
      if (disposed || !currentOwner() || connecting) return
      connecting = true
      if (timer) { clearTimeout(timer); timer = undefined }
      try {
        await catchUp()
        if (disposed || !currentOwner()) return
        if (!roomActive) { setConnectionState('live'); return }
        const next = await openRoomSocket(roomId, roomEventCursor.afterSeq(roomId))
        if (disposed || !currentOwner()) { next.close(); return }
        socket = next
        let failed = false
        next.onopen = () => {
          if (!failed && !disposed && currentOwner() && socket === next) setConnectionState('live')
        }
        next.onmessage = (message) => {
          if (disposed || !currentOwner() || socket !== next) return
          try {
            const wire = JSON.parse(message.data) as RoomEvent & { type?: string }
            if (wire.type !== 'event' || wire.room_id !== roomId) return
            if (wire.seq > roomEventCursor.afterSeq(roomId) + 1) {
              void catchUp().catch(() => { if (!disposed && currentOwner() && socket === next) setFeedError('chat.connection.gap') })
            } else apply([wire])
            cache.invalidateQueries({ queryKey: ['activity', roomId] })
            cache.invalidateQueries({ queryKey: ['members', roomId] })
          } catch { if (!disposed && currentOwner()) setFeedError('chat.connection.invalidEvent') }
        }
        next.onerror = () => {
          if (!disposed && currentOwner() && socket === next) {
            failed = true
            setConnectionState('retrying')
            next.close()
          }
        }
        next.onclose = () => {
          if (!disposed && currentOwner() && socket === next) {
            failed = true
            setConnectionState('retrying')
            scheduleReconnect(1000)
          }
        }
      } catch {
        if (!disposed && currentOwner()) {
          setHistoryState('error')
          setFeedError('chat.connection.interrupted')
          setConnectionState('retrying')
          scheduleReconnect(1500)
        }
      } finally { connecting = false }
    }
    const retry = () => {
      if (disposed || !currentOwner() || connecting) return
      if (timer) { clearTimeout(timer); timer = undefined }
      // Invalidate old socket callbacks before closing; replace transport, never resend work.
      const previous = socket
      socket = undefined
      previous?.close()
      setConnectionState('connecting')
      void connect()
    }
    historyRetryRef.current = retry
    void connect()
    return () => {
      disposed = true
      if (historyRetryRef.current === retry) historyRetryRef.current = null
      if (timer) clearTimeout(timer)
      socket?.close()
    }
    // Metadata refreshes must not reset the reader, draft, or active connection.
  }, [roomId, cache, authEpoch, ownerId, conversation.data?.id, ownsRoom, roomActive])

  useEffect(() => {
    let active = true
    setReadStateReady(false)
    setReadSaveError(false)
    lastReadRef.current = 0
    ackTargetRef.current = 0
    timelinePositioned.current = false
    authoredHighRef.current = 0
    seenAuthoredSeqs.current.clear()
    void readStatesApi.get(roomId).then((state) => {
      if (!active || useAuthStore.getState().epoch !== authEpoch) return
      lastReadRef.current = state.last_read_seq
      ackTargetRef.current = state.last_read_seq
      setLastReadSeq(state.last_read_seq)
      setReadStateReady(true)
    }).catch(() => {})
    return () => { active = false }
  }, [roomId, authEpoch])

  const requestReadAck = useCallback((seq: number) => {
    if (managementOpen || attentionAction || attentionFailure || !readStateReady || seq <= lastReadRef.current || useAuthStore.getState().epoch !== authEpoch) return
    ackTargetRef.current = Math.max(ackTargetRef.current, seq)
    if (ackPendingRef.current) return
    ackPendingRef.current = true
    const run = async () => {
      try {
        while (ackTargetRef.current > lastReadRef.current) {
          const target = ackTargetRef.current
          const state = await readStatesApi.put(roomId, target)
          if (useAuthStore.getState().epoch !== authEpoch) return
          lastReadRef.current = Math.max(lastReadRef.current, state.last_read_seq)
          setLastReadSeq(lastReadRef.current)
          setReadSaveError(false)
        }
      } catch {
        if (useAuthStore.getState().epoch === authEpoch) setReadSaveError(true)
      } finally { ackPendingRef.current = false }
    }
    void run()
  }, [roomId, authEpoch, readStateReady, attentionAction, attentionFailure])

  const recipientGroups = buildRecipientGroups(agents.data || [], members.data || [])
  const contextSourceVersion = `${memoryWorkspace.changeId}:${historyState}:${events.length}:${events.at(-1)?.seq || 0}:${events.at(-1)?.event_id || ''}`
  const contextRecipientIdentity = (members.data || []).filter((member) => member.is_active).map((member) => {
    const agent = (agents.data || []).find((candidate) => candidate.id === member.agent_id)
    return `${member.agent_id}:${member.paused === true}:${member.run_state || ''}:${agent?.is_active === true}:${agent?.connection_id || 0}:${agent?.model_id || ''}:${agent?.updated_at || ''}`
  }).sort().join('|')
  const mentionToken = mentionOpen ? mentionAtCaret(input, mentionCaret) : null
  const mentionSuggestions = mentionToken ? filterRecipientGroups(recipientGroups, mentionToken.query) : []
  const activeMentionIndex = mentionSuggestions.length ? Math.min(activeMention, mentionSuggestions.length - 1) : -1
  const mentionIdentity = (group: (typeof recipientGroups)[number]) => t(group.agentIds.length > 1 ? 'chat.routing.groupIdentity' : 'chat.routing.recipientIds', {
    count: group.agentIds.length, ids: group.agentIds.map(id => `#${id}`).join(', '),
  })
  const mentionedRecipients = recipientGroups.filter((group) => group.matches(input))
  const hasMentionToken = /(^|[^\p{L}\p{N}_])@\S+/u.test(routableMentionContent(input))
  const unmatchedMention = ordinaryRoom && hasMentionToken && mentionedRecipients.length === 0
  const mentionListId = `room-${roomId}-mention-suggestions`
  useEffect(() => {
    if (mentionOpen && activeMentionIndex >= 0) activeMentionRef.current?.scrollIntoView({ block: 'nearest' })
  }, [mentionOpen, activeMentionIndex])
  const updateMention = (value: string, caret: number | null, resetActive = true) => {
    const nextCaret = caret ?? value.length
    setMentionCaret(nextCaret)
    setMentionOpen(!!mentionAtCaret(value, nextCaret))
    if (resetActive) setActiveMention(0)
  }
  const selectMention = (group: RecipientGroup) => {
    if (!mentionToken) return
    const next = replaceMentionAtCaret(input, mentionToken, group.name)
    changeComposerInput(next.content)
    setMentionCaret(next.caret)
    setMentionOpen(false)
    setActiveMention(0)
    draft.current = null
    suppressedSelection.current = { value: next.content, caret: next.caret }
    queueMicrotask(() => { inputRef.current?.focus(); inputRef.current?.setSelectionRange(next.caret, next.caret) })
  }
  const updateComposerValue = (element: HTMLTextAreaElement) => {
    suppressedSelection.current = null
    changeComposerInput(element.value)
    updateMention(element.value, element.selectionStart)
    if (draft.current?.content !== element.value) draft.current = null
  }
  const handleComposerKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Composition owns these keys, including the final Enter after compositionend.
    // Leave native IME commitment alone; do not run mention/send shortcuts.
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229 || composerComposing.current || performance.now() < composerImeUntil.current) return
    suppressedSelection.current = null
    if (mentionOpen && mentionToken) {
      if (event.key === 'Escape') { event.preventDefault(); setMentionOpen(false); return }
      if (mentionSuggestions.length) {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault()
          const step = event.key === 'ArrowDown' ? 1 : -1
          setActiveMention((current) => (current + step + mentionSuggestions.length) % mentionSuggestions.length)
          return
        }
        if (event.key === 'Enter' || event.key === 'Tab') {
          event.preventDefault()
          selectMention(mentionSuggestions[activeMentionIndex])
          return
        }
      }
    }
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault()
      event.currentTarget.form?.requestSubmit()
    }
  }

  const settleRejectedRoutine=async(item:PendingRoomMessage,originalLocation=location.key)=>{
    const stillHere=()=>clipboardOwner()&&clipboardView.current.location===originalLocation&&clipboardView.current.roomView==='conversation'
    if(!item.routineRef||item.roomId!==roomId||!stillHere()||deliveryBusy)throw Error('Routine rejection recovery unavailable')
    setDeliveryBusy(true)
    try{
      keepUnsavedComposerDraft(readingIdentity,roomId,latestInput.current,readingGeneration,composerEditId.current)
      await clearSentDraft(item.draftVersion)
      if(!stillHere())throw Error('View changed')
      const saved=await persistDraft(latestInput.current)
      if(!stillHere()||saved.idempotency_key===item.key)throw Error('Routine draft key not renewed')
      rejectPendingMessage(readingIdentity,item.roomId,item.key,readingGeneration)
      setRoutineSelection(current=>sameRoutineReference(item.routineRef,current?{agent_id:current.agent_id,routine_id:current.routine_id,revision:current.revision}:null)?null:current)
      setRoutineChanged(true);setSendError('')
    }finally{if(clipboardOwner())setDeliveryBusy(false)}
  }
  const send = useMutation({
    retry: false,
    mutationFn: async (sent: { content: string; sourceContent: string; sourceEditId: string; sentRoomId: number; sentOwnerId: number; sentEpoch: number; sentLocation:string; outputKind: 'auto' | 'text' | 'image_generation' | 'speech_generation'; transportStarted: boolean; stopRevision: number; replyTo?: ReplyReference | null; routineRef?:RoutineReference|null; retryKey?: string; pendingKey?: string }) => {
      const currentOwner = () => { const auth = useAuthStore.getState(); return auth.epoch === sent.sentEpoch && auth.user?.id === sent.sentOwnerId }
      if (!currentOwner() || !roomSendAllowed(readingIdentity, sent.sentRoomId, sent.stopRevision)) throw new Error('Owner or stop state changed')
      const beforeBinding=await readSharedRoomBinding(sent.sentRoomId)
      if (!currentOwner() || !bindingConfirmed || (ordinaryRoom ? beforeBinding.kind!=='ordinary' : !sharedReady || beforeBinding.kind!=='shared' || beforeBinding.state!=='ready' || sharedBinding.data?.kind!=='shared' || sharedBinding.data.state!=='ready' || beforeBinding.publication_id!==sharedBinding.data.publication_id || beforeBinding.session_generation!==sharedBinding.data.session_generation)) throw new Error('Room access changed')
      let envelope: PendingRoomMessage
      if (sent.retryKey) {
        const items = pendingRoomMessages(readingIdentity, roomId)
        const prior = items.find(item => item.key === sent.retryKey)
        if (!prior || prior.status !== 'unknown' || prior.mismatch || items.some(item => item.key !== prior.key && (item.status === 'sending' || !item.parked))) throw new Error('Original send unavailable')
        envelope = prior
        updatePendingMessage(readingIdentity, roomId, prior.key, { status: 'sending' }, readingGeneration)
      } else {
        const saved = await persistDraft(sent.content, false, sent.replyTo ?? null)
        if (!currentOwner() || !roomSendAllowed(readingIdentity, sent.sentRoomId, sent.stopRevision)) throw new Error('Owner or stop state changed during draft preparation')
        if (!saved.idempotency_key) throw new Error('Draft idempotency key unavailable')
        const latestBinding=await readSharedRoomBinding(sent.sentRoomId)
        if (!currentOwner() || !roomSendAllowed(readingIdentity,sent.sentRoomId,sent.stopRevision) || (ordinaryRoom ? latestBinding.kind!=='ordinary' : latestBinding.kind!=='shared' || latestBinding.state!=='ready' || sharedBinding.data?.kind!=='shared' || sharedBinding.data.state!=='ready' || latestBinding.publication_id!==sharedBinding.data.publication_id || latestBinding.session_generation!==sharedBinding.data.session_generation)) throw new Error('Room access changed')
        envelope = { identity: readingIdentity, roomId, key: saved.idempotency_key, content: sent.content, sourceContent: sent.sourceContent,
          sourceEditId: sent.sourceEditId, outputKind: sent.outputKind, routineRef:sent.routineRef?Object.freeze({...sent.routineRef}):null, replyTo: sent.replyTo ? Object.freeze({ ...sent.replyTo }) : null, stopRevision: sent.stopRevision, draftVersion: saved.version, afterSeq: roomEventCursor.afterSeq(roomId), status: 'sending', parked: false }
        freezePendingMessage(envelope, readingGeneration)
      }
      sent.pendingKey = envelope.key
      sent.transportStarted = true
      try {
        const event = envelope.routineRef ? await eventsApi.send(envelope.roomId, envelope.content, envelope.key, envelope.outputKind, envelope.replyTo ?? null,envelope.routineRef) : await eventsApi.send(envelope.roomId, envelope.content, envelope.key, envelope.outputKind, envelope.replyTo ?? null)
        if (currentOwner()) {
          if (matchesPendingMessage(envelope, event, sent.sentOwnerId)) {
            roomEventCursor.add(envelope.roomId, [event])
            updatePendingMessage(readingIdentity, envelope.roomId, envelope.key, { status: 'confirmed', receipt: event }, readingGeneration)
          } else updatePendingMessage(readingIdentity, envelope.roomId, envelope.key, { status: 'unknown', mismatch: true }, readingGeneration)
        }
        return { event, key: envelope.key }
      } catch (error) {
        const live = pendingRoomMessages(readingIdentity, envelope.roomId).find(item => item.key === envelope.key)
        const response = (error as { response?: { status?: number; data?: { detail?: unknown } } }).response
        const detail = response?.data?.detail
        const mismatch = !!envelope.replyTo && response?.status === 409 && (detail === 'Idempotency key already used for a different event' || !!detail && typeof detail === 'object' && (detail as { code?: string }).code === 'reply_draft_changed')
        if (currentOwner() && live && live.status !== 'confirmed') updatePendingMessage(readingIdentity, envelope.roomId, envelope.key, { status: 'unknown', ...(mismatch ? { mismatch: true } : {}) }, readingGeneration)
        throw error
      }
    },
    onSuccess: (result, sent) => {
      const auth = useAuthStore.getState()
      if (sent.sentEpoch !== auth.epoch || sent.sentOwnerId !== auth.user?.id) return
      const envelope = pendingRoomMessages(readingIdentity, sent.sentRoomId).find(item => item.key === result.key)
      if (!envelope) return
      if (!matchesPendingMessage(envelope, result.event, sent.sentOwnerId)) {
        updatePendingMessage(readingIdentity, sent.sentRoomId, result.key, { status: 'unknown', mismatch: true }, readingGeneration)
        return
      }
      roomEventCursor.add(sent.sentRoomId, [result.event])
      if (mounted.current&&sent.sentRoomId===roomId&&clipboardView.current.location===sent.sentLocation) setEvents(roomEventCursor.events(sent.sentRoomId))
      updatePendingMessage(readingIdentity, sent.sentRoomId, result.key, { status: 'confirmed', receipt: result.event }, readingGeneration)
      if (mounted.current&&sent.sentRoomId===roomId&&clipboardView.current.location===sent.sentLocation) setSendError('')
    },
    onError: (error, sent) => {
      const auth = useAuthStore.getState()
      if (sent.sentEpoch !== auth.epoch || sent.sentOwnerId !== auth.user?.id) return
      const envelope = sent.pendingKey ? pendingRoomMessages(readingIdentity, sent.sentRoomId).find(item => item.key === sent.pendingKey) : null
      if (sent.pendingKey && (!envelope || envelope.status === 'confirmed')) return
      const conflict=(error as {response?:{status?:number;data?:{detail?:{code?:string;fenced?:boolean}}}}).response
      if(envelope?.routineRef&&conflict?.data?.detail?.fenced===true&&[404,409].includes(conflict.status||0)&&['routine_not_found','routine_agent_not_found','routine_reference_changed','routine_reference_room_changed'].includes(conflict.data.detail.code||'')){void settleRejectedRoutine(envelope,sent.sentLocation).catch(()=>{if(clipboardOwner()&&sent.sentRoomId===roomId&&clipboardView.current.location===sent.sentLocation)setSendError('chat.sendUnconfirmed')});return}
      if (envelope) updatePendingMessage(readingIdentity, sent.sentRoomId, envelope.key, { status: 'unknown' }, readingGeneration)
      if (mounted.current&&sent.sentRoomId===roomId&&clipboardView.current.location===sent.sentLocation) {
        const code = (error as { response?: { data?: { detail?: { code?: string } } } })?.response?.data?.detail?.code
        setSendError(code === 'model_output_unavailable' ? 'localModels.outputBlocked' : sent.transportStarted ? envelope?.replyTo ? 'reply.unknown' : 'chat.sendUnconfirmed' : 'chat.draft.sendBlocked')
      }
    },
    onSettled: () => { sendLock.current = false },
  })
  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (composerComposing.current || composerTooLong || sharedReady && (!!replyRef.current || !['auto','text'].includes(outputKind))) return
    const content = input.trim()
    if (readRoomStop(readingIdentity, roomId).phase === 'sending' || !roomActive || !draftReady || draftLoadError || sendLock.current || deliveryBusy || blocksNewMessage(deliveryMessages) || send.isPending || draftConflictRef.current) return
    if (fileQuestion.selected) {
      if (replyRef.current) return
      if (fileQuestion.busy || (!fileQuestion.pending && (!content || Array.from(input).length > 4000 || (outputKind !== 'auto' && outputKind !== 'text')))) return
      fileQuestionRef.current?.submit()
      return
    }
    if (!content || unmatchedMention || replyUnavailable || routineChanged || !!replyTo && historyState !== 'complete'||routineSelection&&(!!replyTo||outputKind!=='auto'||fileQuestion.selected)) return
    sendLock.current = true
    send.mutate({ content, sourceContent: input, sourceEditId: composerEditId.current, sentRoomId: roomId, sentOwnerId: ownerId, sentEpoch: authEpoch, sentLocation:location.key, outputKind, routineRef:routineSelection?{agent_id:routineSelection.agent_id,routine_id:routineSelection.routine_id,revision:routineSelection.revision}:null, replyTo: replyRef.current, stopRevision: readRoomStop(readingIdentity, roomId).revision, transportStarted: false })
  }
  const retryOriginalMessage = (item: PendingRoomMessage) => {
    if (item.replyTo && (item.stopRevision === undefined || !roomSendAllowed(readingIdentity, item.roomId, item.stopRevision))) return
    const auth = useAuthStore.getState()
    if (!ordinaryRoom || readRoomStop(readingIdentity, roomId).phase === 'sending' || !roomActive || sendLock.current || deliveryBusy || send.isPending || auth.user?.id !== ownerId || auth.epoch !== authEpoch) return
    sendLock.current = true
    send.mutate({ content: item.content, sourceContent: item.sourceContent, sourceEditId: item.sourceEditId,
      sentRoomId: roomId, sentOwnerId: ownerId, sentEpoch: authEpoch, sentLocation:location.key, outputKind: item.outputKind, routineRef:item.routineRef??null, replyTo: item.replyTo ?? null, stopRevision: readRoomStop(readingIdentity, roomId).revision, transportStarted: false, retryKey: item.key })
  }
  const finishConfirmedMessage = async (item: PendingRoomMessage) => {
    const originalLocation=location.key
    const stillHere=()=>clipboardOwner()&&item.roomId===roomId&&clipboardView.current.location===originalLocation
    if (!stillHere() || settlingDeliveries.current.has(item.key)) return
    settlingDeliveries.current.add(item.key)
    updatePendingMessage(readingIdentity, roomId, item.key, { settlementFailed: false }, readingGeneration)
    try {
      const auth = useAuthStore.getState()
      if (!mounted.current || auth.user?.id !== ownerId || auth.epoch !== authEpoch) return
      if (!item.parked && composerEditId.current === item.sourceEditId
        && (latestInput.current === item.sourceContent || latestInput.current === item.content) && sameReply(replyRef.current, item.replyTo)) { changeComposerInput(''); changeReply(null);if(item.routineRef)setRoutineSelection(current=>sameRoutineReference(item.routineRef,current?{agent_id:current.agent_id,routine_id:current.routine_id,revision:current.revision}:null)?null:current) }
      else keepUnsavedComposerDraft(readingIdentity, roomId, latestInput.current, readingGeneration, composerEditId.current)
      await clearSentDraft(item.draftVersion)
      const current = useAuthStore.getState()
      if (!stillHere() || current.user?.id !== ownerId || current.epoch !== authEpoch) return
      const saved = await persistDraft(latestInput.current)
      if (saved.idempotency_key === item.key) throw new Error('Sent key still bound to draft')
      consumeConfirmedMessage(readingIdentity, roomId, item.key, readingGeneration)
      if (mounted.current) setSendError('')
    } catch {
      updatePendingMessage(readingIdentity, roomId, item.key, { settlementFailed: true }, readingGeneration)
    } finally { settlingDeliveries.current.delete(item.key) }
  }
  useEffect(() => {
    for (const item of deliveryMessages) {
      const found = events.find(event => event.idempotency_key === item.key)
      if (item.status !== 'confirmed' && found && (!item.mismatch || matchesPendingMessage(item, found, ownerId))) {
        updatePendingMessage(readingIdentity, roomId, item.key, matchesPendingMessage(item, found, ownerId)
          ? { status: 'confirmed', receipt: found } : { mismatch: true, receipt: found }, readingGeneration)
      }
      if (item.status === 'confirmed' && roomActive && draftReady && !send.isPending && !deliveryBusy && !autoSettledDeliveries.current.has(item.key)) {
        autoSettledDeliveries.current.add(item.key)
        void finishConfirmedMessage(item)
      }
    }
  }, [deliveryMessages, events, draftReady, roomActive, send.isPending, deliveryBusy, ownerId, readingIdentity, roomId, readingGeneration])
  const parkUnconfirmedMessage = async (item: PendingRoomMessage) => {
    const auth = useAuthStore.getState()
    if (sendLock.current || deliveryBusy || auth.user?.id !== ownerId || auth.epoch !== authEpoch) throw new Error('Send busy')
    const live = pendingRoomMessages(readingIdentity, roomId).find(record => record.key === item.key)
    if (!live || live.status !== 'unknown' || live.parked) throw new Error('Original send state changed')
    sendLock.current = true; setDeliveryBusy(true)
    try {
      keepUnsavedComposerDraft(readingIdentity, roomId, latestInput.current, readingGeneration, composerEditId.current)
      await clearSentDraft(item.draftVersion)
      if (!mounted.current) throw new Error('View changed')
      const saved = await persistDraft(latestInput.current)
      if (saved.idempotency_key === item.key) throw new Error('Original key must remain separate')
      updatePendingMessage(readingIdentity, roomId, item.key, { parked: true }, readingGeneration)
      if (mounted.current) setSendError('')
    } finally { sendLock.current = false; if (mounted.current) setDeliveryBusy(false) }
  }
  const stopOwnerCurrent = (sent: RoomStopRequest) => {
    const auth = useAuthStore.getState()
    return !!auth.token && auth.user?.id === sent.ownerId && auth.epoch === sent.epoch && directoryViewGeneration() === sent.generation
  }
  const cancel = useMutation({ retry: false,
    mutationFn: async (sent: RoomStopRequest) => {
      if (!mounted.current || !stopOwnerCurrent(sent) || sent.roomId !== roomId || !roomCanStop) throw new Error('Room stop not sent')
      sent.transportStarted = true
      await conversationsApi.cancel(sent.roomId, id => { if (stopOwnerCurrent(sent)) attachScopeStop(readingIdentity, sent.generation, sent.roomId, sent.stopId, id) })
    },
    onSuccess: (_value, sent) => {
      if (!stopOwnerCurrent(sent) || sent.roomId !== roomId) return
      settleRoomStop(readingIdentity, sent.generation, sent.roomId, sent.stopId, 'accepted')
      if (mounted.current) setActionError('')
      void cache.invalidateQueries({ queryKey: toolActivityKey(sent.roomId, sent.ownerId, sent.epoch) })
      void cache.invalidateQueries({ queryKey: ['members', sent.roomId, sent.ownerId, sent.epoch] })
      void cache.invalidateQueries({ queryKey: ['desktop-lease'] })
      void cache.invalidateQueries({ queryKey: ['root-tool-actions', sent.ownerId, sent.epoch] })
    },
    onError: (_failure, sent) => {
      if (stopOwnerCurrent(sent) && sent.roomId === roomId) settleRoomStop(readingIdentity, sent.generation, sent.roomId, sent.stopId, sent.transportStarted ? 'unknown' : 'notSent')
    },
  })
  const requestStop = () => {
    if (!clipboardOwner() || !roomCanStop) return
    const record = beginRoomStop(readingIdentity, readingGeneration, roomId)
    if (!record?.id) return
    setActionError('')
    cancel.mutate({ roomId, ownerId, epoch: authEpoch, generation: readingGeneration, stopId: record.id, transportStarted: false })
  }
  const closeOwnerCurrent=(sent:CloseRoomRequest)=>{const auth=useAuthStore.getState();return !!auth.token&&auth.user?.id===sent.target.ownerId&&auth.epoch===sent.epoch&&directoryViewGeneration()===sent.generation}
  const close = useMutation({retry:false,
    mutationFn:async(sent:CloseRoomRequest)=>{
      const latestRoom=cache.getQueryData<Conversation>(['conversation',sent.target.roomId,sent.target.ownerId,sent.epoch])
      const latestProject=cache.getQueryData<Project>(['project',sent.target.ownerId,sent.epoch,sent.target.projectId])
      if(!mounted.current||!closeOwnerCurrent(sent)||!sameRoomCloseTarget(sent.target,latestRoom,latestProject))throw new Error('Room close not sent')
      sent.transportStarted=true
      await conversationsApi.delete(sent.target.roomId)
    },
    onSuccess:async(_result,sent)=>{
      if(!closeOwnerCurrent(sent))return
      await cache.cancelQueries({queryKey:['conversation',sent.target.roomId,sent.target.ownerId,sent.epoch]})
      if(!closeOwnerCurrent(sent))return
      cache.setQueryData<Conversation>(['conversation',sent.target.roomId,sent.target.ownerId,sent.epoch],previous=>previous?.id===sent.target.roomId?{...previous,is_active:false}:previous)
      const identity=`${sent.target.ownerId}:${sent.epoch}`
      void cache.invalidateQueries({queryKey:['rooms',identity,sent.target.projectId]})
      void cache.invalidateQueries({queryKey:['projects',identity]})
      void cache.invalidateQueries({queryKey:['dashboard',sent.target.ownerId,sent.epoch]})
      if(mounted.current&&sent.target.roomId===roomId&&clipboardView.current.location===sent.locationKey)navigate('/projects')
    },
    onError:(_error,sent)=>{if(mounted.current&&closeOwnerCurrent(sent)&&sent.target.roomId===roomId)setCloseFailure({target:sent.target,phase:sent.transportStarted?'unknown':'not_sent'})},
    onSettled:()=>{closeSendLock.current=false},
  })
  const openRoomClose=()=>{
    if(!clipboardOwner()||!roomActive||closeSendLock.current||closingTargetRef.current)return
    const target=roomCloseTarget(conversation.data,project.data);if(!target)return
    setCloseFailure(null);setOpenActionSeq(null);setMentionOpen(false);setManagementOpen(false)
    closingTargetRef.current=target;setClosingTarget(target)
  }
  const cancelRoomClose=()=>{if(!clipboardOwner())return;closingTargetRef.current=null;setClosingTarget(null)}
  const confirmRoomClose=(target:RoomCloseTarget)=>{
    if(!clipboardOwner()||closingTargetRef.current!==target||closeSendLock.current||!roomMetadataReady||availabilityRetrying||conversation.isFetching||project.isFetching)return
    const latestRoom=cache.getQueryData<Conversation>(['conversation',roomId,ownerId,authEpoch])
    const latestProject=cache.getQueryData<Project>(['project',ownerId,authEpoch,target.projectId])
    if(!sameRoomCloseTarget(target,latestRoom,latestProject))return
    closeSendLock.current=true;closingTargetRef.current=null;setClosingTarget(null);setCloseFailure(null)
    close.mutate({target,epoch:authEpoch,generation:readingGeneration,locationKey:location.key,transportStarted:false})
  }
  const openScreenRecordingSettings = async () => {
    if (settingsPending) return
    setSettingsError('')
    setSettingsPending(true)
    try {
      await invoke('open_screen_recording_settings')
    } catch {
      setSettingsError('chat.settingsFailure')
    } finally {
      setSettingsPending(false)
    }
  }
  const uncertain = (activity.data || []).filter((item) => item.state === 'uncertain' && !acknowledged.includes(item.action_id))
  const lastUserSeq = events.filter((item) => item.actor_type === 'user' && item.kind === 'message').at(-1)?.seq || 0
  const failure = events.filter((item) => item.kind === 'failure' && item.seq > lastUserSeq).at(-1)
  const configurationFailure = !!failure && ['model_feature_unavailable', 'local_model_unavailable', 'model_capabilities_changed', 'missing_secret', 'provider_unavailable', 'provider_invalid_response', 'unsupported_option', 'secret_store_unavailable'].includes(String(failure.payload.code))
  const pendingApprovals = (activity.data || []).filter((item) => item.state === 'waiting')
  const attentionApproval = attentionAction ? pendingApprovals.find((item) => item.action_id === attentionAction) : undefined
  const attentionRecord = attentionAction && !attentionApproval ? activity.data?.find(item => item.room_id === roomId && item.action_id === attentionAction && item.state !== 'waiting') : undefined
  const displayedEvents = useMemo(() => filterTranscriptEvents(presentationEvents, transcriptQuery, transcriptActor, visibleMessageTexts),
    [presentationEvents, transcriptQuery, transcriptActor, visibleMessageTexts, locale])
  const filteringTranscript = !!transcriptQuery.trim() || transcriptActor !== 'all'
  const timelineEvents = filteringTranscript ? displayedEvents : presentationEvents
  const transcriptPositions = useMemo(() => new Map(displayedEvents.map((event, index) => [event.event_id, { event, index }] as const)), [displayedEvents])
  const positionedMatch = transcriptTarget?.context === transcriptNavContext ? transcriptPositions.get(transcriptTarget.eventId) : undefined
  const transcriptMatchIndex = positionedMatch && positionedMatch.event.seq === transcriptTarget?.seq ? positionedMatch.index : -1
  const transcriptSearchTarget = filteringTranscript && transcriptSearchOpen && roomView === 'conversation' && transcriptMatchIndex >= 0 ? displayedEvents[transcriptMatchIndex] : null
  const authoredEvents = events.filter((item) => item.kind === 'message' && (item.actor_type === 'user' || item.actor_type === 'agent'))
  const seqParam = searchParams.get('seq')
  const expectedEventId = searchParams.get('event')
  const requestedSeq = seqParam && /^\d+$/.test(seqParam) && Number.isSafeInteger(Number(seqParam)) && Number(seqParam) > 0 ? Number(seqParam) : null
  const summarizedBatchRequests = useMemo(() => {
    const roots = new Map<string, number>()
    if (filteringTranscript || !botBatches.isSuccess || botBatches.isError || roomActive && roomStatusConnection !== 'live') return roots
    for (const item of authoredEvents) {
      if (requestedSeq === item.seq) continue
      const root = summarizedBatchRequestRoot(item, botBatches.data.batches, timelineEvents, ownerId)
      if (root) roots.set(item.event_id, root.seq)
    }
    return roots
  }, [filteringTranscript, botBatches.isSuccess, botBatches.isError, botBatches.data, roomActive, roomStatusConnection, authoredEvents, requestedSeq, timelineEvents, ownerId])
  const visibleAuthoredEvents = authoredEvents.filter(item => !summarizedBatchRequests.has(item.event_id))
  const attentionFailureEvent = attentionFailure && requestedSeq
    ? events.find((item) => item.kind === 'failure' && item.event_id === attentionFailure && item.seq === requestedSeq && item.seq > lastUserSeq)
    : undefined
  const rememberTranscriptPosition = () => {
    if (transcriptOrigin.current || !timelinePositioned.current || roomView !== 'conversation') return
    const scroller = scrollRef.current
    if (!scroller) return
    const root = scroller.getBoundingClientRect()
    const first = Array.from(scroller.querySelectorAll<HTMLElement>('[data-event-seq]')).find((node) => {
      const rect = node.getBoundingClientRect()
      return rect.height > 0 && rect.bottom > root.top && rect.top < root.bottom
    })
    const seq = first ? Number(first.dataset.eventSeq) : null
    transcriptOrigin.current = { seq, eventId: events.find((item) => item.seq === seq)?.event_id,
      offset: first ? first.getBoundingClientRect().top - root.top : 0, top: scroller.scrollTop,
      bottom: atBottomRef.current, high: authoredEvents.at(-1)?.seq || 0, location: location.key }
  }
  const changeTranscriptQuery = (value: string) => { setTranscriptContextReturn(null); setTranscriptResume(null); rememberTranscriptPosition(); if (!value.trim() && transcriptActor === 'all') transcriptRestore.current = true; setTranscriptQuery(value) }
  const changeTranscriptActor = (value: TranscriptActorFilter) => { setTranscriptContextReturn(null); setTranscriptResume(null); rememberTranscriptPosition(); if (value === 'all' && !transcriptQuery.trim()) transcriptRestore.current = true; setTranscriptActor(value) }
  const clearTranscriptSearch = (close = false) => {
    setTranscriptContextReturn(null); setTranscriptResume(null)
    transcriptRestore.current = true
    setTranscriptQuery(''); setTranscriptActor('all')
    if (close) { setTranscriptSearchOpen(false); transcriptFocusReturn.current = true }
  }
  // A return bookmark belongs only to the next exact context route, then that
  // history entry. Later unrelated navigation must never revive it.
  useLayoutEffect(() => {
    const saved = transcriptContextReturn
    if (!saved) return
    if (!clipboardOwner()) { setTranscriptContextReturn(null); return }
    if (saved.atLocation === null) {
      if (location.key === saved.fromLocation) return
      if (seqParam === String(saved.seq) && expectedEventId === saved.eventId && !attentionAction && !attentionFailure && !resultRouteActive) {
        setTranscriptContextReturn(previous => previous === saved ? { ...saved, atLocation: location.key } : previous)
      } else setTranscriptContextReturn(null)
    } else if (saved.atLocation !== location.key) setTranscriptContextReturn(null)
  }, [transcriptContextReturn, location.key, seqParam, expectedEventId, attentionAction, attentionFailure, resultRouteActive])
  // Restore after the full transcript DOM returns, before initial positioning/stream effects.
  useLayoutEffect(() => {
    const origin = transcriptOrigin.current
    if (origin && origin.location !== location.key) {
      transcriptOrigin.current = null; transcriptRestore.current = false
      setTranscriptQuery(''); setTranscriptActor('all'); setTranscriptSearchOpen(false)
      return
    }
    if (roomView !== 'conversation' || filteringTranscript || !transcriptRestore.current) return
    const scroller = scrollRef.current
    if (!scroller) return
    transcriptRestore.current = false
    if (origin) {
      const event = events.find((item) => item.seq === origin.seq && item.event_id === origin.eventId)
      const node = event ? scroller.querySelector<HTMLElement>(`[data-event-seq="${event.seq}"]`) : null
      if (node) {
        let parent = node.parentElement
        while (parent && parent !== scroller) { if (parent instanceof HTMLDetailsElement) parent.open = true; parent = parent.parentElement }
        scroller.scrollTop += node.getBoundingClientRect().top - scroller.getBoundingClientRect().top - origin.offset
      } else scroller.scrollTop = origin.top
      timelinePositioned.current = true
      positionedDeepLink.current = seqParam === null ? '' : `${location.key}:${seqParam}:${expectedEventId || ''}`
      const high = authoredEvents.at(-1)?.seq || 0
      authoredHighRef.current = high
      atBottomRef.current = origin.bottom && high === origin.high && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 48
      transcriptOrigin.current = null
    }
    if (transcriptFocusReturn.current) { transcriptFocusReturn.current = false; transcriptButtonRef.current?.focus({ preventScroll: true }) }
  }, [filteringTranscript, transcriptSearchOpen, roomView, events, location.key, seqParam, expectedEventId])
  useEffect(() => {
    if (!transcriptSearchOpen || managementOpen || roomView !== 'conversation') return
    const origin = document.activeElement
    const frame = window.requestAnimationFrame(() => {
      const field = transcriptInputRef.current
      if (!clipboardOwner() || clipboardView.current.roomView !== 'conversation' || !document.hasFocus() || document.visibilityState !== 'visible'
        || document.activeElement !== origin && document.activeElement !== document.body || !field?.isConnected || !field.getClientRects().length
        || field.closest('[hidden], [inert], [aria-hidden="true"]') || document.querySelector('dialog[open], [aria-modal="true"]')) return
      field.focus({ preventScroll: true })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [transcriptSearchOpen, managementOpen, roomView])
  const navigateToEvent = (seq: number, eventId?: string, keepSearch = false) => {
    setTranscriptResume(null)
    setTranscriptContextReturn(keepSearch && eventId && clipboardOwner() && !managementOpen && roomView === 'conversation'
      && transcriptSearchOpen && filteringTranscript && displayedEvents.some(event => event.room_id === roomId && event.seq === seq && event.event_id === eventId)
      ? { query: transcriptQuery, actor: transcriptActor, seq, eventId, origin: transcriptOrigin.current ? { ...transcriptOrigin.current } : null,
        fromLocation: location.key, atLocation: null } : null)
    transcriptOrigin.current = null; transcriptRestore.current = false; transcriptFocusReturn.current = false
    setTranscriptQuery(''); setTranscriptActor('all'); setTranscriptSearchOpen(false)
    setRoomView('conversation')
    setManagementOpen(false)
    const next = new URLSearchParams(searchParams)
    next.delete('action')
    next.delete('failure')
    next.delete('result'); next.delete('version'); next.delete('base')
    next.set('seq', String(seq))
    if (eventId) next.set('event', eventId)
    else next.delete('event')
    navigate({ search: `?${next.toString()}` })
  }
  const restoreTranscriptSearch = () => {
    const saved = transcriptContextReturn
    if (!saved || !clipboardOwner() || managementOpen || inspectedRequest || roomView !== 'conversation' || transcriptSearchOpen
      || saved.atLocation !== location.key || seqParam !== String(saved.seq) || expectedEventId !== saved.eventId
      || document.visibilityState !== 'visible' || !document.hasFocus() || document.querySelector('dialog[open], [aria-modal="true"]')) return
    setTranscriptContextReturn(null)
    transcriptOrigin.current = saved.origin ? { ...saved.origin, location: location.key } : null
    transcriptRestore.current = false; transcriptFocusReturn.current = false
    setTranscriptQuery(saved.query); setTranscriptActor(saved.actor); setTranscriptSearchOpen(true)
    setTranscriptTarget(null); setTranscriptMoveFailure(null); setTranscriptResume(saved)
  }
  const unreadCount = readStateReady ? visibleAuthoredEvents.filter((item) => item.seq > lastReadSeq).length : 0
  const acknowledgeVisible = useCallback(() => {
    if (roomView !== 'conversation' || managementOpen || attentionAction || attentionFailure || !readStateReady || historyState !== 'complete' || filteringTranscript || document.visibilityState !== 'visible') return
    const scroller = scrollRef.current
    if (!scroller) return
    const root = scroller.getBoundingClientRect()
    const top = Math.max(0, root.top)
    const bottom = Math.min(window.innerHeight, root.bottom)
    scroller.querySelectorAll<HTMLElement>('[data-authored-seq]').forEach((node) => {
      const rect = node.getBoundingClientRect()
      const seq = Number(node.dataset.authoredSeq) || 0
      if (seq > lastReadRef.current && rect.bottom > top && rect.top < bottom) seenAuthoredSeqs.current.add(seq)
    })
    let visibleThrough = lastReadRef.current
    for (const item of authoredEvents) {
      if (item.seq <= lastReadRef.current) continue
      // A visible summary represents its sending receipts, but the server
      // cursor still advances through the original raw authored sequence.
      const rootSeq = summarizedBatchRequests.get(item.event_id)
      if (rootSeq !== undefined ? rootSeq > lastReadRef.current && !seenAuthoredSeqs.current.has(rootSeq) : !seenAuthoredSeqs.current.has(item.seq)) break
      visibleThrough = item.seq
    }
    if (visibleThrough > lastReadRef.current) requestReadAck(visibleThrough)
  }, [roomView, managementOpen, readStateReady, historyState, filteringTranscript, requestReadAck, authoredEvents, summarizedBatchRequests, attentionAction, attentionFailure])

  const captureReading = useCallback(() => {
    const auth = useAuthStore.getState()
    if (!mounted.current || auth.epoch !== authEpoch || auth.user?.id !== ownerId
      || historyState !== 'complete' || !timelinePositioned.current || filteringTranscript || transcriptSearchOpen
      || managementOpen || inspectedRequest || roomView !== 'conversation' || !scrollRef.current) return
    const saved = captureRoomReadingPosition(scrollRef.current, events, atBottomRef.current)
    if (saved) {
      readingPosition.current = saved
      writeDirectoryView(readingIdentity, readingKey, saved, readingGeneration)
    }
  }, [authEpoch, ownerId, historyState, filteringTranscript, transcriptSearchOpen, managementOpen, inspectedRequest, roomView, events, readingIdentity, readingKey, readingGeneration])
  const interruptReadingRestore = () => {
    if (!timelinePositioned.current) readingRestoreInterrupted.current = true
  }

  const moveTranscriptResult = (direction: number, requested?: RoomEvent) => {
    if (!clipboardOwner() || !ownsRoom || managementOpen || roomView !== 'conversation' || !transcriptSearchOpen || !filteringTranscript || !displayedEvents.length) return
    const scroller = scrollRef.current
    if (!scroller?.isConnected || scroller.clientHeight <= 0) return
    const index = transcriptMatchIndex < 0 ? direction > 0 ? 0 : displayedEvents.length - 1
      : (transcriptMatchIndex + direction + displayedEvents.length) % displayedEvents.length
    const event = requested || displayedEvents[index]
    if (requested && !displayedEvents.some(value => value.seq === requested.seq && value.event_id === requested.event_id)) return
    if (!event || event.room_id !== roomId || !Number.isSafeInteger(event.seq) || event.seq <= 0) { setTranscriptMoveFailure(transcriptNavContext); return }
    const node = scroller.querySelector<HTMLElement>(`[data-event-seq="${event.seq}"]`)
    if (!node?.isConnected) { setTranscriptMoveFailure(transcriptNavContext); return }
    // Finalizer requests can be authored search matches inside folded activity
    // logs. Reveal those existing nodes without clearing the search or routing.
    let parent = node.parentElement
    while (parent && parent !== scroller) { if (parent instanceof HTMLDetailsElement) parent.open = true; parent = parent.parentElement }
    if (node.classList.contains('conversation-auxiliary')) {
      const detail = node.querySelector<HTMLDetailsElement>(':scope > details')
      if (detail) detail.open = true
    }
    if (!node.getClientRects().length) { setTranscriptMoveFailure(transcriptNavContext); return }
    interruptReadingRestore(); atBottomRef.current = false
    scroller.scrollTop += node.getBoundingClientRect().top - scroller.getBoundingClientRect().top - scroller.clientTop - 12
    setTranscriptTarget({ context: transcriptNavContext, eventId: event.event_id, seq: event.seq }); setTranscriptMoveFailure(null)
  }

  useLayoutEffect(() => {
    const saved = transcriptResume
    if (!saved) return
    // Consume once after the restored filter DOM exists; never wait and jump later.
    setTranscriptResume(null)
    if (!clipboardOwner() || managementOpen || inspectedRequest || roomView !== 'conversation' || !transcriptSearchOpen
      || saved.atLocation !== location.key || transcriptQuery !== saved.query || transcriptActor !== saved.actor
      || document.visibilityState !== 'visible' || !document.hasFocus()) return
    const event = displayedEvents.find(value => value.seq === saved.seq && value.event_id === saved.eventId && value.room_id === roomId)
    if (event) moveTranscriptResult(0, event)
    else setTranscriptMoveFailure(transcriptNavContext)
  }, [transcriptResume, transcriptNavContext, transcriptQuery, transcriptActor, displayedEvents, location.key, roomView, transcriptSearchOpen, managementOpen, inspectedRequest])

  const handleTimelineScroll = useCallback(() => {
    const scroller = scrollRef.current
    if (!scroller) return
    atBottomRef.current = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 48
    captureReading()
    acknowledgeVisible()
  }, [acknowledgeVisible, captureReading])

  useEffect(() => {
    const visible = () => { if (document.visibilityState === 'visible') acknowledgeVisible() }
    document.addEventListener('visibilitychange', visible)
    return () => document.removeEventListener('visibilitychange', visible)
  }, [acknowledgeVisible])

  useLayoutEffect(() => {
    if (roomView !== 'conversation' || filteringTranscript || !readStateReady || historyState !== 'complete') return
    if (attentionAction || attentionFailure) return
    const deepLinkKey = seqParam === null ? '' : `${location.key}:${seqParam}:${expectedEventId || ''}`
    if (timelinePositioned.current && positionedDeepLink.current === deepLinkKey) return
    const savedReading = !readingRestoreAttempted.current && !readingRestoreInterrupted.current ? initialReadingPosition.current : null
    readingRestoreAttempted.current = true
    timelinePositioned.current = true
    positionedDeepLink.current = deepLinkKey
    if (seqParam !== null) {
      const targetEvent = requestedSeq ? events.find((item) => item.seq === requestedSeq
        && (expectedEventId === null || item.event_id === expectedEventId)) : undefined
      const target = targetEvent ? scrollRef.current?.querySelector<HTMLElement>(`[data-event-seq="${targetEvent.seq}"]`) : null
      setDeepLinkMissing(!target)
      setHighlightedSeq(target ? targetEvent!.seq : null)
      if (target) { let parent = target.parentElement; while (parent && parent !== scrollRef.current) { if (parent instanceof HTMLDetailsElement) parent.open = true; parent = parent.parentElement } }
      target?.scrollIntoView({ block: 'center' })
      atBottomRef.current = false
    } else if (savedReading && !savedReading.following && scrollRef.current && restoreRoomReadingPosition(scrollRef.current, events, savedReading)) {
      setDeepLinkMissing(false)
      setHighlightedSeq(null)
      atBottomRef.current = false
    } else if (readingRestoreInterrupted.current && scrollRef.current) {
      // Reading/scrolling before history finishes takes precedence over a late automatic jump.
      setDeepLinkMissing(false)
      setHighlightedSeq(null)
      const scroller = scrollRef.current
      atBottomRef.current = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 48
    } else {
      setDeepLinkMissing(false)
      setHighlightedSeq(null)
      const firstUnread = visibleAuthoredEvents.find((item) => item.seq > lastReadRef.current)
      const target = firstUnread ? scrollRef.current?.querySelector<HTMLElement>(`[data-authored-seq="${firstUnread.seq}"]`) : endRef.current
      target?.scrollIntoView({ block: firstUnread ? 'start' : 'end' })
      atBottomRef.current = !firstUnread
    }
    authoredHighRef.current = authoredEvents.at(-1)?.seq || 0
    const frame = window.requestAnimationFrame(() => { captureReading(); acknowledgeVisible() })
    return () => window.cancelAnimationFrame(frame)
  }, [roomView, readStateReady, historyState, events, authoredEvents, visibleAuthoredEvents, acknowledgeVisible, captureReading, requestedSeq, seqParam, expectedEventId, location.key, filteringTranscript, attentionAction, attentionFailure])

  useEffect(() => {
    if (!timelinePositioned.current) return
    const high = authoredEvents.at(-1)?.seq || 0
    if (high <= authoredHighRef.current) return
    authoredHighRef.current = high
    if (managementOpen || filteringTranscript || transcriptOrigin.current || roomView !== 'conversation') { atBottomRef.current = false; return }
    if (atBottomRef.current) {
      endRef.current?.scrollIntoView({ block: 'end' })
      window.requestAnimationFrame(acknowledgeVisible)
    }
  }, [authoredEvents, acknowledgeVisible, managementOpen, roomView, filteringTranscript])

  useEffect(() => {
    if (!attentionAction && !attentionFailure) { positionedAttention.current = null; setAttentionMissing(false); return }
    if (roomView !== 'conversation' || managementOpen || filteringTranscript || transcriptOrigin.current) return
    if (attentionAction && !activity.isSuccess && !attentionRecord) return
    if (attentionFailure && historyState !== 'complete') return
    const targetId = attentionAction && attentionApproval ? `room-action-${attentionApproval.action_id}`
      : attentionAction && attentionRecord ? `room-action-history-${attentionRecord.action_id}` : attentionFailure && attentionFailureEvent ? `room-failure-${attentionFailureEvent.event_id}` : ''
    const attentionKey = `${location.key}:${targetId}`
    if (targetId && positionedAttention.current === attentionKey) return
    const frame = window.requestAnimationFrame(() => {
      const target = targetId ? document.getElementById(targetId) : null
      if (target) positionedAttention.current = attentionKey
      setAttentionMissing(!target)
      setHighlightedSeq(target && attentionFailureEvent ? attentionFailureEvent.seq : null)
      if (target) { let parent = target.parentElement; while (parent && parent !== scrollRef.current) { if (parent instanceof HTMLDetailsElement) parent.open = true; parent = parent.parentElement } }
      target?.scrollIntoView({ block: 'center' })
      if (target) atBottomRef.current = false
    })
    return () => window.cancelAnimationFrame(frame)
  }, [attentionAction, attentionFailure, attentionApproval, attentionRecord, attentionFailureEvent, activity.isSuccess, historyState, filteringTranscript, location.key, roomView, managementOpen])
  const memberStatuses = (members.data || []).map((member) => agentStatus(member, activity.data || [], events, members.isError ? 'retrying' : roomStatusConnection, t))
  const roomActivityLabel = projectConfirmedInactive ? t('projects.archivedProject') : roomConfirmedInactive ? t('chat.closedRoom') : !roomMetadataReady ? t(availabilityFailed ? 'chat.availability.failed' : 'chat.availability.loading') : connectionState !== 'live' || members.isLoading ? t('chat.activityChecking') : members.isError ? t('chat.activityUnavailable') : (() => {
    const approvalCount = memberStatuses.filter((status) => status.visual === 'approval_waiting').length
    const toolCount = memberStatuses.filter((status) => status.visual === 'tool_running').length
    const toolWaitingCount = memberStatuses.filter((status) => status.visual === 'tool_waiting').length
    const thinkingCount = memberStatuses.filter((status) => status.visual === 'thinking').length
    const queuedCount = memberStatuses.filter((status) => status.visual === 'queued').length
    const peerCount = memberStatuses.filter((status) => status.visual === 'peer_waiting').length
    const errorCount = memberStatuses.filter((status) => status.visual === 'error').length
    const pausedCount = memberStatuses.filter((status) => status.visual === 'paused').length
    const unknownCount = memberStatuses.filter((status) => status.visual === 'unknown').length
    const parts = [approvalCount ? t('chat.activityApprovals', { count: approvalCount }) : '', toolCount ? t('chat.activityTools', { count: toolCount }) : '', thinkingCount ? t('chat.activityThinking', { count: thinkingCount }) : '', pausedCount ? t('chat.activityPaused', { count: pausedCount }) : '', errorCount ? t('chat.activityErrors', { count: errorCount }) : '', unknownCount ? t('chat.activityUnknown', { count: unknownCount }) : ''].filter(Boolean)
    if (queuedCount) parts.push(t('chat.activityQueued', { count: queuedCount }))
    if (peerCount) parts.push(t('chat.activityPeerWaiting', { count: peerCount }))
    if (toolWaitingCount) parts.push(t('chat.activityToolWaiting', { count: toolWaitingCount }))
    return parts.join(' · ') || t('chat.activityAllIdle')
  })()
  const visibleTriggers = useMemo(() => new Set(events.map((item) => item.event_id)), [events])
  const finalizerRequests = useMemo(() => new Map(events.flatMap((item) => {
    const value = item.payload.finalize
    if (item.actor_type !== 'user' || item.kind !== 'message' || !value || typeof value !== 'object') return []
    const finalize = value as Record<string, unknown>
    return finalize.version === 1 && typeof finalize.source_request_event_id === 'string'
      && Array.isArray(finalize.source_outcome_event_ids) && typeof finalize.agent_id === 'number'
      ? [[item.event_id, finalize.agent_id] as const] : []
  })), [events])
  // Keep the original filtered-event derivation (including its runtime locale
  // semantics). The normal full feed can reuse grouping while only the
  // composer or other unrelated UI state changes. Never cache rendered nodes.
  const timelineGroups = useMemo(() => conversationTimeline(timelineEvents, finalizerRequests), [timelineEvents, finalizerRequests])
  const refreshApprovals = useCallback(() => { cache.invalidateQueries({ queryKey: ['activity', roomId] }); cache.invalidateQueries({ queryKey: ['members', roomId] }) }, [cache, roomId])
  const inlinePrompt = (action: typeof pendingApprovals[number]) => <div key={action.action_id} id={`room-action-${action.action_id}`}
    className={attentionAction === action.action_id ? 'attention-target' : undefined}><GrantPrompt roomId={roomId}
      roomName={conversation.data?.title || t('chat.roomTitle', { id: String(roomId) })}
      agentName={agents.data?.find((agent) => agent.id === action.agent_id)?.name || t('members.fallbackName', { id: String(action.agent_id) })}
      action={action} onRefresh={refreshApprovals} onStop={requestStop} stopPending={roomStopPending} /></div>
  const transcriptExportEvents=useMemo(()=>transcriptExportRange==='matches'?displayedEvents:Array.from(visibleMessageTexts.keys()),[transcriptExportRange,displayedEvents,visibleMessageTexts])
  const transcriptExportScope:TranscriptExportScope={range:transcriptExportRange,body:transcriptExportBody,messageCount:transcriptExportEvents.length,...(transcriptExportRange==='matches'?{query:transcriptQuery,actor:transcriptActor}:{})}
  const transcriptExportContext=JSON.stringify([transcriptExportRange,transcriptExportBody,transcriptExportRange==='matches'?transcriptQuery:'',transcriptExportRange==='matches'?transcriptActor:'all'])
  useEffect(()=>{setTranscriptDownload(null)},[transcriptExportContext])
  const transcriptContent = () => transcriptMarkdown(conversation.data?.title || t('chat.roomTitle', { id: String(roomId) }), transcriptExportEvents, {
      title: t('chat.transcript.exportTitle'), user: t('chat.transcript.user'), agent: t('chat.transcript.agent'),
      sequence: t('chat.transcript.sequence'),scope:describeTranscriptExport(transcriptExportScope,t),
    }, (value) => formatDateTime(value, { dateStyle: 'medium', timeStyle: 'short' }),event=>transcriptExportBody==='visible'?(visibleMessageTexts.get(event)??(event.actor_type==='agent'?assistantVisibleMarkdown(String(event.payload.content)):String(event.payload.content))):String(event.payload.content))
  const transcriptSnapshot=():RoomCopySnapshot|null=>historyState==='complete'&&clipboardOwner()&&transcriptExportEvents.length>0
    ?{text:transcriptContent(),kind:'transcript',capturedAt:new Date().toISOString(),transcriptScope:{...transcriptExportScope}}:null
  const exportTranscript = () => {
    let href:string|null=null
    try{
      const original=transcriptSnapshot();if(!original)return
      href=URL.createObjectURL(new Blob([original.text],{type:'text/markdown;charset=utf-8'}))
      const anchor=document.createElement('a');anchor.href=href;anchor.download=transcriptFilename(conversation.data?.title||`room-${roomId}`)
      anchor.click();setTranscriptDownload('requested')
    }catch{if(clipboardOwner())setTranscriptDownload('failed')}
    finally{if(href){const owned=href;window.setTimeout(()=>URL.revokeObjectURL(owned),0)}}
  }
  const copyTranscript=()=>{const value=transcriptSnapshot();if(value)void transcriptCopy.copy(value)}
  const manualTranscript=()=>{const value=transcriptSnapshot();if(value)transcriptCopy.openManual(value)}
  const changeTranscriptExport=(range:TranscriptExportScope['range'],body:TranscriptExportScope['body'])=>{
    if(!clipboardOwner()||transcriptCopy.busy)return
    transcriptCopy.invalidate();setTranscriptDownload(null);setTranscriptExportRange(range);setTranscriptExportBody(body)
  }
  const messageSnapshot=(seq:number,value:string,success:'copied'|'linkCopied'):RoomCopySnapshot|null=>{
    const event=clipboardEvents.current.find(row=>row.room_id===roomId&&row.seq===seq)
    if(!event)return null
    return{text:value,kind:success==='linkCopied'?'link':'message',seq,eventId:event.event_id,capturedAt:new Date().toISOString()}
  }
  const copyMessageValue=(seq:number,value:string,success:'copied'|'linkCopied')=>{
    const original=messageSnapshot(seq,value,success)
    if(!original||!messageCopyAllowed(original)||messageCopy.busy)return
    setMessageAction(null);setOpenActionSeq(null)
    if(actionAnchor.current?.isConnected&&!actionAnchor.current.closest('[inert]'))actionAnchor.current.focus({preventScroll:true})
    void messageCopy.copy(original)
  }
  const manualMessageValue=(seq:number,value:string,success:'copied'|'linkCopied')=>{
    const original=messageSnapshot(seq,value,success)
    if(!original||!messageCopyAllowed(original))return
    setOpenActionSeq(null)
    if(actionAnchor.current?.isConnected&&!actionAnchor.current.closest('[inert]'))actionAnchor.current.focus({preventScroll:true})
    messageCopy.openManual(original)
  }
  const messagePermalink = (seq: number) => {
    const event = clipboardEvents.current.find(row => row.room_id === roomId && row.seq === seq)
    return event ? messageEventHref(roomId, event.seq, event.event_id) : null
  }
  const closeInspector = () => {
    const seq = inspectorReturnSeq.current
    setInspectedRequest(null)
    window.requestAnimationFrame(() => scrollRef.current?.querySelector<HTMLElement>(`[data-event-seq="${seq}"] .message-actions__trigger`)?.focus())
  }
  const refreshAfterRetry = async (_result: RequestRetryResult | FinalizeResult) => {
    cache.invalidateQueries({ queryKey: ['activity', roomId] })
    cache.invalidateQueries({ queryKey: ['members', roomId] })
    try {
      let batch: RoomEvent[]
      do {
        const after = roomEventCursor.afterSeq(roomId)
        batch = await eventsApi.list(roomId, after, 100)
        const auth = useAuthStore.getState()
        if (!mounted.current || auth.epoch !== authEpoch || auth.user?.id !== ownerId
          || batch.some((item, index) => item.seq !== after + index + 1)) return
        roomEventCursor.add(roomId, batch)
      } while (batch.length === 100)
      const auth = useAuthStore.getState()
      if (mounted.current && auth.epoch === authEpoch && auth.user?.id === ownerId) setEvents(roomEventCursor.events(roomId))
    } catch { /* the existing room socket/catch-up loop remains authoritative */ }
  }
  const refreshAfterFinalize = async (result: FinalizeResult, canNavigate?: () => boolean) => {
    await refreshAfterRetry(result)
    const auth = useAuthStore.getState()
    if (!mounted.current || auth.epoch !== authEpoch || auth.user?.id !== ownerId || result.room_id !== roomId) return
    if (!canNavigate?.()) return
    setInspectedRequest(null)
    navigate(`/chat/${roomId}?seq=${result.finalize_seq}&event=${encodeURIComponent(result.finalize_event_id)}`)
  }
  const quoteMessage = (seq: number, content: string) => {
    messageCopy.invalidate()
    const caret = inputRef.current?.selectionStart ?? input.length
    const next = quoteAtCaret(input, content, caret)
    if (editorTextLength(next.content) > 65536) { setMessageAction({ seq, status: 'quoteError' }); setOpenActionSeq(null); return }
    changeComposerInput(next.content)
    latestInput.current = next.content
    draft.current = null
    setMentionCaret(next.caret)
    setMentionOpen(false)
    setActiveMention(0)
    setMessageAction(null)
    setOpenActionSeq(null)
    queueMicrotask(() => { inputRef.current?.focus(); inputRef.current?.setSelectionRange(next.caret, next.caret) })
  }
  const quoteReply = (item:RoomEvent, content:string) => {
    const fail=(status:'replyUnavailable'|'replyConflict'|'quoteError')=>{setMessageAction({seq:item.seq,status});setOpenActionSeq(null)}
    if (!clipboardOwner() || clipboardView.current.roomView!=='conversation' || clipboardView.current.location!==location.key || managementOpen || inspectedRequest || item.room_id!==roomId || item.kind!=='message' || item.actor_type!=='agent' || !canonicalEventId(item.event_id)) return
    const source=clipboardEvents.current.find(row=>row.room_id===roomId&&row.event_id===item.event_id&&row.seq===item.seq&&row.actor_type==='agent'&&row.actor_id===item.actor_id&&row.kind==='message')
    if(!source || clipboardVisibleTexts.current.get(source)!==content)return
    if (!roomWriteReady.current || !draftReady || draftLoadError || draftConflictRef.current || roomStopPending || send.isPending || deliveryBusy || blocksNewMessage(deliveryMessages) || fileQuestion.selected || fileQuestion.busy || composerComposing.current || performance.now()<composerImeUntil.current || !agents.isSuccess || agents.isError || !members.isSuccess || members.isError || cache.getQueryState(['agents',readingIdentity])?.isInvalidated || cache.getQueryState(['members',roomId,ownerId,authEpoch])?.isInvalidated || !item.actor_id) {fail('replyUnavailable');return}
    const liveMembers=(members.data||[]).filter(member=>member.room_id===roomId && member.is_active && !member.paused)
    const liveAgents=agents.data||[],target=liveAgents.find(agent=>agent.id===item.actor_id && agent.user_id===ownerId && agent.is_active)
    if(!target || !liveMembers.some(member=>member.agent_id===target.id) || liveMembers.some(member=>!liveAgents.some(agent=>agent.id===member.agent_id))) {fail('replyUnavailable');return}
    const value=latestInput.current, caret=inputRef.current?.selectionStart ?? value.length
    const next=quoteReplyToBot(value,content,caret,target.id,buildRecipientGroups(liveAgents,liveMembers))
    if(next.status!=='ready'){fail(next.status==='conflict'?'replyConflict':'replyUnavailable');return}
    if(editorTextLength(next.content)>65536){fail('quoteError');return}
    messageCopy.invalidate();changeComposerInput(next.content);draft.current=null;setMentionCaret(next.caret);setMentionOpen(false);setActiveMention(0);setMessageAction({seq:item.seq,status:'replyInserted'});setOpenActionSeq(null)
    const locationKey=location.key
    queueMicrotask(()=>{if(!clipboardOwner() || clipboardView.current.roomView!=='conversation' || clipboardView.current.location!==locationKey || !roomWriteReady.current)return;inputRef.current?.focus();inputRef.current?.setSelectionRange(next.caret,next.caret)})
  }
  const canSelectReply = (item: RoomEvent) => ordinaryRoom && !(!clipboardOwner() || clipboardView.current.roomView !== 'conversation' || clipboardView.current.location !== location.key || managementOpen || inspectedRequest || item.room_id !== roomId || item.kind !== 'message' || !['user','agent'].includes(item.actor_type) || !canonicalEventId(item.event_id) || !Number.isSafeInteger(item.seq) || item.seq <= 0 || !clipboardEvents.current.some(row => row.event_id === item.event_id && row.seq === item.seq && row.room_id === roomId) || !roomWriteReady.current || !draftReady || draftLoadError || draftConflictRef.current || roomStopPending || send.isPending || deliveryBusy || blocksNewMessage(deliveryMessages) || fileQuestion.selected || fileQuestion.busy || composerComposing.current || performance.now() < composerImeUntil.current)
  const selectReply = (item: RoomEvent) => {
    if (!canSelectReply(item)) return
    changeReply({event_id:item.event_id,seq:item.seq}); draft.current=null; setOpenActionSeq(null); inputRef.current?.focus()
  }
  const switchRoomView = (next: 'conversation' | 'results' | 'memory') => {
    setOpenActionSeq(null); setMentionOpen(false)
    if (next === roomView) return
    setTranscriptContextReturn(null); setTranscriptResume(null)
    if (next !== 'conversation') {
      if (roomView === 'conversation') { captureReading(); savedFeedPosition.current = scrollRef.current?.scrollTop || 0 }
      atBottomRef.current = false; if (next === 'results') setResultsOpened(true); else setMemoryOpened(true)
    }
    setRoomView(next)
    if (next === 'conversation') window.requestAnimationFrame(() => {
      if (scrollRef.current) scrollRef.current.scrollTop = savedFeedPosition.current
    })
  }
  const openTranscriptSearch = () => {
    setTranscriptContextReturn(null); setTranscriptResume(null)
    captureReading()
    rememberTranscriptPosition()
    setManagementOpen(false); switchRoomView('conversation'); setTranscriptSearchOpen(true)
  }
  const saveResultSource = (event: RoomEvent) => { setResultSourceEvent(event); switchRoomView('results') }
  const activityActor = (item: RoomEvent) => {
    const id = typeof item.payload.agent_id === 'number' ? item.payload.agent_id : item.actor_type === 'agent' ? item.actor_id : null
    return agents.data?.find((agent) => agent.id === id)?.name || (item.actor_type === 'agent' ? item.actor_name_snapshot : '') || t('chat.system.activity')
  }
  const renderAttachments = (item: RoomEvent, guidance = true) => {
              const missingSecretId = missingSecretConnectionId(item)
              const permissionGuidance = guidance && item.kind === 'tool_result' && typeof item.payload.error_code === 'string' && permissionHelp[item.payload.error_code] ?
                <div className="conversation-auxiliary__guidance">
                  <p role="alert">{t(permissionHelp[item.payload.error_code])}</p>
                  {item.payload.error_code === 'screen_recording_denied' && <>
                    <button type="button" className="button button--secondary button--compact" disabled={settingsPending} onClick={() => { void openScreenRecordingSettings() }}>{t('chat.openScreenSettings')}</button>
                    <ScreenRecordingStatus />
                    {settingsError && <p role="alert" aria-label={t('chat.settingsError')}>{t(settingsError)}</p>}
                  </>}
                </div> : null
    return <>
      {permissionGuidance}
      {ordinaryRoom && guidance && needsNewActionReview(item) && <p className="activity-guidance">{t('chat.tool.reviewChanged')}</p>}
      {ordinaryRoom && guidance && needsNewActionReview(item) && item.payload.state === 'denied' && item.actor_type === 'tool' && canonicalEventId(item.event_id) && roomActive && <TargetedRetry roomId={roomId} failureEventId={item.event_id} ownerId={ownerId} authEpoch={authEpoch} toolReview showHistory={false} enabled={roomView === 'conversation' && !managementOpen && !inspectedRequest && draftReady && !draftLoadError && !draftConflict && !roomStopPending && !deliveryBusy && !blocksNewMessage(deliveryMessages) && !send.isPending && !fileQuestion.busy && !fileQuestion.pending} onViewSource={(seq,id)=>{if(clipboardOwner() && clipboardView.current.roomView==='conversation' && !managementOpen) navigateToEvent(seq,id)}} onCreated={result=>{if(clipboardOwner())void refreshAfterRetry(result)}}/>}
      {guidance && !needsNewActionReview(item) && item.kind === 'tool_result' && ['failed', 'denied', 'uncertain', 'cancelled', 'partial'].includes(String(item.payload.state)) && !permissionGuidance
        && <p className="activity-guidance">{t(item.payload.state === 'uncertain' ? 'chat.activity.uncertainHelp' : 'chat.activity.toolHelp')}</p>}
                {guidance && missingSecretId !== null && <p className="section-copy"><Link className="text-link" to={`/connections?edit=${missingSecretId}`}>
                  {t('chat.reenterKey', { id: String(missingSecretId) })}
                </Link></p>}
                {item.kind === 'tool_result' && typeof item.payload.screenshot_ref === 'string' && <p className="metadata activity-attachment-label">{t('chat.activity.attachment', { agent: activityActor(item) })}</p>}
                {item.kind === 'tool_result' && typeof item.payload.screenshot_ref === 'string'
                  && typeof item.payload.action_id === 'string' && typeof item.payload.image_width === 'number'
                  && typeof item.payload.image_height === 'number' && <ScreenshotArtifact roomId={roomId}
                    actionId={item.payload.action_id} eventSeq={item.seq} width={item.payload.image_width}
                    height={item.payload.image_height} createdAt={item.created_at} compact readOnly={!roomActive} />}
                {(() => { const memo=voiceMemoFromEvent(item,ownerId,roomId); if(memo)return <VoiceMemoArtifact key={`${readingIdentity}:${roomId}:${item.event_id}`} memo={memo} source={{kind:'event'}} roomId={roomId} ownerId={ownerId} epoch={authEpoch} canUse={()=>clipboardOwner() && clipboardView.current.roomView==='conversation' && clipboardView.current.location===location.key && !managementOpen && !inspectedRequest} />; return item.kind==='tool_result' && item.payload.voice_memo != null ? <p role="alert">{locale==='ko'?'음성 메모 정보를 확인할 수 없습니다.':'Voice memo information could not be confirmed.'}</p> : null })()}
                {roomActive && resultSource(item, roomId) === 'file' && <button type="button" className="text-link" onClick={() => saveResultSource(item)}>{t('results.save')}</button>}
                {roomActive && pendingApprovals.filter((action) => (ordinaryRoom || sharedReady && sharedAppsAvailable && action.family === 'mcp_tool_call') && action.trigger_event_id === item.event_id).map(inlinePrompt)}
              </>
  }
  const openBotMessageEvent = (reference: BotMessageRef) => {
    if (!clipboardOwner() || managementOpen || roomView !== 'conversation') return
    if (reference.room_id === roomId) navigateToEvent(reference.seq, reference.event_id)
    else { setTranscriptQuery(''); setTranscriptActor('all'); setTranscriptSearchOpen(false); navigate(`/chat/${reference.room_id}?${new URLSearchParams({ seq: String(reference.seq), event: reference.event_id })}`) }
  }
  const renderRoutineReference=(item:RoomEvent)=>{
    const v=item.payload.routine_ref
    if(item.actor_type!=='user'||item.actor_id!==ownerId||!v||typeof v!=='object'||Array.isArray(v))return null
    const ref=v as Record<string,unknown>
    if(Object.keys(ref).sort().join(',')!=='agent_id,revision,routine_id'||!Number.isSafeInteger(ref.agent_id)||!Number.isSafeInteger(ref.revision)||Number(ref.agent_id)<1||Number(ref.revision)<1||!canonicalEventId(ref.routine_id))return null
    return <p className="metadata">{locale==='ko'?'참조한 루틴':'Referenced routine'} · Bot #{String(ref.agent_id)} · {String(ref.routine_id).slice(-8)} · v{String(ref.revision)}</p>
  }
  const renderBotMessage = (item: RoomEvent) => !isBatchMemberMessage(item) && <BotMessage event={item} messages={botMessages.data?.messages || []} agents={agents.data || []}
    current={botMessages.isSuccess && !botMessages.isError && (!roomActive || roomStatusConnection === 'live')}
    fetching={botMessages.isFetching} failed={botMessages.isError} canUse={() => clipboardOwner() && !managementOpen && roomView === 'conversation'}
    onRead={() => { if (clipboardOwner()) void botMessages.refetch({ cancelRefetch: false }) }} onOpen={openBotMessageEvent} />
  const renderBatchEvent = (item: RoomEvent) => <BotBatchEvent event={item} batches={botBatches.data?.batches || []}
    current={botBatches.isSuccess && !botBatches.isError && (!roomActive || roomStatusConnection === 'live')}
    fetching={botBatches.isFetching} failed={botBatches.isError} onRead={() => { if (clipboardOwner()) void botBatches.refetch({ cancelRefetch: false }) }}
    canUse={() => clipboardOwner() && !managementOpen && roomView === 'conversation'} onOpen={openBotMessageEvent} />
  const renderCreation = (item: RoomEvent) => botCreationMetadata(item) && <BotCreation event={item} receipts={botCreations.data?.creations || []}
    receivedAt={botCreations.dataUpdatedAt} current={botCreations.isSuccess && !botCreations.isError}
    fetching={botCreations.isFetching} canUse={() => clipboardOwner() && !managementOpen && roomView === 'conversation'} />
  const renderEvent = (item: RoomEvent, withAttachments = true) => {
              if (!filteringTranscript && requestedSeq !== item.seq && duplicateCreationRequest(item, timelineEvents)) return null
              if (summarizedBatchRequests.has(item.event_id)) return null
              const checkinPayload = item.payload.checkin as Record<string, unknown> | null
              const checkinSource = checkinPayload?.version === 1 && canonicalEventId(checkinPayload.run_id) && typeof checkinPayload.scheduled_at === 'string' && item.kind === 'activity' && item.actor_type === 'system' && item.actor_id === null && item.payload.category === 'primary_checkin' && item.payload.status === 'started' && typeof item.payload.content === 'string' && typeof item.payload.checkin === 'object' && item.payload.checkin !== null
              const routinePayload = item.payload.routine as Record<string, unknown> | null
              const routineSource = routinePayload?.version === 1 && canonicalEventId(routinePayload.run_id) && canonicalEventId(routinePayload.routine_id) && routinePayload.owner_id === ownerId && typeof routinePayload.agent_id === 'number' && ['scheduled','test','webhook'].includes(String(routinePayload.origin)) && typeof routinePayload.scheduled_at === 'string' && item.kind === 'activity' && item.actor_type === 'system' && item.actor_id === null && item.payload.category === 'owner_routine' && item.payload.status === 'started' && typeof item.payload.content === 'string'
              const changeMeta = memoryChangeMetadata(item)
              const routineMeta = routinePersistenceMetadata(item)
              const skillMeta = skillPersistenceMetadata(item)
              const validChangeAgent = (id: number) => ownsRoom && !!members.data?.some(member => member.agent_id === id && member.is_active) && !!agents.data?.some(agent => agent.id === id)
              const status = eventStatus(item, t)
              const actorName = item.actor_name_snapshot || item.actor_type
              const isFinalizeRequest = finalizerRequests.has(item.event_id)
              const isAuxiliary = auxiliaryEvent(item, finalizerRequests)
              const searchTarget = !!transcriptSearchTarget && transcriptSearchTarget.event_id === item.event_id && transcriptSearchTarget.seq === item.seq
              const highlighted = transcriptSearchTarget ? searchTarget : highlightedSeq === item.seq
              const attachments = withAttachments ? renderAttachments(item) : null
              if (isAuxiliary) return <article key={item.event_id} id={item.kind === 'failure' ? `room-failure-${item.event_id}` : undefined}
                data-event-seq={item.seq} aria-current={searchTarget ? 'true' : undefined} className={`conversation-auxiliary${highlighted ? ' attention-target' : ''}`}>
                {routineSource && <div className="checkin-source"><strong>{t(routinePayload?.origin === 'test' ? 'routine.sourceTest' : routinePayload?.origin === 'webhook' ? 'webhook.runOrigin' : 'routine.sourceScheduled')}</strong><p>{String(item.payload.content)}</p></div>}
                {checkinSource && <div className="checkin-source"><strong>{t('checkins.sourceLabel')}</strong><p>{String(item.payload.content)}</p></div>}
                {ownerPageRename(item,ownerId)&&<div className="conversation-event__header"><span className="conversation-event__name">{t('pageRename.sourceEvent',{version:String(item.payload.page_version)})}</span><ServerTime className="metadata" value={item.created_at} compact/></div>}
                {pageResultMetadata(item)&&<PageResultCard key={`${readingIdentity}:${roomId}:${item.event_id}`} event={item} ownerId={ownerId} authEpoch={authEpoch} agentName={agents.data?.find(agent=>agent.id===pageResultMetadata(item)?.agent_id)?.name||t('members.fallbackName',{id:String(pageResultMetadata(item)?.agent_id||'')})} updatedBelow={events.some(e=>{const before=pageResultMetadata(item),later=pageResultMetadata(e);return !!before&&!!later&&e.room_id===item.room_id&&e.seq>item.seq&&later.result_id===before.result_id&&later.agent_id===before.agent_id&&later.page_version>before.page_version})} signal={events.filter(e=>pageResultMetadata(e)?.result_id===pageResultMetadata(item)?.result_id).at(-1)?.seq||item.seq} canUse={()=>clipboardOwner()&&!managementOpen&&roomView==='conversation'} onOpen={(id,version)=>{if(clipboardOwner()){const href=resultVersionHref(roomId,id,version);if(href)navigate(href)}}} onSource={(seq,id)=>{if(clipboardOwner())navigateToEvent(seq,id)}} />}
                {renderRoutineReference(item)}{renderBotMessage(item)}{renderBatchEvent(item)}{renderCreation(item)}{botPersistenceMetadata(item) && <BotPersistence event={item} personalAgentId={botConversation.data?.personal_agent_id ?? (validChangeAgent(botPersistenceMetadata(item)!.agent_id) ? botPersistenceMetadata(item)!.agent_id : null)} canUse={() => clipboardOwner() && !managementOpen && roomView === 'conversation'} onOpen={(seq, id) => navigateToEvent(seq, id)} />}{changeMeta && <MemoryChangeEvent event={item} allowed={validChangeAgent(changeMeta.agent_id)} canUse={() => clipboardOwner() && validChangeAgent(changeMeta.agent_id) && !managementOpen && roomView === 'conversation'} onOpen={(seq, id) => navigateToEvent(seq, id)} />}{routineMeta && <RoutinePersistenceEvent event={item} visible={roomView === 'conversation' && !managementOpen} allowed={validChangeAgent(routineMeta.agent_id)} canUse={() => clipboardOwner() && validChangeAgent(routineMeta.agent_id) && !managementOpen && roomView === 'conversation'} onOpen={(seq, id) => navigateToEvent(seq, id)} />}{skillMeta && <SkillPersistenceEvent event={item} allowed={validChangeAgent(skillMeta.agent_id)} canUse={() => clipboardOwner() && validChangeAgent(skillMeta.agent_id) && !managementOpen && roomView === 'conversation'} onOpen={(seq, id) => navigateToEvent(seq, id)} />}
                {!(botMessageMetadata(item) && item.kind === 'activity' && item.payload.category === 'bot_message' || botBatchMetadata(item) && item.kind === 'activity' || botCreationMetadata(item) || botPersistenceMetadata(item) || changeMeta || skillMeta || routineMeta || checkinSource || routineSource || pageResultMetadata(item) || ownerPageRename(item,ownerId)) && <details className="conversation-auxiliary__details"><summary>
                  <span>{auxiliaryEventSummary(item, t, isFinalizeRequest)}</span>
                  <ServerTime value={item.created_at} compact/>
                </summary><dl>
                    {eventTechnicalDetails(item, t, isFinalizeRequest).map(([label, value]) => <Fragment key={label}><dt>{label}</dt><dd>{value}</dd></Fragment>)}
                  </dl>{isFinalizeRequest && <button type="button" className="text-link" onClick={() => { inspectorReturnSeq.current = item.seq; setInspectedRequest(item.event_id) }}>
                    {t('inspector.open')}
                  </button>}</details>}
                {attachments}
              </article>
              // Copy/quote the visible answer; the canonical event and raw exports stay intact.
              const importedSource = memoryImportMessage(item)
              const authoredContent = importedSource?.content ?? visibleMessageTexts.get(item) ?? null
              const isFinalResult = item.actor_type === 'agent' && typeof item.payload.trigger_event_id === 'string'
                && item.actor_id === finalizerRequests.get(item.payload.trigger_event_id)
              const finalSource = isFinalResult ? finalResultSource(item, events) : null
              return <article key={item.event_id} aria-current={searchTarget ? 'true' : undefined} className={`conversation-event${highlighted ? ' conversation-event--target' : ''}`}
                data-event-seq={item.seq} data-authored-seq={authoredContent !== null ? item.seq : undefined}>
                {item.actor_type === 'agent' ? <AgentCharacter agentId={item.actor_id} size="medium" /> : <Avatar name={actorName} tone={eventTone(item)} />}
                <div className="conversation-event__content">
                  <div className="conversation-event__header"><span className="conversation-event__name">{actorName}</span>
                    <ServerTime className="metadata" value={item.created_at} compact/>
                    {filteringTranscript && <button type="button" className="text-link transcript-context-link" onClick={() => navigateToEvent(item.seq, item.event_id, true)}>{t('chat.transcript.context')}</button>}
                    {status && <StatusText tone={status.tone}>{status.label}</StatusText>}
                    {isFinalResult && <StatusText tone={finalSource?.state === 'partial' ? 'warning' : 'neutral'}>{t('finalize.finalResult')}</StatusText>}
                    {authoredContent !== null && <div className="message-actions"><button type="button" className="icon-button message-actions__trigger" aria-label={t('chat.messageAction.menu', { actor: actorName })}
                      aria-haspopup="menu" aria-expanded={openActionSeq === item.seq} onClick={(event) => {
                        actionAnchor.current = event.currentTarget
                        setOpenActionSeq((current) => current === item.seq ? null : item.seq)
                      }}>
                      <MoreHorizontal size={17} aria-hidden="true" /></button>
                      {openActionSeq === item.seq && <AnchoredPopover anchor={actionAnchor.current} boundary={scrollRef.current} menu className="message-actions__menu"
                        label={t('chat.messageAction.menu', { actor: actorName })} contentKey={`${roomActive}:${item.actor_type}:${actorName}`}
                        onClose={(restoreFocus) => { setOpenActionSeq(null); if (restoreFocus && !actionAnchor.current?.closest('[inert]')) actionAnchor.current?.focus() }}>
                        <button role="menuitem" type="button" disabled={messageCopy.busy} onClick={() => copyMessageValue(item.seq, authoredContent, 'copied')}><Copy size={15} aria-hidden="true" />{t('chat.messageAction.copy')}</button>
                        {roomActive && <button role="menuitem" type="button" onClick={() => quoteMessage(item.seq, authoredContent)}><Quote size={15} aria-hidden="true" />{t('chat.messageAction.quote')}</button>}
                        {roomActive && (item.actor_type === 'agent' || item.actor_type === 'user' && item.actor_id === ownerId) && <button role="menuitem" type="button" onClick={() => selectReply(item)}>{t('reply.select')}</button>}
                        {roomActive && item.actor_type==='agent' && <button role="menuitem" type="button" onClick={()=>quoteReply(item,authoredContent)}><Quote size={15} aria-hidden="true" />{t('chat.messageAction.quoteReply')}</button>}
                        {roomActive && resultSource(item, roomId) === 'text' && <button role="menuitem" type="button" onClick={() => saveResultSource(item)}>{t('results.save')}</button>}
                        {item.actor_type === 'user' && <button role="menuitem" type="button" onClick={() => { setOpenActionSeq(null); inspectorReturnSeq.current = item.seq; setInspectedRequest(item.event_id) }}>
                          <ListChecks size={15} aria-hidden="true" />{t('inspector.open')}</button>}
                        <button role="menuitem" type="button" disabled={messageCopy.busy || messagePermalink(item.seq) === null} onClick={() => { const href = messagePermalink(item.seq); if (href) copyMessageValue(item.seq, href, 'linkCopied') }}><LinkIcon size={15} aria-hidden="true" />{t('chat.messageAction.copyLink')}</button>
                        <button role="menuitem" type="button" onClick={()=>manualMessageValue(item.seq,authoredContent,'copied')}>{t('chat.clipboard.messageManual')}</button>
                        <button role="menuitem" type="button" disabled={messagePermalink(item.seq) === null} onClick={()=>{const href=messagePermalink(item.seq);if(href)manualMessageValue(item.seq,href,'linkCopied')}}>{t('chat.clipboard.linkManual')}</button>
                      </AnchoredPopover>}</div>}</div>
                  {item.kind === 'message' && validReply(item.payload.reply_to) && <button type="button" className="text-link" onClick={() => { if (clipboardOwner()) navigateToEvent((item.payload.reply_to as ReplyReference).seq, (item.payload.reply_to as ReplyReference).event_id) }}>{t('reply.source')}</button>}
                  {finalSource && finalSource.state !== 'ready' && <p className="metadata">
                    {t(finalSource.state === 'partial' ? 'finalize.sourcePartial' : 'finalize.sourceUnverified')}{' '}
                    <button type="button" className="text-link" onClick={() => {
                      inspectorReturnSeq.current = item.seq
                      setInspectedRequest(finalSource.sourceRequestId)
                    }}>{t('finalize.sourceReview')}</button>
                  </p>}
                  {item.actor_type === 'agent' && <PeerReply answer={item} events={events} loading={historyState !== 'complete'} onOpen={(seq, eventId) => {
                    setTranscriptQuery(''); setTranscriptActor('all'); navigateToEvent(seq, eventId)
                  }} />}
                  {item.actor_type === 'agent' && <TeammateHandoff event={item} handoffs={teammates.data?.handoffs || []} agents={agents.data || []}
                    current={teammates.isSuccess && !teammates.isError && (!roomActive || roomStatusConnection === 'live')}
                    fetching={teammates.isFetching} failed={teammates.isError} onRead={() => { if (clipboardOwner()) void teammates.refetch({ cancelRefetch: false }) }}
                    onOpen={(seq, eventId) => { if (clipboardOwner()) { setTranscriptQuery(''); setTranscriptActor('all'); navigateToEvent(seq, eventId) } }} />}
                  {messageCopy.pending?.eventId===item.event_id&&<p role="status" className="metadata">{t('chat.clipboard.pending')}</p>}
                  {messageCopy.result?.item.eventId===item.event_id&&<p className="metadata" role={messageCopy.result.status==='failed'?'alert':'status'}>{t(messageCopy.result.status==='failed'?'chat.clipboard.failure':messageCopy.result.item.kind==='link'?'chat.messageAction.linkCopied':'chat.messageAction.copied')}</p>}
                  {messageCopy.manual?.eventId===item.event_id&&<ClipboardTextFallback text={messageCopy.manual.text} label={t(messageCopy.manual.kind==='link'?'chat.clipboard.linkText':'chat.clipboard.messageText',{seq:item.seq})} caption={t('chat.clipboard.manualHelp')} canUse={messageCopy.manualAllowed} onClose={messageCopy.closeManualAndRestore} onInteract={messageCopy.touchManual} busy={messageCopy.busy} onRetry={()=>{if(messageCopy.manual)void messageCopy.copy(messageCopy.manual)}}/>}
                  {importedSource ? <MemoryImportSource {...importedSource} /> : authoredContent !== null ? <MessageContent content={authoredContent} copyScope={`${readingIdentity}:${roomId}:${item.event_id}:${roomView}`} copyAllowed={()=>{
                    const auth=useAuthStore.getState();return auth.user?.id===ownerId&&auth.epoch===authEpoch&&directoryViewGeneration()===readingGeneration&&roomView==='conversation'
                  }}/>
                    : <p className="conversation-event__body">{eventSummary(item, t)}</p>}
                  {roomView === 'conversation' && !managementOpen && !inspectedRequest && item.kind === 'message' && authoredContent !== null && canonicalEventId(item.event_id) && (item.actor_type === 'user' && item.actor_id === ownerId || item.actor_type === 'agent' && typeof item.actor_id === 'number' && item.actor_id > 0) && <MessageThread key={`${readingIdentity}:${roomId}:${location.key}:${item.event_id}:${item.seq}`} roomId={roomId} ownerId={ownerId} root={{event_id:item.event_id,seq:item.seq}} canUse={() => clipboardOwner() && clipboardView.current.roomView === 'conversation' && clipboardView.current.location === location.key && !managementOpen && !inspectedRequest} onOpen={(seq,id) => navigateToEvent(seq,id)} canReply={canSelectReply} onReply={ordinaryRoom && roomActive ? (replyItem) => selectReply(replyItem || item) : undefined} />}
                  {historyState==='complete' && authoredContent!==null && canonicalEventId(item.event_id) && (item.actor_type==='user'&&item.actor_id===ownerId || item.actor_type==='agent'&&typeof item.actor_id==='number'&&item.actor_id>0) && <MessageReactions key={`${readingIdentity}:${roomId}:${item.event_id}`} roomId={roomId} ownerId={ownerId} authEpoch={authEpoch} targetId={item.event_id} observed={reactions.get(item.event_id)} botObserved={botReactions.get(item.event_id)} enabled={roomActive&&connectionState==='live'&&!managementOpen&&!inspectedRequest&&roomView==='conversation'} canUse={()=>clipboardOwner()&&clipboardView.current.roomView==='conversation'&&!managementOpen&&!inspectedRequest}/>}
                  {item.actor_type === 'agent' && (() => { const coverage=fileReadCoverage(item,events,ownerId); if(!coverage)return null; return <p className="metadata">{coverage.state==='unverified'?t('fileQuestion.coverageUnknown'):t(coverage.state==='complete'?'fileQuestion.coverageComplete':'fileQuestion.coveragePartial',{read:coverage.readChars,total:coverage.totalChars,search:coverage.searchUsed?t('fileQuestion.yes'):t('fileQuestion.no')})}</p> })()}
                  {item.actor_type === 'user' && item.actor_id === ownerId && (() => { const meta=fileQuestionMetadata(item); const proof=meta?.proof; if (!meta || !proof || typeof proof !== 'object') return null; const p=proof as Record<string,unknown>; const file=p.file as Record<string,unknown> | undefined, extraction=p.extraction as Record<string,unknown> | undefined, target=p.target as Record<string,unknown> | undefined, image=p.image as Record<string,unknown> | undefined; if(meta.version===2 && image && file && target && image.mime==='image/png' && typeof image.width==='number' && typeof image.height==='number' && typeof image.size_bytes==='number' && typeof file.filename==='string' && typeof target.bot_name==='string' && typeof target.model_id==='string')return <p className="metadata">{t('fileQuestion.imageHistory',{filename:file.filename,width:image.width,height:image.height,size:image.size_bytes,bot:target.bot_name,model:target.model_id})}</p>; return file && extraction && target && typeof file.filename==='string' && typeof extraction.extractor==='string' && typeof extraction.chars==='number' && typeof target.bot_name==='string' && typeof target.model_id==='string' ? <p className="metadata">{t('fileQuestion.history',{filename:file.filename,format:extraction.extractor,chars:extraction.chars,bot:target.bot_name,model:target.model_id})}</p> : null })()}
                  {item.actor_type === 'user' && item.actor_id === ownerId && (() => {
                    const related = (botMessages.data?.messages || []).filter(message => message.root_ref.room_id === roomId && message.root_ref.event_id === item.event_id && message.root_ref.seq === item.seq)
                    const relatedBatches = (botBatches.data?.batches || []).filter(batch => batch.root_ref.room_id === roomId && batch.root_ref.event_id === item.event_id && batch.root_ref.seq === item.seq)
                    const recordedBatchIds = [...new Set(events.map(botBatchMetadata).filter(meta => meta?.root_room_id === roomId && meta.root_request_event_id === item.event_id).map(meta => meta!.batch_id))]
                    const first = related[0], batch = relatedBatches[0]
                    const actionMessageId = first?.message_id || batch?.members[0]?.message_id, rootRef = first?.root_ref || batch?.root_ref
                    return <>
                      <RootBotBatches batches={relatedBatches} recordedBatchIds={recordedBatchIds} current={botBatches.isSuccess && !botBatches.isError && (!roomActive || roomStatusConnection === 'live')}
                        fetching={botBatches.isFetching} failed={botBatches.isError} truncated={!!botBatches.data?.batches_truncated}
                        canUse={() => clipboardOwner() && !managementOpen && roomView === 'conversation'} onRead={() => { if (clipboardOwner()) void botBatches.refetch({ cancelRefetch: false }) }} onOpen={openBotMessageEvent} />
                      {actionMessageId && rootRef && <RootToolActions messageId={actionMessageId} rootRef={rootRef} agents={agents.data || []}
                        pending={related.some(message => ['pending', 'running'].includes(message.state) || ['pending', 'running'].includes(message.reply_state)) || relatedBatches.some(value => ['pending', 'running'].includes(value.state) || ['pending', 'running'].includes(value.reply_state))}
                        canUse={() => clipboardOwner() && !managementOpen && roomView === 'conversation'} />}
                    </>
                  })()}
                  {renderRoutineReference(item)}{renderBotMessage(item)}{renderBatchEvent(item)}{renderCreation(item)}{botPersistenceMetadata(item) && <BotPersistence event={item} personalAgentId={botConversation.data?.personal_agent_id ?? (validChangeAgent(botPersistenceMetadata(item)!.agent_id) ? botPersistenceMetadata(item)!.agent_id : null)} canUse={() => clipboardOwner() && !managementOpen && roomView === 'conversation'} onOpen={(seq, id) => navigateToEvent(seq, id)} />}{changeMeta && <MemoryChangeEvent event={item} allowed={validChangeAgent(changeMeta.agent_id)} canUse={() => clipboardOwner() && validChangeAgent(changeMeta.agent_id) && !managementOpen && roomView === 'conversation'} onOpen={(seq, id) => navigateToEvent(seq, id)} />}{skillMeta && <SkillPersistenceEvent event={item} allowed={validChangeAgent(skillMeta.agent_id)} canUse={() => clipboardOwner() && validChangeAgent(skillMeta.agent_id) && !managementOpen && roomView === 'conversation'} onOpen={(seq, id) => navigateToEvent(seq, id)} />}
                  {messageAction?.seq === item.seq && <p className="metadata message-action-status" role={messageAction.status === 'copyError' || messageAction.status === 'quoteError' || messageAction.status === 'replyUnavailable' || messageAction.status === 'replyConflict' ? 'alert' : 'status'}>
                    {t(`chat.messageAction.${messageAction.status}`)}</p>}
                  {attachments}
                </div>
              </article>
  }
  useFindShortcut(roomView === 'conversation' && !managementOpen && ownsRoom, event => {
    if (Date.now() <= transcriptImeUntil.current || !clipboardOwner()) return false
    const editor = event.target instanceof HTMLElement ? event.target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])') : null
    if (editor && editor !== transcriptInputRef.current) return false
    if (transcriptSearchOpen) { transcriptInputRef.current?.focus({ preventScroll: true }); transcriptInputRef.current?.select() }
    else openTranscriptSearch()
    return true
  })
  return <div className="room-shell room-shell--focused">
    <header className="room-header"><div className="room-header__inner">
      <div className="page-header__identity"><Link to="/projects" aria-label={t('chat.backProjects')} className="icon-button"><ArrowLeft size={21} /></Link>
        <div><h1 className="page-title">{conversation.data?.title || t('chat.roomTitle', { id: String(roomId) })}</h1><p className="page-subtitle room-header__context"><span className="room-header__location">{botConversation.isSuccess && botConversation.data.personal_agent_id !== null ? t('bots.personalConversation') : project.data?.name} · {t('chat.roomTitle', { id: String(roomId) })}</span>
          <span role="status" aria-label={t('chat.roomActivity')} className="room-header__activity">{roomActivityLabel}</span></p></div></div>
      <div className="page-actions room-header__actions">{ordinaryRoom && <RoomMembersSummary roomId={roomId} available={members.isSuccess} members={members.data || []} agents={agents.data || []} actions={activity.data || []} events={events}
        connection={members.isError ? 'retrying' : roomStatusConnection} open={managementOpen} dialogId={`room-management-${roomId}`}
        disabled={!conversation.data} onOpen={() => { setOpenActionSeq(null); setMentionOpen(false); setManagementOpen(true) }} />}{roomCanStop && <button type="button" aria-label={t(roomStopPending ? 'chat.stopSending' : 'chat.stop')} className="button button--danger button--compact" disabled={roomStopPending} onClick={requestStop}>{t(roomStopPending ? 'chat.stopSending' : 'chat.stop')}</button>}
        {ordinaryRoom && botConversation.isSuccess && botConversation.data.personal_agent_id !== null && <button type="button" className="button button--ghost button--compact" onClick={() => { if (clipboardOwner()) setBotMemoryOpen(true) }}>{t('botMemory.title')}</button>}
        {roomActive && <details className="room-header__menu"><summary aria-label={t('chat.ui.more')} title={t('chat.ui.more')}><MoreHorizontal size={19} aria-hidden="true" /></summary><div className="room-header__menu-content"><button type="button" aria-label={t('chat.close')} className="button button--ghost button--compact" disabled={close.isPending||!!closingTarget} onClick={event => { const menu = event.currentTarget.closest('details'); if (menu) menu.open = false; openRoomClose() }}>{t(close.isPending?'chat.closeDialog.sending':'chat.close')}</button></div></details>}
        {roomClosed && <span className="status-text status-text--neutral">{t(projectConfirmedInactive ? 'projects.archivedProject' : 'chat.closedRoom')}</span>}</div>
      <HeaderControlsSlot />
    </div>{ordinaryRoom && (broad.data?.enabled || broad.isError || broadAction.unknown.length > 0) && <div role="status" className="room-header__permission">
      <strong>{!broad.isSuccess ? t('activity.broadUnknown') : broad.data.enabled ? broad.data.expires_at ? t('chat.broadTimed') : t('chat.broadPermanent') : t('activity.broadLastOff')}</strong>
      <button className="button button--ghost button--compact danger-link" disabled={broadAction.busy} onClick={() => broadAction.send('off')}>{t('chat.turnOff')}</button>
      {broadAction.unknown.length > 0 && <span role="alert" title={t('activity.changeUnconfirmed')}>{t('activity.unconfirmedRequests', { count: broadAction.unknown.length })}</span>}
      {broadAction.latest?.status === 'error' && broadAction.latest.failure === 'rejected' && <span role="alert">{t('activity.broadFailure')}</span>}
    </div>}</header>
    <main className="room-layout">


      <div className="room-column">
      <RoomStopNotice state={stopState}/>
      <ScopeStopStatus key={`scope-stop:${readingIdentity}:${roomId}:${roomStop.id || 'history'}`} roomId={roomId} requestId={roomStop.id} stopId={roomStop.scopeStopId} state={stopState} canUse={() => clipboardOwner()} />
      {closeFailure&&<section role="alert" className="notice notice--warning stack stack--tight" style={{flexShrink:0,maxHeight:'min(35dvh,240px)',overflowY:'auto',overflowWrap:'anywhere'}}><p><strong>{closeFailure.target.title}</strong> · {t('chat.roomTitle',{id:String(closeFailure.target.roomId)})}</p><p>{t(closeFailure.phase==='not_sent'?'chat.closeDialog.notSent':'chat.closeDialog.unknown')}</p><div><button type="button" className="text-link" disabled={availabilityRetrying||conversation.isFetching||project.isFetching} onClick={()=>{if(clipboardOwner())void retryRoomAvailability()}}>{t('chat.closeDialog.read')}</button></div></section>}
        {!roomMetadataReady && !roomClosed && <section className="notice notice--warning stack stack--tight" role="status" style={{ flexShrink: 0 }}>
          <strong>{t(availabilityFailed ? 'chat.availability.failed' : 'chat.availability.loading')}</strong>
          <p>{t(availabilityFailed ? 'chat.availability.explain' : 'chat.availability.wait')}</p>
          <div className="inline-actions"><button type="button" className="button button--secondary button--compact" disabled={availabilityRetrying || conversation.isFetching || project.isFetching} onClick={() => { void retryRoomAvailability() }}>{t('chat.availability.retry')}</button><Link className="text-link" to="/projects">{t('chat.backProjects')}</Link></div>
        </section>}
        {!ordinaryRoom && <SharedRoomStatus state={!bindingConfirmed ? (sharedBinding.isError ? 'unknown' : 'checking') : sharedReady ? 'ready' : 'unavailable'} memoryAvailable={sharedReady && sharedBinding.data?.kind === 'shared' && sharedBinding.data.state === 'ready' && sharedBinding.data.shared_private_memory_available} ko={locale === 'ko'} onRefresh={()=>void sharedBinding.refetch()} />}
        {sharedReady && sharedBinding.data?.kind === 'shared' && sharedBinding.data.state === 'ready' && sharedBinding.data.shared_private_memory_available && <SharedPrivateMemory key={`${ownerId}:${authEpoch}:${roomId}:${sharedBinding.data.session_generation}:${sharedBinding.data.account_id}:${sharedBinding.data.publication_id}`} scope={{room_id:roomId,publication_id:sharedBinding.data.publication_id,team_id:sharedBinding.data.team_id,account_id:sharedBinding.data.account_id,issuer:sharedBinding.data.issuer,session_generation:sharedBinding.data.session_generation}} read={readSharedPrivateMemory} ko={locale === 'ko'}/>}
        {sharedReady && sharedBinding.data?.kind === 'shared' && sharedBinding.data.state === 'ready' && sharedBinding.data.profile_id && sharedBinding.data.key_fingerprint && <SharedBotApps key={`shared-apps:${ownerId}:${authEpoch}:${roomId}:${sharedBinding.data.session_generation}:${sharedBinding.data.account_id}:${sharedBinding.data.publication_id}:${sharedBinding.data.profile_id}:${sharedBinding.data.key_fingerprint}`} scope={{owner_id:ownerId,owner_epoch:authEpoch,agent_id:sharedBinding.data.agent_id,profile_id:sharedBinding.data.profile_id,key_fingerprint:sharedBinding.data.key_fingerprint,room_id:roomId,publication_id:sharedBinding.data.publication_id,team_id:sharedBinding.data.team_id,account_id:sharedBinding.data.account_id,issuer:sharedBinding.data.issuer,session_generation:sharedBinding.data.session_generation}} adapter={sharedAppsAdapter} ko={locale === 'ko'} onCapability={setSharedAppsAvailable} onManage={()=>navigate('/apps')}/>}
        {sharedReady && sharedBinding.data?.kind === 'shared' && sharedBinding.data.state === 'ready' && (!sharedBinding.data.profile_id || !sharedBinding.data.key_fingerprint) && <p className="notice">{locale==='ko'?'개인 앱 사용 상태를 확인할 수 없습니다.':'Personal app availability could not be confirmed.'}</p>}
        <nav className="room-result-tabs" aria-label={t('results.views.label')}>
          {ordinaryRoom && <RoomViewTabs value={roomView} baseId={roomViewId} onSelect={switchRoomView}
            canUse={() => clipboardOwner() && !managementOpen && !inspectedRequest} resultHint={resultHint || undefined} memoryHint={memoryHint || undefined}/>}
          <button ref={transcriptButtonRef} type="button" className="button button--ghost button--compact transcript-search-toggle" aria-keyshortcuts="Meta+F Control+F" title={`${t('chat.transcript.search')} · ⌘/Ctrl F`} aria-expanded={transcriptSearchOpen && roomView === 'conversation'} aria-controls="room-transcript-search" onClick={() => transcriptSearchOpen && roomView === 'conversation' ? clearTranscriptSearch(true) : openTranscriptSearch()}><Search size={16} aria-hidden="true" />{t('chat.transcript.search')}{filteringTranscript && <span aria-hidden="true"> · {displayedEvents.length}</span>}</button>
        </nav>
        {!memoryOpened && <section id={`${roomViewId}-panel-memory`} role="tabpanel" aria-labelledby={`${roomViewId}-tab-memory`} hidden/>}
        {!resultsOpened && <section id={`${roomViewId}-panel-results`} role="tabpanel" aria-labelledby={`${roomViewId}-tab-results`} hidden/>}
        {ordinaryRoom && memoryOpened && <RoomMemory panelId={`${roomViewId}-panel-memory`} labelledBy={`${roomViewId}-tab-memory`} roomId={roomId} ownerId={ownerId} authEpoch={authEpoch} active={roomActive} visible={roomView === 'memory'} events={events} agents={agents.data || []}
          onNavigate={(seq, eventId) => { setTranscriptQuery(''); setTranscriptActor('all'); navigateToEvent(seq, eventId) }} />}
        {ordinaryRoom && resultsOpened && <RoomResults panelId={`${roomViewId}-panel-results`} labelledBy={`${roomViewId}-tab-results`} roomId={roomId} ownerId={ownerId} authEpoch={authEpoch} active={roomActive} visible={roomView === 'results'} routeTarget={resultRouteActive ? resultRoute : undefined} routeEntry={location.key}
          events={events} source={resultSourceEvent} onClearSource={(expected) => {
            const auth = useAuthStore.getState()
            if (!mounted.current || auth.user?.id !== ownerId || auth.epoch !== authEpoch) return
            setResultSourceEvent(current => !expected || current?.event_id === expected ? null : current)
          }} onNavigate={(seq, eventId) => { setTranscriptQuery(''); setTranscriptActor('all'); navigateToEvent(seq, eventId) }} pageActions={activity.data||[]} renderPageApproval={inlinePrompt} onPageStop={roomCanStop?requestStop:undefined} onPageClose={()=>{if(clipboardOwner())switchRoomView('conversation')}} pageStopPending={roomStopPending} onPageEvent={event=>{if(clipboardOwner()){roomEventCursor.add(roomId,[event]);setEvents(roomEventCursor.events(roomId));refreshApprovals()}}} />}
        <div id={`${roomViewId}-panel-conversation`} role="tabpanel" aria-labelledby={`${roomViewId}-tab-conversation`} className="room-conversation" hidden={roomView !== 'conversation'}>
        {transcriptSearchOpen && <section id="room-transcript-search" className="transcript-search" aria-label={t('chat.transcript.search')}
          onCompositionStart={() => { transcriptImeUntil.current = Number.POSITIVE_INFINITY }} onCompositionEnd={() => { transcriptImeUntil.current = Date.now() + 120 }}
          onKeyDown={(event) => { if (event.key === 'Escape' && !event.nativeEvent.isComposing && Date.now() > transcriptImeUntil.current) { event.preventDefault(); event.stopPropagation(); clearTranscriptSearch(true) } }}>
          <div className="transcript-search__fields">
            <label className="field">{t('chat.transcript.search')}<input ref={transcriptInputRef} type="search" value={transcriptQuery} aria-keyshortcuts="Enter Shift+Enter" aria-describedby={`room-transcript-search-scope-${roomId} room-transcript-navigation-help-${roomId}`} onChange={(event) => changeTranscriptQuery(event.target.value)}
              onKeyDown={event => {
                if (event.key !== 'Enter') return
                event.stopPropagation()
                if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229 || Date.now() <= transcriptImeUntil.current) return
                event.preventDefault()
                if (event.repeat || event.altKey || event.ctrlKey || event.metaKey) return
                moveTranscriptResult(event.shiftKey ? -1 : 1)
              }} /></label>
            <label className="field">{t('chat.transcript.filter')}<select value={transcriptActor} onChange={(event) => changeTranscriptActor(event.target.value as TranscriptActorFilter)}>
              <option value="all">{t('chat.transcript.all')}</option><option value="people">{t('chat.transcript.people')}</option><option value="agents">{t('chat.transcript.agents')}</option>
            </select></label>
            <button type="button" className="button button--secondary button--compact" onClick={() => clearTranscriptSearch(true)}>{t('chat.transcript.return')}</button>
          </div>
          <p className="metadata" role="status">{t('chat.transcript.results', { count: displayedEvents.length })} · {t('chat.transcript.filteredHint')}</p>
          <div className="page-actions">
            <button type="button" className="button button--secondary button--compact" aria-controls={`room-transcript-events-${roomId}`} disabled={!filteringTranscript || !displayedEvents.length} onClick={() => moveTranscriptResult(-1)}>{t('chat.transcript.previousResult')}</button>
            <button type="button" className="button button--secondary button--compact" aria-controls={`room-transcript-events-${roomId}`} disabled={!filteringTranscript || !displayedEvents.length} onClick={() => moveTranscriptResult(1)}>{t('chat.transcript.nextResult')}</button>
          </div>
          {transcriptMatchIndex >= 0 && <p className="metadata" role="status">{t('chat.transcript.resultPosition', { current: transcriptMatchIndex + 1, total: displayedEvents.length })}</p>}
          <p id={`room-transcript-navigation-help-${roomId}`} className="field-hint">{t(filteringTranscript ? 'chat.transcript.navigationHelp' : 'chat.transcript.navigationStart')}</p>
          {transcriptMoveFailure === transcriptNavContext && <p role="alert" className="notice notice--warning">{t('chat.transcript.navigationMissing')}</p>}
          <p id={`room-transcript-search-scope-${roomId}`} className="field-hint">{t('chat.transcript.visibleScope')}</p>
          {historyState !== 'complete' && <p className="notice notice--warning" role="status">{t('chat.transcript.partialSearch')}</p>}
        </section>}
        {!transcriptSearchOpen && transcriptContextReturn?.atLocation === location.key && <section className="notice notice--info stack stack--tight" aria-label={t('chat.transcript.returnSearch')}>
          <p className="metadata">{t('chat.transcript.returnQuery', { query: transcriptContextReturn.query || t('chat.transcript.all') })} · {t(`chat.transcript.${transcriptContextReturn.actor}`)}</p>
          <div><button type="button" className="button button--secondary button--compact" onClick={restoreTranscriptSearch}>{t('chat.transcript.returnSearch')}</button></div>
          <p className="field-hint">{t('chat.transcript.returnHelp')}</p>
        </section>}
        <div ref={scrollRef} className="room-scroll" onScroll={handleTimelineScroll}
          onWheel={interruptReadingRestore} onTouchStart={interruptReadingRestore} onPointerDownCapture={interruptReadingRestore}
          onKeyDownCapture={(event) => { if (['Tab', 'ArrowDown', 'ArrowUp', 'PageDown', 'PageUp', 'Home', 'End', ' '].includes(event.key)) interruptReadingRestore() }}><div className="room-feed">
          {roomActive && <RecoveryBanner events={events} ownerId={ownerId} identity={readingIdentity} generation={readingGeneration} failureKey={failure?.event_id || null} stopRevision={roomStop.revision} blockedByStop={roomStopPending} canContinue={() => !!clipboardOwner() && readRoomStop(readingIdentity, roomId).phase !== 'sending'} roomId={roomId} uncertain={uncertain} failure={failure ? eventSummary(failure, t) : null}
            onViewContinuation={(seq, eventId) => {
              if (!clipboardOwner()) return
              setOpenActionSeq(null); setMentionOpen(false)
              navigateToEvent(seq, eventId)
            }}
            onViewFailure={failure ? () => {
              if (!clipboardOwner() || !clipboardEvents.current.some(item => item.room_id === roomId && item.kind === 'failure' && item.seq === failure.seq && item.event_id === failure.event_id)) return
              setOpenActionSeq(null); setMentionOpen(false)
              navigateToEvent(failure.seq, failure.event_id)
            } : undefined}
            onReviewConfiguration={configurationFailure ? () => {
              if (!clipboardOwner() || !conversation.data) return
              setOpenActionSeq(null); setMentionOpen(false); setManagementOpen(true)
            } : undefined}
            onContinue={() => { setAcknowledged((old) => [...old, ...uncertain.map((item) => item.action_id)]); cache.invalidateQueries({ queryKey: ['activity', roomId] }) }} />}
          {actionError && <p role="alert" className="notice notice--danger">{t(actionError)}</p>}
          {feedError && <p role="status" className="notice notice--warning">{t(feedError)}</p>}
          {(historyState !== 'complete' || roomActive && connectionState !== 'live') && <section className="notice notice--warning stack stack--tight" role="status" aria-label={t('chat.history.recovery')}>
            <p>{t(historyState === 'loading' ? 'chat.history.loading' : historyState === 'error' ? 'chat.history.partial' : 'chat.history.reconnecting')}</p>
            <p className="metadata">{t('chat.history.preserve')}</p>
            {(historyState === 'error' || connectionState === 'retrying') && <button type="button" className="button button--secondary button--compact" disabled={historyState === 'loading' || !ownsRoom} onClick={() => historyRetryRef.current?.()}>{t('chat.history.retry')}</button>}
          </section>}
          {deepLinkMissing && !filteringTranscript && <p role="status" className="notice notice--warning">{t('chat.messageAction.notFound')}</p>}
          {attentionMissing && <p role="status" className="notice notice--warning">{t('attention.stale')}</p>}
          {attentionAction && activity.isError && <p role="status" className="notice notice--warning">{t('activity.historyFailure')}{attentionRecord && <> {t('activity.lastReceived')}</>} <button type="button" className="text-link" disabled={activity.isFetching} onClick={() => { if (clipboardOwner()) void activity.refetch({ cancelRefetch: false }) }}>{t('activity.retryHistory')}</button></p>}
          {attentionRecord && <section id={`room-action-history-${attentionRecord.action_id}`} className="surface stack attention-target" aria-label={t('activity.record.title')}>
            <header className="split"><h2 className="section-title">{t('activity.record.title')}</h2><button type="button" className="button button--ghost button--compact" onClick={() => {
              if (!clipboardOwner()) return
              const next = new URLSearchParams(searchParams); next.delete('action'); navigate({ search: next.toString() ? `?${next.toString()}` : '' }, { replace: true })
            }}>{t('common.close')}</button></header>
            <p className="field-hint">{t('activity.record.help')}</p>
            <ToolActivityRecord action={attentionRecord} agentName={agents.data?.find(agent => agent.id === attentionRecord.agent_id)?.name || t('members.fallbackName', { id: attentionRecord.agent_id })}/>
          </section>}
          <section id={`room-transcript-events-${roomId}`} aria-label={t('chat.events')} className="room-events">
            {events.length === 0 && !filteringTranscript && <p className="section-copy">{t('chat.empty')}</p>}
            {events.length === 0 && !filteringTranscript && roomActive && botConversation.isSuccess && botConversation.data.personal_agent_id === null && teammates.isSuccess && teammates.data.teammates.length < 2 && <p className="teammate-empty-help metadata">
              {t('teammates.noColleague')}{' '}<button type="button" className="text-link" onClick={() => { if (clipboardOwner()) setManagementOpen(true) }}>{t('teammates.manageColleagues')}</button>
            </p>}
            {filteringTranscript && displayedEvents.length === 0 && <div className="notice stack stack--tight">
              <p role="status">{t('chat.transcript.noMatches')}</p>
              {visibleMessageTexts.size > 0 && transcriptActor !== 'all' && <>
                <div><button type="button" className="button button--secondary button--compact" onClick={() => {
                  if (!clipboardOwner() || managementOpen || roomView !== 'conversation' || !transcriptSearchOpen) return
                  changeTranscriptActor('all')
                  // This action disappears after recovery; keep focus on the query.
                  transcriptInputRef.current?.focus({ preventScroll: true })
                }}>{t('chat.transcript.widenKeepingQuery')}</button></div>
                <p className="field-hint">{t('chat.transcript.widenHelp')}</p>
              </>}
            </div>}
            {timelineGroups.map((group) => {
              if (!group.auxiliary) return renderEvent(group.events[0])
              if (group.events.some(item => botMessageMetadata(item) || botBatchMetadata(item) || botCreationMetadata(item) || botPersistenceMetadata(item) || memoryChangeMetadata(item) || skillPersistenceMetadata(item) || routinePersistenceMetadata(item) || item.kind === 'activity' && item.payload.category === 'primary_checkin' || pageResultMetadata(item) || ownerPageRename(item,ownerId))) return <Fragment key={group.key}>{group.events.map(item => renderEvent(item))}</Fragment>
              const outcome = activityOutcome(group.events)
              const state = eventStatus(outcome, t)
              const first = group.events[0]
              if (outcome.kind === 'failure') {
                const code = String(outcome.payload.code || '')
                const configurationIssue = ['model_feature_unavailable', 'local_model_unavailable', 'model_capabilities_changed', 'missing_secret', 'provider_unavailable', 'provider_invalid_response', 'unsupported_option', 'secret_store_unavailable'].includes(code)
                const connectionId = missingSecretConnectionId(outcome) ?? agents.data?.find(agent => agent.id === outcome.actor_id)?.connection_id
                return <section key={first.event_id} className="conversation-failure">
                  <div className="conversation-failure__main"><strong>{activityActor(outcome)}</strong><p>{eventSummary(outcome, t)}</p>
                    {roomActive && configurationIssue && <div className="conversation-failure__actions">{connectionId ? <Link className="button button--ghost button--compact" to={`/connections?edit=${connectionId}`}>{t('chat.ui.reviewConnection')}</Link>
                      : <button type="button" className="button button--ghost button--compact" onClick={() => { if (clipboardOwner()) { setOpenActionSeq(null); setMentionOpen(false); setManagementOpen(true) } }}>{t('chat.ui.reviewConnection')}</button>}</div>}
                  </div>
                  <details className="conversation-failure__details"><summary aria-label={t('chat.ui.failureDetails')} title={t('chat.ui.failureDetails')}><MoreHorizontal size={17} aria-hidden="true"/></summary><ServerTime value={outcome.created_at} compact />
                    {group.events.map(item => <Fragment key={item.event_id}>{renderEvent(item, false)}
                      {item.kind === 'failure' && <FailureReference event={item} canUse={() => clipboardOwner() && clipboardView.current.roomView === 'conversation' && clipboardEvents.current.some(row => row.room_id === roomId && row.seq === item.seq && row.event_id === item.event_id && row.kind === 'failure')} />}
                    </Fragment>)}
                  </details>
                  {group.events.map(item => <Fragment key={item.event_id}>{renderAttachments(item, item !== outcome && (needsNewActionReview(item) || item.kind === 'tool_result' && typeof item.payload.error_code === 'string' && !!permissionHelp[item.payload.error_code]))}</Fragment>)}
                </section>
              }
              return <section key={first.event_id} className={`activity-group${state ? ` activity-group--${state.tone}` : ''}`}>
                <details className="activity-group__log"><summary>
                  <span>{activityActor(outcome)} · {auxiliaryEventSummary(outcome, t, finalizerRequests.has(outcome.event_id))}</span>
                  {group.events.length > 1 && <span className="activity-group__count">{t('chat.activity.records', { count: group.events.length })}</span>}
                  <ServerTime value={outcome.created_at} compact/>
                </summary>{group.events.map((item) => renderEvent(item, false))}</details>
                {group.events.map((item) => {
                  const code = item.payload.error_code
                  const showGuidance = item === outcome || needsNewActionReview(item) || typeof code === 'string' && !!permissionHelp[code]
                    && group.events.find((event) => event.payload.error_code === code) === item
                  return <Fragment key={item.event_id}>{renderAttachments(item, showGuidance)}</Fragment>
                })}
              </section>
            })}
            {roomActive && pendingApprovals.filter((action) => (ordinaryRoom || sharedReady && sharedAppsAvailable && action.family === 'mcp_tool_call') && (!action.trigger_event_id || !visibleTriggers.has(action.trigger_event_id))).map(inlinePrompt)}
            <ConversationAgents roomId={roomId} active={roomActive} members={members.data || []} agents={agents.data || []}
              actions={activity.data || []} events={events} connection={members.isError || activity.isError ? 'retrying' : members.isLoading ? 'connecting' : roomStatusConnection}
              onAction={(id) => { setTranscriptQuery(''); setTranscriptActor('all'); navigate({ search: `?action=${encodeURIComponent(id)}` }) }}
              onNavigate={(seq, eventId) => { setTranscriptQuery(''); setTranscriptActor('all'); navigateToEvent(seq, eventId) }} />
            <div ref={endRef} className="room-feed-end" />
          </section>
        </div></div>
        {(unreadCount > 0 || readSaveError) && <div className="timeline-status">
          {unreadCount > 0 && <button type="button" className="button button--secondary button--compact" aria-label={t('chat.timeline.jump', { count: unreadCount })}
            onClick={() => { endRef.current?.scrollIntoView({ block: 'end' }); atBottomRef.current = true; window.requestAnimationFrame(acknowledgeVisible) }}>
            {t('chat.timeline.newMessages', { count: unreadCount })}</button>}
          {readSaveError && <span role="status" className="metadata">{t('chat.timeline.saveFailure')} <button type="button" className="text-link" onClick={() => requestReadAck(ackTargetRef.current)}>{t('chat.timeline.retrySave')}</button></span>}
        </div>}
          <FinalizeHistory key={`finalize-history:${readingIdentity}:${roomId}:${readingGeneration}`} identity={readingIdentity} roomId={roomId} onViewEvent={(seq, eventId) => navigateToEvent(seq, eventId)}/>
          <RequestRetryHistory key={`retry-history:${readingIdentity}:${roomId}:${readingGeneration}`} identity={readingIdentity} roomId={roomId} onViewEvent={(seq, eventId) => navigateToEvent(seq, eventId)}/>
          <MessageDeliveryNotice key={`delivery:${readingIdentity}:${roomId}:${readingGeneration}`} items={deliveryMessages} ownerId={ownerId} epoch={authEpoch} generation={readingGeneration}
            onResolveRoutine={async item=>{const originalLocation=location.key;const stillHere=()=>clipboardOwner()&&item.roomId===roomId&&clipboardView.current.location===originalLocation&&clipboardView.current.roomView==='conversation';if(!item.routineRef||!stillHere())throw Error('Routine request unavailable');const result=await eventsApi.resolveRoutine(item.roomId,item.content,item.key,item.routineRef);if(!stillHere())throw Error('View changed');if(result.state==='accepted'){const found=await eventsApi.list(item.roomId,result.seq!-1,1);const event=found[0];if(!stillHere()||!event||event.event_id!==result.event_id||event.seq!==result.seq||!matchesPendingMessage(item,event,ownerId))throw Error('Routine receipt mismatch');roomEventCursor.add(item.roomId,[event]);setEvents(roomEventCursor.events(item.roomId));updatePendingMessage(readingIdentity,item.roomId,item.key,{status:'confirmed',receipt:event},readingGeneration)}else if(result.state==='rejected'){await settleRejectedRoutine(item)}return result.state}}
            busy={roomStopPending || send.isPending || deliveryBusy || !draftReady || !roomActive} canRetry={item => ordinaryRoom && (!item.replyTo || item.stopRevision !== undefined && roomSendAllowed(readingIdentity, item.roomId, item.stopRevision))} onRetry={retryOriginalMessage} onPark={parkUnconfirmedMessage}
            onFocusComposer={() => {
              const auth = useAuthStore.getState()
              if (!mounted.current || auth.user?.id !== ownerId || auth.epoch !== authEpoch) return
              const input = inputRef.current
              if (input?.isConnected && input.getClientRects().length) input.focus({ preventScroll: true })
              else transcriptButtonRef.current?.focus({ preventScroll: true })
            }}
            onFinish={(item) => { void finishConfirmedMessage(item) }} onEvents={(batch) => {
              const auth = useAuthStore.getState()
              if (!mounted.current || auth.user?.id !== ownerId || auth.epoch !== authEpoch) return
              roomEventCursor.add(roomId, batch); setEvents(roomEventCursor.events(roomId))
            }} onView={(event) => navigateToEvent(event.seq, event.event_id)} />
        {ordinaryRoom&&showComposer&&roomView==='conversation'&&!managementOpen&&botConversation.isSuccess&&<div className="room-composer"><RoutineChatPicker key={`${ownerId}:${authEpoch}:${roomId}:${botConversation.data.personal_agent_id??0}`} bots={(agents.data||[]).filter(a=>a.user_id===ownerId&&a.is_active&&(members.data||[]).some(m=>m.room_id===roomId&&m.agent_id===a.id&&m.is_active&&!m.paused)).map(a=>({id:a.id,name:a.name}))} personalAgentId={botConversation.data.personal_agent_id} canUse={()=>clipboardOwner()&&roomActive} disabled={!draftReady||!!draftConflict||draftLoadError||routineHandoffBusy||deliveryBusy||blocksNewMessage(deliveryMessages)||send.isPending||!!replyTo||outputKind!=='auto'||!!fileQuestion.selected} onChoose={r=>{void (async()=>{if(!clipboardOwner()||routineHandoffBusy)return;if(botConversation.data.personal_agent_id===r.agent_id&&r.room_id===roomId){setRoutineSelection(r);setRoutineChanged(false);return}if(botConversation.data.personal_agent_id!==null||!(members.data||[]).some(m=>m.room_id===roomId&&m.agent_id===r.agent_id&&m.is_active&&!m.paused)||!(agents.data||[]).some(a=>a.id===r.agent_id&&a.user_id===ownerId&&a.is_active))return;const text=latestInput.current,edit=composerEditId.current,originalLocation=location.key,originalRoom=roomId,intent=++routineHandoffIntent.current;const stillHere=()=>clipboardOwner()&&routineHandoffIntent.current===intent&&roomId===originalRoom&&clipboardView.current.location===originalLocation&&clipboardView.current.roomView==='conversation';setRoutineHandoffBusy(true);setRoutineHandoffError(false);try{await persistDraft(text);if(!stillHere()||latestInput.current!==text||composerEditId.current!==edit)return;const room=await botsApi.conversation(r.agent_id);if(!stillHere()||latestInput.current!==text||composerEditId.current!==edit||room.room_id!==r.room_id)return;const handoff=saveRoutineHandoff({identity:readingIdentity,room:room.room_id,agent:r.agent_id,routine:r,draft:text});navigate(`/chat/${room.room_id}`,{state:{routineHandoff:handoff}})}catch{if(stillHere())setRoutineHandoffError(true)}finally{if(stillHere())setRoutineHandoffBusy(false)}})()}}/>{routineHandoffError&&<p role="alert">{t('common.error')}</p>}{routineChanged&&<p role="alert">{locale==='ko'?'선택한 루틴을 사용할 수 없어 요청이 접수되지 않았습니다. 다시 선택하거나 선택을 취소해 주세요.':'The selected routine was unavailable and the request was not accepted. Select it again or clear the selection.'} <button type="button" onClick={()=>setRoutineChanged(false)}>{t('common.cancel')}</button></p>}{routineSelection&&<p role="status">{routineSelection.name} · Bot #{routineSelection.agent_id} · {routineSelection.routine_id.slice(-8)} · v{routineSelection.revision} — {locale==='ko'?'루틴 도구가 제공되지 않으면 선택 내용이 Bot에게 전달되지 않을 수 있습니다. 접수는 변경·실행 완료가 아닙니다.':'If routine tools are unavailable, the selection may not reach the Bot. Receipt does not mean the change or run completed.'} <button type="button" onClick={()=>setRoutineSelection(null)}>{t('common.cancel')}</button></p>}{(()=>{const id=location.state?.routineHandoff,agent=botConversation.data.personal_agent_id;const handoff=agent?readRoutineHandoff(id,readingIdentity,roomId,agent):null;return handoff&&<div className="notice notice--info"><p>{handoff.routine.name} · Bot #{handoff.routine.agent_id} · {handoff.routine.routine_id.slice(-8)} · v{handoff.routine.revision} — {locale==='ko'?'개인 대화에서 확인하고 직접 보내세요. 기존 입력이 있으면 그 뒤에 추가합니다.':'Review and send in this personal chat. The draft will be appended to any existing input.'}</p><details><summary>{locale==='ko'?'가져올 초안 확인':'Review draft to insert'}</summary><p style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{handoff.draft}</p></details><button type="button" disabled={!draftReady||!!draftConflict||draftLoadError||deliveryBusy||blocksNewMessage(deliveryMessages)||send.isPending} onClick={()=>{if(!clipboardOwner())return;const next=latestInput.current===handoff.draft?latestInput.current:latestInput.current&&handoff.draft?`${latestInput.current}\n\n${handoff.draft}`:latestInput.current||handoff.draft;if(next.length>65536)return;changeComposerInput(next);setRoutineSelection(handoff.routine);setRoutineChanged(false);clearRoutineHandoff(id);navigate(location.pathname+location.search,{replace:true,state:null});inputRef.current?.focus()}}>{t('routine.insert')}</button> <button type="button" onClick={()=>{if(!clipboardOwner())return;clearRoutineHandoff(id);navigate(location.pathname+location.search,{replace:true,state:null})}}>{t('common.cancel')}</button></div>})()}</div>}
        {ordinaryRoom && showComposer && roomView === 'conversation' && !managementOpen && botConversation.isSuccess && (() => { const handoff = location.state?.routineEdit; return handoff && handoff.ownerId === ownerId && handoff.epoch === authEpoch && handoff.roomId === roomId && handoff.agentId === botConversation.data.personal_agent_id && typeof handoff.name === 'string' && handoff.name.length > 0 && handoff.name.length <= 120 && <div className="notice notice--info"><span>{t('routine.draftHelp')}</span> <button type="button" className="text-link" disabled={!draftReady || !!draftConflict || draftLoadError || fileQuestion.busy || fileQuestion.pending} onClick={() => { if (!clipboardOwner() || !draftReady || draftConflictRef.current || draftLoadError) return; const addition=t('routine.draftTemplate',{name:handoff.name}); const next=latestInput.current ? `${latestInput.current}\n\n${addition}` : addition; if (next.length > 65536) return; changeComposerInput(next); navigate(location.pathname+location.search,{replace:true,state:null}); inputRef.current?.focus() }}>{t('routine.insert')}</button></div> })()}
        {ordinaryRoom && showComposer && roomView === 'conversation' && roomActive && <div className="room-composer" style={{minHeight:0}}><details className="compact-details" open={fileQuestion.preview || undefined}><summary>{t('fileQuestion.title')}</summary><div style={{maxHeight:'30vh',overflowY:'auto'}}><FileQuestionComposer key={`${ownerId}:${authEpoch}:${roomId}`} roomId={roomId} ownerId={ownerId} authEpoch={authEpoch} ref={fileQuestionRef} integrated controlledContent={input} externalDisabled={!!replyTo || roomStopPending || !roomActive || !draftReady || draftLoadError || deliveryBusy || blocksNewMessage(deliveryMessages) || send.isPending || !!draftConflict} onIntegratedState={next=>setFileQuestion(old=>Object.keys(next).every(key=>old[key as keyof FileQuestionState]===next[key as keyof FileQuestionState])?old:next)} onAccepted={hash=>{void (async()=>{const value=latestInput.current, editId=composerEditId.current, originLocation=location.key;const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value));const actual=Array.from(new Uint8Array(bytes)).map(x=>x.toString(16).padStart(2,'0')).join('');const auth=useAuthStore.getState();if(actual!==hash||!clipboardOwner()||clipboardView.current.location!==originLocation||auth.user?.id!==ownerId||auth.epoch!==authEpoch||latestInput.current!==value||composerEditId.current!==editId)return;changeComposerInput('');try{await persistDraft('')}catch{if(mounted.current)setDraftSaveError(true)}})()}} bots={(agents.data || []).filter(a=>a.user_id===ownerId && a.is_active && (members.data || []).some(m=>m.room_id===roomId && m.agent_id===a.id && m.is_active)).map(a=>({id:a.id,name:a.name}))} onEvent={event=>{const auth=useAuthStore.getState();if(!mounted.current||auth.user?.id!==ownerId||auth.epoch!==authEpoch)return;roomEventCursor.add(roomId,[event]);setEvents(roomEventCursor.events(roomId))}} /></div></details></div>}
        {ordinaryRoom && roomActive && roomView === 'conversation' && !managementOpen && !inspectedRequest && <SkillRunComposer
          key={`${readingIdentity}:${roomId}:${location.key}:${roomStop.revision}`} roomId={roomId} ownerId={ownerId} epoch={authEpoch}
          bots={(members.data || []).filter(member => member.is_active && !member.paused).flatMap(member => { const agent=(agents.data || []).find(item => item.id===member.agent_id && item.is_active); return agent ? [{id:agent.id,name:agent.name}] : [] })}
          canUse={() => clipboardOwner() && clipboardView.current.location === location.key && clipboardView.current.roomView === 'conversation'
            && roomActive && ordinaryRoom && roomSendAllowed(readingIdentity,roomId,roomStop.revision) && !roomStopPending && !send.isPending && !deliveryBusy && !blocksNewMessage(deliveryMessages) && !fileQuestion.selected && !fileQuestion.busy && !fileQuestion.pending}
          onEvent={event => { if (clipboardOwner() && event.room_id===roomId) { roomEventCursor.add(roomId,[event]); setEvents(roomEventCursor.events(roomId)) } }}/>}
        {showComposer ? <form onSubmit={submit} className={`room-composer${composerExpanded ? ' room-composer--expanded' : ''}`}>
          {replyTo && <div className="notice notice--info"><button type="button" className="text-link" onClick={() => { if (clipboardOwner()) navigateToEvent(replyTo.seq,replyTo.event_id) }}>{t('reply.source')}{(() => { const source = events.find(row => row.room_id === roomId && row.event_id === replyTo.event_id && row.seq === replyTo.seq); const text = source && visibleMessageTexts.get(source); return source && text ? ` · ${source.actor_name_snapshot}: ${Array.from(text.replace(/\s+/g,' ').trim()).slice(0,100).join('')}` : '' })()}</button> <button type="button" className="text-link" disabled={send.isPending || deliveryBusy || blocksNewMessage(deliveryMessages)} onClick={() => { if (clipboardOwner() && !composerComposing.current) changeReply(null) }}>{t('reply.remove')}</button></div>}
          {replyUnavailable && <p role="alert">{t('reply.unavailable')}</p>}
          {replyTo && <p className="metadata">{t('reply.routingHelp')}</p>}
          {replyTo && fileQuestion.selected && <p role="alert">{t('reply.fileBlocked')}</p>}
          {fileQuestion.selected && <div className="notice notice--info"><span>{fileQuestion.filename}{fileQuestion.botName ? ` · ${fileQuestion.botName}` : ''}</span> {!fileQuestion.pending && <button type="button" className="text-link" disabled={fileQuestion.busy || send.isPending || deliveryBusy || blocksNewMessage(deliveryMessages)} onClick={()=>fileQuestionRef.current?.clear()}>{t('fileQuestion.clear')}</button>}</div>}
          {fileQuestion.selected && !fileQuestion.pending && Array.from(input).length > 4000 && <p role="alert">{t('fileQuestion.tooLong')}</p>}
          {fileQuestion.selected && !fileQuestion.pending && outputKind !== 'auto' && outputKind !== 'text' && <p role="alert">{t('fileQuestion.outputBlocked')}</p>}
          <div className="room-composer__main"><div className="room-composer__input"><label className="sr-only" htmlFor="room-message">{t('chat.message')}</label>
            <div className="mention-autocomplete"><textarea ref={inputRef} id="room-message" rows={2} role="combobox" disabled={fileQuestion.busy || fileQuestion.pending} aria-invalid={composerTooLong || undefined} value={input}
              aria-autocomplete="list" aria-controls={mentionListId} aria-expanded={mentionOpen && mentionSuggestions.length > 0}
              aria-activedescendant={activeMentionIndex >= 0 ? `${mentionListId}-${activeMentionIndex}` : undefined}
              aria-describedby={`${mentionListId}-shortcut ${mentionListId}-input-count ${mentionListId}-input-rule`} placeholder={t('chat.placeholder')}
              onChange={(event) => updateComposerValue(event.currentTarget)}
              onCompositionStart={() => { composerComposing.current = true; composerImeUntil.current = 0 }}
              onCompositionEnd={(event) => { composerComposing.current = false; composerImeUntil.current = performance.now() + 100; updateComposerValue(event.currentTarget) }}
              onSelect={(event) => { const blocked = suppressedSelection.current; const caret = event.currentTarget.selectionStart ?? event.currentTarget.value.length
                if (blocked?.value === event.currentTarget.value && blocked.caret === caret) { suppressedSelection.current = null; return }
                suppressedSelection.current = null; updateMention(event.currentTarget.value, caret, false) }}
              onPointerDown={() => { suppressedSelection.current = null }}
              onFocus={(event) => { if (!suppressedSelection.current) updateMention(event.currentTarget.value, event.currentTarget.selectionStart) }}
              onBlur={() => { composerComposing.current = false; composerImeUntil.current = 0; setMentionOpen(false) }} onKeyDown={handleComposerKeyDown} />
              {ordinaryRoom && mentionOpen && mentionToken && mentionSuggestions.length > 0 && <AnchoredPopover anchor={inputRef.current} className="mention-popover" preferred="above" align="start"
                minWidth={180} maxWidth={360} maxHeight={220} contentKey={mentionSuggestions.map(group => `${group.name}:${group.agentIds.join(',')}`).join('|')}
                onClose={() => setMentionOpen(false)}><ul id={mentionListId} role="listbox" aria-label={t('chat.routing.label')} aria-describedby={`${mentionListId}-identity`} className="mention-suggestions">
                {mentionSuggestions.map((group, index) => <li id={`${mentionListId}-${index}`} role="option" aria-selected={index === activeMentionIndex}
                  aria-label={`${t('chat.routing.mention', { name: group.name })} · ${mentionIdentity(group)}`}
                  ref={index === activeMentionIndex ? activeMentionRef : undefined}
                  className={index === activeMentionIndex ? 'mention-suggestion mention-suggestion--active' : 'mention-suggestion'}
                  key={`${group.name}:${group.agentIds.join(',')}`} onPointerDown={(event) => { event.preventDefault(); selectMention(group) }}>
                  <div className="mention-suggestion__identity"><span>@{group.name}</span><span className="metadata">{mentionIdentity(group)}</span></div>
                </li>)}
              </ul><p id={`${mentionListId}-identity`} className="field-hint" style={{ padding: '0 10px 8px', margin: 0 }}>{t('chat.routing.identityHelp')}</p></AnchoredPopover>}
            </div>
            <button aria-label={fileQuestion.selected ? t(fileQuestion.pending ? 'fileQuestion.check' : fileQuestion.preview ? fileQuestion.imagePreview ? 'fileQuestion.imageSend' : 'fileQuestion.send' : 'fileQuestion.preview') : sendError ? t('chat.retrySend') : t('chat.send')} type="submit" disabled={routineChanged || !!routineSelection && (!!replyTo || outputKind!=='auto' || !!fileQuestion.selected) || sharedReady && (!!replyTo || !['auto','text'].includes(outputKind)) || replyUnavailable || !!replyTo && (fileQuestion.selected || historyState !== 'complete') || roomStopPending || !roomActive || !draftReady || draftLoadError || composerTooLong || deliveryBusy || blocksNewMessage(deliveryMessages) || send.isPending || fileQuestion.busy || !!draftConflict || (fileQuestion.selected ? !fileQuestion.pending && (fileQuestion.consentNeeded || !input.trim() || Array.from(input).length > 4000 || (outputKind !== 'auto' && outputKind !== 'text')) : !input.trim() || unmatchedMention)} className="button button--primary"><Send size={18} /><span>{fileQuestion.selected ? t(fileQuestion.pending ? 'fileQuestion.check' : fileQuestion.preview ? fileQuestion.imagePreview ? 'fileQuestion.imageSend' : 'fileQuestion.send' : 'fileQuestion.preview') : sendError ? t('common.retry') : send.isPending ? t('chat.sending') : t('chat.send')}</span></button></div></div>
          {ordinaryRoom && roomView === 'conversation' && !managementOpen && !inspectedRequest && botConversation.isSuccess && botConversation.data.personal_agent_id !== null && <VoiceCallStart roomId={roomId} agentId={botConversation.data.personal_agent_id} title={conversation.data?.title || ''} canStart={() => clipboardOwner() && clipboardView.current.location === location.key && clipboardView.current.roomView === 'conversation' && roomWriteReady.current && draftReady && !draftLoadError && !draftConflictRef.current && !latestInput.current.trim() && !replyRef.current && !dictationActiveRef.current && !roomStopPending && stopState !== 'unknown' && !send.isPending && !deliveryBusy && !blocksNewMessage(deliveryMessages) && !fileQuestion.selected && !fileQuestion.busy && !fileQuestion.pending && !composerComposing.current} />}
          {dictationActive && <p role="status" className="field-hint">{locale === 'ko' ? '통화를 시작하려면 먼저 음성 입력을 마치거나 취소하세요.' : 'Finish or cancel dictation before starting a call.'}</p>}
          {ordinaryRoom && roomView === 'conversation' && !managementOpen && !inspectedRequest && <VoiceInput key={`${ownerId}:${authEpoch}:${roomId}:${location.key}`}
            canUse={() => callView.call?.status !== 'active' && !['creating', 'stopping', 'unknown'].includes(callView.phase) && clipboardOwner() && clipboardView.current.location === location.key && clipboardView.current.roomView === 'conversation' && roomWriteReady.current && draftReady && !draftLoadError && !draftConflictRef.current && !roomStopPending && !send.isPending && !deliveryBusy && !blocksNewMessage(deliveryMessages) && !fileQuestion.selected && !fileQuestion.busy && !fileQuestion.pending && !composerComposing.current && performance.now() >= composerImeUntil.current}
            onActiveChange={active => { dictationActiveRef.current = active; setDictationActive(active) }}
            scope={() => { const auth = useAuthStore.getState(); return auth.user?.id === ownerId && auth.epoch === authEpoch && clipboardOwner() && clipboardView.current.location === location.key ? `${ownerId}:${authEpoch}:${roomId}:${readingGeneration}:${location.key}:${composerEditId.current}` : null }}
            onInsert={text => { const next = latestInput.current ? `${latestInput.current}\n${text}` : text; if (next.length > 65536) return false; changeComposerInput(next); draft.current = null; inputRef.current?.focus(); return true }} />}
          {ordinaryRoom && roomView === 'conversation' && !managementOpen && botConversation.isSuccess && botConversation.data.personal_agent_id !== null && <VoiceCallHistory key={`${ownerId}:${authEpoch}:${roomId}`} roomId={roomId} ownerId={ownerId} epoch={authEpoch} />}
          <div className="room-composer__footer"><details className="composer-options" onToggle={event => { if (event.target === event.currentTarget) setComposerOptionsOpen(event.currentTarget.open) }}><summary>{t('chat.ui.composerOptions')}</summary><div className="composer-options__body">{ordinaryRoom && <label className="metadata local-output-kind">{t('localModels.outputKind')}<select aria-label={t('localModels.outputKind')} value={outputKind} disabled={send.isPending || fileQuestion.busy || fileQuestion.pending} onChange={e=>{setOutputKind(e.target.value as typeof outputKind);setSendError('')}}><option value="auto">{t('localModels.autoText')}</option><option value="text">{t('localModels.textOnly')}</option><option value="image_generation" disabled>{t('localModels.imageUnavailable')}</option><option value="speech_generation" disabled>{t('localModels.speechUnavailable')}</option></select></label>}<p id={`${mentionListId}-shortcut`} className="metadata room-composer__help">{t('chat.composer.help')}</p>
            {ordinaryRoom && <details className="composer-routing-help"><summary>{t('chat.routing.more')}</summary><p className="metadata">{t('chat.routing.help')}</p></details>}
            {ordinaryRoom && <ContextPreview roomId={roomId} ownerId={ownerId} authEpoch={authEpoch} content={input} replyTo={replyTo} sourceVersion={contextSourceVersion}
              recipientIdentity={contextRecipientIdentity} active={!replyUnavailable && !fileQuestion.selected && roomActive && historyState === 'complete' && !unmatchedMention && !composerTooLong}
              visible={composerOptionsOpen && roomView === 'conversation' && !managementOpen && !inspectedRequest && clipboardOwner()} />}
            <button type="button" className="button button--ghost button--compact" aria-pressed={composerExpanded} aria-controls="room-message" title={t('chat.composer.heightHelp')}
              onClick={() => { if (clipboardOwner() && clipboardView.current.roomView === 'conversation') setComposerExpanded(value => !value) }}>{t(composerExpanded ? 'chat.composer.compact' : 'chat.composer.expand')}</button>
            {input && !replyDeliveryPending && !fileQuestion.busy && !fileQuestion.pending && <button type="button" className="button button--ghost button--compact" aria-expanded={!!clearDraftChoice && clearDraftChoice.editId === composerEditId.current && clearDraftChoice.locationKey === location.key}
              aria-controls={clearDraftChoice?.editId === composerEditId.current && clearDraftChoice.locationKey === location.key ? `${mentionListId}-clear-draft` : undefined}
              onClick={() => { if (clipboardOwner() && clipboardView.current.roomView === 'conversation' && latestInput.current === input) setClearDraftChoice({ editId: composerEditId.current, locationKey: location.key }) }}>
              <X size={14} aria-hidden="true" />{t('chat.composer.clear')}
            </button>}</div></details></div>
          {input && !replyDeliveryPending && !fileQuestion.busy && !fileQuestion.pending && clearDraftChoice?.editId === composerEditId.current && clearDraftChoice.locationKey === location.key && <section id={`${mentionListId}-clear-draft`} className="notice notice--warning stack stack--tight" aria-label={t('chat.draftClear.title')}>
            <p role="status">{t('chat.draftClear.help', { count: Array.from(input).length })}</p>
            <div className="inline-actions">
              <button type="button" className="button button--secondary button--compact" onClick={() => {
                if (!clipboardOwner()) return
                setClearDraftChoice(null)
                const field = inputRef.current
                if (field?.isConnected && field.getClientRects().length && !field.closest('[hidden], [inert], [aria-hidden="true"]')) { field.focus({ preventScroll: true }); setMentionOpen(false) }
              }}>{t('chat.draftClear.keep')}</button>
              <button type="button" className="button button--ghost button--compact" onClick={() => {
                const field = inputRef.current
                if (!clipboardOwner() || clipboardView.current.roomView !== 'conversation' || clipboardView.current.location !== clearDraftChoice.locationKey
                  || composerEditId.current !== clearDraftChoice.editId || latestInput.current !== input || !field?.isConnected || !field.getClientRects().length
                  || field.closest('[hidden], [inert], [aria-hidden="true"]')) return
                setClearDraftChoice(null)
                changeComposerInput(''); changeReply(null); draft.current = null
                if (draftReady && roomActive) void persistDraft('', false, null).catch(() => {})
                field.focus({ preventScroll: true }); setMentionOpen(false)
              }}>{t('chat.draftClear.confirm')}</button>
            </div>
          </section>}
          <p id={`${mentionListId}-input-count`} className={`${composerCharacters < 60000 && !composerOptionsOpen ? 'sr-only' : 'metadata'}${composerTooLong ? ' metadata--danger' : ''}`}>{t('chat.composer.inputCount', { count: formatNumber(composerCharacters), limit: formatNumber(65536) })}</p>
          <p id={`${mentionListId}-input-rule`} className={composerOptionsOpen ? 'field-hint' : 'sr-only'}>{t('chat.composer.inputRule')}</p>
          {composerTooLong && <p role="alert" className="notice notice--warning">{t('chat.composer.inputTooLong', { limit: formatNumber(65536) })}</p>}
          {(!roomActive || !draftReady && !draftLoadError) && <p role="status" className="metadata">{t(!roomActive ? 'chat.availability.composePaused' : 'chat.draft.loading')}</p>}
          {draftLoadError && <div role="alert" className="notice notice--warning draft-conflict"><p>{t('chat.draft.loadFailure')}</p>
            <button type="button" className="button button--secondary button--compact" onClick={() => { const auth = useAuthStore.getState(); if (auth.epoch === authEpoch && auth.user?.id === ownerId) setDraftReloadAttempt(value => value + 1) }}>{t('chat.draft.retryLoad')}</button>
          </div>}
          {unmatchedMention && <p role="alert" className="metadata metadata--danger">{t('chat.routing.unmatched')}</p>}
          {draftConflict && (draftConflict.reply_to || replyTo) && <p className="metadata">{t('reply.conflict')} · {t('reply.saved')}: {draftConflict.reply_to ? `#${draftConflict.reply_to.seq}` : '—'} · {t('reply.local')}: {replyTo ? `#${replyTo.seq}` : '—'}</p>}
          {draftConflict && <ComposerDraftConflict saved={draftConflict} content={input} editId={composerEditId.current} canSave={roomActive && draftReady && !composerTooLong && !replyDeliveryPending}
            canUse={() => !replyDeliveryPending && clipboardOwner() && clipboardView.current.roomView === 'conversation' && clipboardView.current.location === location.key}
            onKeep={() => {
              if (!clipboardOwner()) return
              const field = inputRef.current
              if (field?.isConnected && field.getClientRects().length && !field.closest('[hidden], [inert], [aria-hidden="true"]')) { field.focus({ preventScroll: true }); setMentionOpen(false) }
            }}
            onApply={async (choice, received, editId) => {
              if (replyDeliveryPending || !clipboardOwner() || clipboardView.current.roomView !== 'conversation' || clipboardView.current.location !== location.key
                || draftConflictRef.current !== received || composerEditId.current !== editId || latestInput.current !== input) throw new Error('Draft choice scope changed')
              if (choice === 'saved') {
                changeComposerInput(received.content); changeReply(received.reply_to ?? null)
                draft.current = received.idempotency_key ? { content: received.content, key: received.idempotency_key } : null
                lastPersistedContent.current = received.content; lastPersistedReply.current = received.reply_to ?? null; draftConflictRef.current = null; setDraftConflict(null)
                const field = inputRef.current
                if (field?.isConnected && field.getClientRects().length && !field.closest('[hidden], [inert], [aria-hidden="true"]')) { field.focus({ preventScroll: true }); setMentionOpen(false) }
              } else {
                if (!roomWriteReady.current || !draftReady) throw new Error('Draft saving is not ready')
                try { await persistDraft(input, true) }
                catch (error) { if (!draftConflictRef.current) setDraftSaveError(true); throw error }
              }
            }} />}
          {draftSaveError && !draftConflict && <div role="status" className="notice notice--warning draft-conflict"><p>{t('chat.draft.saveFailure')}</p><div className="inline-actions">
            <button type="button" className="button button--secondary button--compact" disabled={!roomActive || !draftReady} onClick={() => { if (!roomWriteReady.current) return; void persistDraft(input).catch(() => setDraftSaveError(true)) }}>{t('chat.draft.retrySave')}</button>
          </div></div>}
          {(draftLoadError || draftSaveError || draftConflict) && <p role="status" className="metadata">{t('chat.draft.localOnly')}</p>}

          {roomStopPending && <p role="status" className="field-hint">{t('chat.stopRequest.waitBeforeSend')}</p>}
          {sendError && <p role="alert" className="metadata">{sendError==='chat.sendUnconfirmed'&&deliveryMessages.some(item=>!!item.routineRef&&item.status==='unknown')?(locale==='ko'?'메시지 접수 여부를 확인하지 못했습니다.':'Message receipt is unconfirmed.'):t(sendError)}</p>}
        </form> : roomClosed && <div className="room-readonly" role="status"><strong>{t(projectConfirmedInactive ? 'projects.archivedProject' : 'chat.closedRoom')}</strong><span>{t(projectConfirmedInactive ? 'chat.availability.projectReadOnly' : 'chat.closedReadOnly')}</span></div>}
        </div>
      </div>
      {closingTarget&&<RoomCloseDialog target={closingTarget} room={conversation.data} project={project.data} ready={roomMetadataReady} reading={availabilityRetrying||conversation.isFetching||project.isFetching} draftCharacters={Array.from(input).length} unconfirmedMessages={deliveryMessages.filter(item=>item.status!=='confirmed').length} canUse={()=>clipboardOwner()&&closingTargetRef.current===closingTarget} onRead={()=>{if(clipboardOwner())void retryRoomAvailability()}} onClose={cancelRoomClose} onConfirm={()=>confirmRoomClose(closingTarget)}/>}
      {ordinaryRoom && managementOpen && <RoomManagementDialog id={`room-management-${roomId}`} onClose={closeManagement}
        onStop={roomCanStop ? requestStop : undefined} stopPending={roomStopPending}>
        <RoomMembers key={`${ownerId}:${authEpoch}:${roomId}`} roomId={roomId} agents={agents.data || []} actions={activity.data || []} events={events} connection={roomStatusConnection} readOnly={!roomActive}
          agentRead={{ fetching: agents.isFetching, failed: agents.isError, ready: agents.isSuccess, refresh: () => { if (clipboardOwner()) void agents.refetch({ cancelRefetch: false }) } }}
          onNavigate={(seq, eventId) => { setTranscriptQuery(''); setTranscriptActor('all'); navigateToEvent(seq, eventId) }}
          onAction={(id) => { setManagementOpen(false); setTranscriptQuery(''); setTranscriptActor('all'); navigate({ search: `?action=${encodeURIComponent(id)}` }) }} />
        {roomActive && <Activity roomId={roomId} roomName={conversation.data?.title || t('chat.roomTitle', { id: String(roomId) })} agents={agents.data || []} onStop={requestStop} stopPending={roomStopPending} stopState={stopState} onInspect={id => { if (!clipboardOwner()) return; setManagementOpen(false); setTranscriptQuery(''); setTranscriptActor('all'); navigate({ search: `?action=${encodeURIComponent(id)}` }) }} />}
        {actionError && <p role="alert" className="notice notice--warning">{t(actionError)}</p>}
          <div className="room-tools"><details name="room-panel" className="transcript-tools" onToggle={event=>{if(event.target!==event.currentTarget)return;closeOtherRoomPanels(event.currentTarget);if(!event.currentTarget.open)transcriptCopy.invalidate()}}><summary><Search size={16} aria-hidden="true" />{t('chat.transcript.title')}</summary>
          <div className="transcript-tools__body">
            <button type="button" className="button button--secondary" onClick={openTranscriptSearch}><Search size={16} aria-hidden="true" />{t('chat.transcript.search')}</button>
            <TranscriptExportOptions scope={transcriptExportScope} onChange={changeTranscriptExport} disabled={transcriptCopy.busy}/>
            {isTauri() && <TranscriptFileSave key={`transcript-save:${readingIdentity}:${roomId}:${location.key}:${transcriptExportContext}`} ownerId={ownerId} epoch={authEpoch} roomId={roomId} ready={historyState==='complete'&&ownsRoom&&transcriptExportEvents.length>0} snapshot={()=>{const original=transcriptSnapshot();return original?{text:original.text,title:conversation.data?.title||t('chat.roomTitle',{id:String(roomId)}),capturedAt:original.capturedAt,scope:original.transcriptScope}:null}} canUse={()=>clipboardOwner()&&managementOpen&&ownsRoom}/>}
            {!isTauri() && <button type="button" className="button button--secondary" disabled={historyState !== 'complete'||!transcriptExportEvents.length} onClick={exportTranscript}>
              <Download size={16} aria-hidden="true" />{t('chat.transcript.export')}
            </button>}
            <button ref={transcriptCopyButton} type="button" className="button button--secondary" disabled={historyState !== 'complete'||!transcriptExportEvents.length||transcriptCopy.busy} onClick={copyTranscript}>
              <Copy size={16} aria-hidden="true" />{t('chat.transcript.copy')}
            </button>
            {historyState === 'loading' && <p role="status" className="metadata">{t('chat.transcript.loading')}</p>}
            {historyState === 'error' && <p role="status" className="notice notice--warning">{t('chat.transcript.incomplete')}</p>}
            <button type="button" className="text-link" disabled={historyState!=='complete'||!transcriptExportEvents.length} onClick={manualTranscript}>{t('chat.clipboard.transcriptManual')}</button>
            {transcriptDownload&&<p role={transcriptDownload==='requested'?'status':'alert'} className="metadata">{t(transcriptDownload==='requested'?'chat.exportScope.downloadRequested':'chat.exportScope.downloadFailed')}</p>}
            {transcriptCopy.busy&&<p role="status" className="metadata">{t('chat.clipboard.pending')}</p>}
            {transcriptCopy.result?.status==='copied'&&<p role="status" className="metadata">{t('chat.transcript.copied')}</p>}
            {transcriptCopy.result?.status==='failed'&&<p role="alert" className="notice notice--warning">{t('chat.clipboard.failure')}</p>}
            {transcriptCopy.manual&&<ClipboardTextFallback text={transcriptCopy.manual.text} label={t('chat.clipboard.transcriptText')} caption={t('chat.clipboard.transcriptSnapshot',{time:formatDateTime(transcriptCopy.manual.capturedAt,{dateStyle:'medium',timeStyle:'medium'})})+(transcriptCopy.manual.transcriptScope?' · '+describeTranscriptExport(transcriptCopy.manual.transcriptScope,t):'')} canUse={transcriptCopy.manualAllowed} onClose={transcriptCopy.closeManualAndRestore} onInteract={transcriptCopy.touchManual} busy={transcriptCopy.busy} onRetry={()=>{if(transcriptCopy.manual)void transcriptCopy.copy(transcriptCopy.manual)}}/>}
          </div>
        </details>
        {conversation.data && <RoomPlans key={`plans:${roomId}:${ownerId}:${authEpoch}`} roomId={roomId} readOnly={!roomActive} events={events} visibleMessageTexts={visibleMessageTexts} historyComplete={historyState==='complete'} onNavigate={(seq,eventId)=>{if(!clipboardOwner())return;setManagementOpen(false);setTranscriptQuery('');setTranscriptActor('all');navigateToEvent(seq,eventId)}} />}
        {conversation.data && <BrowserProfiles key={`browser:${roomId}:${ownerId}:${authEpoch}`} roomId={roomId} readOnly={!roomActive} />}
        {conversation.data && <RoomScheduleSettings key={`policy:${roomId}:${conversation.data.user_id}:${authEpoch}`}
          roomId={roomId} active={roomActive} authEpoch={authEpoch} />}
        {conversation.data && <ScreenshotGallery key={`gallery:${roomId}:${conversation.data.user_id}:${authEpoch}`}
          roomId={roomId} ownerId={ownerId} authEpoch={authEpoch} readOnly={!roomActive} knownEvents={events} historyComplete={historyState === 'complete'} onNavigateSeq={navigateToEvent} />}
        <ResponseComparison events={events} historyState={historyState} onNavigateSeq={navigateToEvent} />
        {conversation.data && <HandoffNote key={`handoff:${roomId}:${conversation.data.user_id}:${authEpoch}`}
          roomId={roomId} active={roomActive} authEpoch={authEpoch} events={events} historyState={historyState}
          onViewSource={(seq, eventId) => { if (clipboardOwner() && clipboardView.current.location === location.key) navigateToEvent(seq, eventId) }}
          copyAllowed={() => clipboardOwner() && managementOpen && clipboardView.current.location === location.key
            && conversation.data?.id === roomId && conversation.data.user_id === ownerId}
          onSaved={(saved) => {
            const auth = useAuthStore.getState()
            if (!mounted.current || auth.user?.id !== ownerId || auth.epoch !== authEpoch || saved.room_id !== roomId) return
            roomEventCursor.add(roomId, [saved]); setEvents(roomEventCursor.events(roomId))
          }} onEvents={(batch) => {
            const auth = useAuthStore.getState()
            if (!mounted.current || auth.user?.id !== ownerId || auth.epoch !== authEpoch || batch.some(event => event.room_id !== roomId)) return
            roomEventCursor.add(roomId, batch); setEvents(roomEventCursor.events(roomId))
          }} />}</div>
      </RoomManagementDialog>}
      {botMemoryOpen && botConversation.isSuccess && botConversation.data.personal_agent_id !== null && <AgentMemoryDialog
        key={`${readingIdentity}:${roomId}:${botConversation.data.personal_agent_id}`} agentId={botConversation.data.personal_agent_id}
        name={agents.data?.find(agent => agent.id === botConversation.data.personal_agent_id)?.name || conversation.data?.title || t('botMemory.title')}
        onClose={() => { if (clipboardOwner()) setBotMemoryOpen(false) }} />}
      {inspectedRequest && <RequestInspector roomId={roomId} triggerEventId={inspectedRequest} ownerId={ownerId} authEpoch={authEpoch}
        onClose={closeInspector} onNavigate={(href) => {
          if (!clipboardOwner()) return
          setTranscriptResume(null); setTranscriptContextReturn(null)
          transcriptOrigin.current = null; transcriptRestore.current = false; transcriptFocusReturn.current = false
          setTranscriptQuery(''); setTranscriptActor('all'); setTranscriptSearchOpen(false)
          setRoomView('conversation'); setManagementOpen(false)
          // Preserve action/approval route parameters; reveal their existing target instead of inheriting a transcript filter.
          navigate(href)
        }} onViewSource={(seq, eventId) => { closeInspector(); navigateToEvent(seq, eventId) }} onRetryCreated={(result) => { void refreshAfterRetry(result) }}
        onFinalizeCreated={(result, canNavigate) => { void refreshAfterFinalize(result, canNavigate) }} />}
    </main>
  </div>
}
