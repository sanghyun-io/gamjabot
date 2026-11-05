import type { KeyboardEvent, ReactNode } from 'react'
import type { ToolActivity } from '../api/tools'
import PageDocumentView from './PageDocument'
import PageMarkdownExport from './PageMarkdownExport'
import PageXlsxExport from './PageXlsxExport'
import PageWorkspace from './PageWorkspace'
import PageRename from './PageRename'
import DirectorySearch, { directoryMatcher, type DirectorySearchHandle } from './DirectorySearch'
import { useFindShortcut } from '../lib/useFindShortcut'
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query'
import type { RoomEvent } from '../api/events'
import { roomResultsApi, type ResultCommand, type ResultSummary, type ResultDetail, type ResultSave, type ResultState } from '../api/roomResults'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import { resultHttpUrl, resultOpinionCandidates, resultSource, validGeneratedFile } from '../lib/resultSelection'
import { canonicalEventId, replyExcerpt } from '../lib/peerReply'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { editorSaveFailure } from './EditorSaveNotice'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import { readResultWorkspace, subscribeResultWorkspaces, updateResultWorkspace,
  resultWorkspaceSending, resultWorkspaceUncertain, type ResultChangeRequest, type ResultCommandIntent,
  type ResultListFilter, type ResultDraft, type ResultSaveRequest, type ResultSaveWorkspace, type ResultTarget } from '../lib/resultSaveWorkspace'
import { reviewTimestamp, reviewTimeOptions } from '../lib/reviewTime'
import MessageContent from './MessageContent'
import ResultContentCopy from './ResultContentCopy'
import ResultFileSave from './ResultFileSave'
import type { ResultTextFindHandle } from './ResultTextFind'
import ResultVersionDiff from './ResultVersionDiff'
import ResultComparisonTools from './ResultComparisonTools'
import ResultComparisonOriginal from './ResultComparisonOriginal'
import ResultVersionLink from './ResultVersionLink'
import ResultVersionNavigator from './ResultVersionNavigator'
import ResultComparisonJump from './ResultComparisonJump'
import { rememberResultView } from '../lib/recentResultViews'
import { resultVersionHref, type ResultRoute } from '../lib/resultLink'
import { useViewNavigationIntent } from '../lib/useViewNavigationIntent'
import { useResultReadingPosition } from '../lib/useResultReadingPosition'
import ScreenshotArtifact from './ScreenshotArtifact'
import GeneratedFileArtifact from './GeneratedFileArtifact'
import VoiceMemoArtifact from './VoiceMemoArtifact'
import { validVoiceMemo } from '../api/voiceMemos'
import ResultRevision from './ResultRevision'

interface Props {
  panelId?: string; labelledBy?: string
  roomId: number; ownerId: number; authEpoch: number; active: boolean; visible: boolean
  routeTarget?: ResultRoute; routeEntry?: string
  events: RoomEvent[]; source: RoomEvent | null; onClearSource: (expectedSourceId?: string) => void
  onNavigate: (seq: number, eventId: string) => void
  pageActions?: ToolActivity[]; renderPageApproval?: (action:ToolActivity)=>ReactNode; onPageStop?:()=>void; onPageClose?:()=>void; pageStopPending?:boolean; onPageEvent?:(event:RoomEvent)=>void
}

export default function RoomResults(props: Props) {
  // The parent keys this component by room/Owner/epoch. Every mount also has its own request identity.
  const { roomId, ownerId, authEpoch, active, visible, events, source, onClearSource, onNavigate, routeTarget, routeEntry, panelId, labelledBy } = props
  const { t, formatDateTime, locale } = useI18n()
  const auditTime = (value: string | null) => {
    const timestamp = reviewTimestamp(value)
    return timestamp ? <time dateTime={timestamp}>{formatDateTime(timestamp, reviewTimeOptions)}</time> : t('results.unrecorded')
  }
  const cache = useQueryClient()
  const [mount] = useState(() => crypto.randomUUID())
  const mounted = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const root = useRef<HTMLElement>(null), heading = useRef<HTMLHeadingElement>(null), routeHandled = useRef<string | null>(null), routePending = useRef<string | null>(null), routeNavigation = useViewNavigationIntent()
  const textFind = useRef<ResultTextFindHandle>(null)
  const listFinder = useRef<DirectorySearchHandle>(null)
  const listRoot = useRef<HTMLUListElement>(null), detailRoot = useRef<HTMLDivElement>(null), detailEnd = useRef<HTMLElement>(null)
  const listButtons = useRef(new Map<string, HTMLButtonElement>())
  const listNavigation = useViewNavigationIntent()
  const [listTarget, setListTarget] = useState<{ token: string; id: string; version: number } | null>(null)
  const [listReturnTarget, setListReturnTarget] = useState<{ token: string; id: string | null } | null>(null)
  const prefix = ['room-results', ownerId, authEpoch, roomId, mount]
  const lastChange = events.filter((event) => event.room_id === roomId && event.kind === 'activity'
    && event.payload.status === 'result_changed').at(-1)?.event_id
  useEffect(() => {
    if (lastChange && visible) void cache.invalidateQueries({ queryKey: ['room-results', ownerId, authEpoch, roomId, mount] })
  }, [lastChange, visible, cache, ownerId, authEpoch, roomId, mount])
  const identity = `${ownerId}:${authEpoch}`, ownerGeneration = directoryViewGeneration()
  const sameOwner = () => { const auth = useAuthStore.getState(); return auth.user?.id === ownerId && auth.epoch === authEpoch && directoryViewGeneration() === ownerGeneration }
  const ownerRead = async <T,>(task: () => Promise<T>): Promise<T> => {
    if (!sameOwner()) throw new Error('Result Owner changed')
    const value = await task()
    if (!sameOwner()) throw new Error('Result Owner changed')
    return value
  }
  const workspace = useSyncExternalStore(subscribeResultWorkspaces, () => readResultWorkspace(identity, roomId))
  const edit = (update: (state: ResultSaveWorkspace) => ResultSaveWorkspace) => updateResultWorkspace(identity, roomId, ownerGeneration, update)
  const { selection, compare, listFilter } = workspace
  const setSelection = (value: { id: string; version: number }) => edit(previous => ({ ...previous, selection: value, viewRevision: previous.viewRevision + 1 }))
  const setCompare = (value: boolean) => {
    if (!sameOwner() || !mounted.current || !view.current.visible) return
    edit(previous => ({ ...previous, compare: value, viewRevision: previous.viewRevision + 1 }))
  }
  const pinnedBase = selection ? workspace.comparisonBases[selection.id] : undefined
  const comparisonVersion = pinnedBase ?? (selection && selection.version > 1 ? selection.version - 1 : null)
  const comparisonReady = !!selection && comparisonVersion !== null && comparisonVersion !== selection.version

  useEffect(() => {
    if (routeTarget?.state !== 'valid') { routePending.current = null; routeNavigation.cancel(); return }
    if (!visible || !sameOwner() || !routeEntry || routeHandled.current === routeEntry) return
    routeHandled.current = routeEntry; routePending.current = routeEntry
    routeNavigation.begin(routeEntry, root.current)
    edit(previous => ({ ...previous, selection: { id: routeTarget.id, version: routeTarget.version }, compare: routeTarget.base !== null,
      comparisonBases: routeTarget.base === null ? previous.comparisonBases : Object.freeze({ ...previous.comparisonBases, [routeTarget.id]: routeTarget.base }), viewRevision: previous.viewRevision + 1 }))
  }, [visible, routeEntry, routeTarget?.state, routeTarget?.state === 'valid' ? routeTarget.id : null, routeTarget?.state === 'valid' ? routeTarget.version : null, routeTarget?.state === 'valid' ? routeTarget.base : null])
  useEffect(() => { if (!visible) routeNavigation.cancel() }, [visible])
  const pending = resultWorkspaceSending(workspace)
  const list = useInfiniteQuery({ queryKey: [...prefix, 'list'], retry: false, initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => ownerRead(async () => {
      const value = await roomResultsApi.list(roomId, pageParam)
      if (value.room_id !== roomId || value.items.some((item) => item.room_id !== roomId)) throw new Error('Result scope changed')
      return value
    }), getNextPageParam: (page) => page.next_cursor || undefined, enabled: visible, gcTime: 0 })
  const resultRows = new Map<string, ResultSummary>()
  for (const item of list.data?.pages.flatMap(page => page.items) || []) {
    const previous = resultRows.get(item.result_id)
    if (!previous || item.version > previous.version || item.version === previous.version && item.state_revision >= previous.state_revision) resultRows.set(item.result_id, item)
  }
  const results = [...resultRows.values()]
  const changeListFilter = (patch: Partial<ResultListFilter>) => {
    if (!sameOwner() || !mounted.current) return
    edit(previous => ({ ...previous, listStart: null, listFilter: Object.freeze({ ...previous.listFilter, ...patch }) }))
  }
  const matchResult = directoryMatcher(listFilter.query)
  const matchedResults = results.filter(item => (listFilter.kind === 'all' || item.kind === listFilter.kind)
    && (listFilter.state === 'all' || item.state === listFilter.state)
    && matchResult([item.title, item.result_id, item.author_name_snapshot, String(item.author_agent_id), `#${item.author_agent_id}`, item.kind, item.state,
      t(`results.kind.${item.kind}`), t(`results.state.${item.state}`)]))
  const listOrder = listFilter.order === 'title' ? 'title' : 'received'
  const titleCollator = useMemo(() => new Intl.Collator(locale, { numeric: true, sensitivity: 'base' }), [locale])
  const shownResults = listOrder === 'title' ? [...matchedResults].sort((left, right) =>
    titleCollator.compare(left.title, right.title) || (left.result_id < right.result_id ? -1 : left.result_id > right.result_id ? 1 : 0)) : matchedResults
  const listStart = workspace.listStart ? Math.max(0, shownResults.findIndex(row => row.result_id === workspace.listStart)) : 0
  const listWindow = shownResults.slice(listStart, listStart + 12)
  const moveList = (index: number) => {
    if (!sameOwner() || !mounted.current || !view.current.visible || !shownResults.length) return
    const token = crypto.randomUUID()
    routeNavigation.cancel(); listNavigation.begin(token, root.current)
    edit(previous => ({ ...previous, listStart: shownResults[Math.max(0, Math.min(index, shownResults.length - 1))].result_id }))
    setListReturnTarget({ token, id: null })
  }
  const readListWithKeys = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.defaultPrevented || event.target !== event.currentTarget || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229
      || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
    if (event.key === 'Enter' && event.repeat) {
      // Returning from detail focuses its list button. A held Enter must not
      // immediately reopen it; release and press again to activate normally.
      event.preventDefault(); event.stopPropagation(); return
    }
    if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return
    if (!sameOwner() || !mounted.current || !view.current.visible || document.visibilityState !== 'visible' || !document.hasFocus()) return
    const origin = event.currentTarget, boundary = listRoot.current
    const modal = document.querySelector('dialog[open], [aria-modal="true"]')
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? listWindow.length - 1
      : Math.max(0, Math.min(index + (event.key === 'ArrowDown' ? 1 : -1), listWindow.length - 1))
    const row = listWindow[next]
    const target = row ? listButtons.current.get(row.result_id) : undefined
    if (!boundary?.isConnected || !origin.isConnected || !target?.isConnected || !boundary.contains(origin) || !boundary.contains(target)
      || !origin.getClientRects().length || !target.getClientRects().length || target.matches(':disabled')
      || origin.closest('[hidden], [inert], [aria-hidden="true"]') || target.closest('[hidden], [inert], [aria-hidden="true"]')
      || modal && !modal.contains(boundary)) return
    event.preventDefault(); event.stopPropagation()
    if (origin === target) return
    listNavigation.cancel(); routeNavigation.cancel(); setListTarget(null); setListReturnTarget(null)
    target.focus({ preventScroll: true }); target.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }
  const hasListFilter = !!listFilter.query || listFilter.kind !== 'all' || listFilter.state !== 'all'
  const hasListOptions = hasListFilter || listOrder !== 'received'
  const detail = useQuery({ retry: false, queryKey: [...prefix, 'detail', selection?.id, selection?.version],
    queryFn: () => ownerRead(async () => {
      const value = await roomResultsApi.detail(roomId, selection!.id, selection!.version)
      if (value.room_id !== roomId || value.result_id !== selection!.id || value.version !== selection!.version) throw new Error('Result scope changed')
      return value
    }), enabled: visible && !!selection, gcTime: 0 })
  const versions = useInfiniteQuery({ retry: false, queryKey: [...prefix, 'versions', selection?.id], initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) => ownerRead(async () => {
      const value = await roomResultsApi.versions(roomId, selection!.id, pageParam)
      if (value.room_id !== roomId || value.result_id !== selection!.id) throw new Error('Result scope changed')
      return value
    }), getNextPageParam: (page) => page.next_before_version || undefined, enabled: visible && !!selection, gcTime: 0 })
  const opinions = useInfiniteQuery({ retry: false, queryKey: [...prefix, 'reviews', selection?.id, selection?.version], initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => ownerRead(() => roomResultsApi.reviews(roomId, selection!.id, selection!.version, pageParam)),
    getNextPageParam: (page) => page.next_cursor || undefined, enabled: visible && !!selection, gcTime: 0 })
  const previous = useQuery({ retry: false, queryKey: [...prefix, 'comparison', selection?.id, comparisonVersion],
    queryFn: () => ownerRead(async () => {
      const value = await roomResultsApi.detail(roomId, selection!.id, comparisonVersion!)
      if (value.room_id !== roomId || value.result_id !== selection!.id || value.version !== comparisonVersion) throw new Error('Result scope changed')
      return value
    }), enabled: visible && compare && comparisonReady, gcTime: 0 })

  const comparisonOptions = new Map<number, string>()
  for (const item of versions.data?.pages.flatMap(page => page.items) || []) {
    if (item.result_id === selection?.id && Number.isSafeInteger(item.version) && item.version > 0) comparisonOptions.set(item.version, item.title)
  }
  if (pinnedBase !== undefined && !comparisonOptions.has(pinnedBase)) comparisonOptions.set(pinnedBase, '')
  const chooseComparison = (value: number | null, direct = false) => {
    if (!sameOwner() || !mounted.current || !view.current.visible || !selection) return
    if (value !== null && (!Number.isSafeInteger(value) || value < 1 || value === selection.version || !comparisonOptions.has(value) && (!direct || knownLatest === null || value > knownLatest))) return
    edit(previous => {
      if (previous.selection?.id !== selection.id || previous.selection.version !== selection.version) return previous
      const comparisonBases = { ...previous.comparisonBases }
      if (value === null) delete comparisonBases[selection.id]
      else comparisonBases[selection.id] = value
      return { ...previous, comparisonBases: Object.freeze(comparisonBases), viewRevision: previous.viewRevision + 1 }
    })
  }

  const commandReceiptMatches = (request: ResultChangeRequest, value: ResultCommand) => {
    if (!value || value.room_id !== roomId || value.result_id !== request.target.id || value.result_version !== request.target.version
      || !canonicalEventId(value.result_id) || !canonicalEventId(value.activity_event_id)
      || !Number.isSafeInteger(value.activity_seq) || value.activity_seq < 1 || typeof value.replayed !== 'boolean'
      || !Number.isSafeInteger(value.state_revision) || value.state_revision < 1
      || !(['draft', 'reviewing', 'final'] as string[]).includes(value.state) || value.operation !== request.intent.operation) return false
    const intent = request.intent
    if (intent.operation === 'review') return value.review_event_id === intent.review_event_id && !!canonicalEventId(value.result_review_id)
    if (value.state_revision !== intent.expected_state_revision + 1) return false
    return intent.operation === 'state' ? value.state === intent.state : value.link_report === intent.report && value.state === request.state
  }
  const launchCommand = async (request: ResultChangeRequest, snapshot: ResultSaveWorkspace, acknowledgeEarlier = false) => {
    if (!sameOwner() || !mounted.current || !view.current.active || !view.current.visible || request.mismatch) return
    if (!edit(previous => previous !== snapshot || resultWorkspaceSending(previous) ? previous : {
      ...previous,
      requests: previous.requests.map(row => acknowledgeEarlier && (row.status === 'unknown' || row.mismatch) ? Object.freeze({ ...row, acknowledged: true }) : row),
      commands: previous.commands.some(row => row.key === request.key)
        ? previous.commands.map(row => row.key === request.key ? Object.freeze({ ...row, status: 'sending' as const }) : row)
        : [...previous.commands.map(row => acknowledgeEarlier && (row.status === 'unknown' || row.mismatch) ? Object.freeze({ ...row, acknowledged: true }) : row), Object.freeze({ ...request, status: 'sending' as const })],
    })) return
    try {
      const intent = request.intent, { id, version } = request.target
      const receipt = intent.operation === 'state'
        ? await roomResultsApi.state(roomId, id, version, { state: intent.state, expected_state_revision: intent.expected_state_revision }, request.key)
        : intent.operation === 'link-report'
          ? await roomResultsApi.linkReport(roomId, id, version, { report: intent.report, expected_state_revision: intent.expected_state_revision }, request.key)
          : await roomResultsApi.review(roomId, id, version, intent.review_event_id, request.key)
      if (!sameOwner()) return
      const matches = commandReceiptMatches(request, receipt)
      edit(previous => ({ ...previous, commands: previous.commands.map(row => row.key !== request.key || row.status === 'accepted' ? row
        : Object.freeze(matches ? { ...row, status: 'accepted' as const, receipt } : { ...row, status: 'unknown' as const, mismatch: true })) }))
      // A result remains on its current page; the retained receipt is an explicit navigation target.
      if (matches) void cache.invalidateQueries({ queryKey: ['room-results', ownerId, authEpoch, roomId] })
    } catch (failure) {
      if (!sameOwner()) return
      const status = (failure as { response?: { status?: number } }).response?.status
      edit(previous => ({ ...previous, commands: previous.commands.map(row => row.key !== request.key || row.status === 'accepted' ? row : Object.freeze({ ...row,
        status: status === 409 ? 'conflict' as const : editorSaveFailure(failure) === 'unknown' ? 'unknown' as const : 'rejected' as const })) }))
    }
  }
  const startCommand = (target: ResultDetail, intent: ResultCommandIntent) => {
    if (!sameOwner() || !mounted.current || !view.current.active || !view.current.visible || detail.isFetching || detail.isError) return
    const snapshot = readResultWorkspace(identity, roomId)
    if (snapshot.selection?.id !== target.result_id || snapshot.selection.version !== target.version || resultWorkspaceSending(snapshot)) return
    const fingerprint = JSON.stringify([target.result_id, target.version, intent])
    const prior = [...snapshot.commands].reverse().find(row => row.fingerprint === fingerprint)
    if (prior?.mismatch || prior?.status === 'accepted' || prior?.status === 'conflict') return
    const acknowledgeEarlier = !prior && resultWorkspaceUncertain(snapshot)
    if (acknowledgeEarlier && !window.confirm(t('results.newCommandConfirm'))) return
    const request: ResultChangeRequest = prior || Object.freeze({ key: crypto.randomUUID(), fingerprint,
      target: Object.freeze({ id: target.result_id, version: target.version, title: target.title }), state: target.state,
      intent: Object.freeze({ ...intent }), viewRevision: snapshot.viewRevision, status: 'unknown' as const })
    void launchCommand(request, snapshot, acknowledgeEarlier)
  }
  const choose = (id: string, version: number, fromList = false) => {
    if (!sameOwner() || !mounted.current || !view.current.visible) return
    listNavigation.cancel(); setListTarget(null); setListReturnTarget(null)
    if (fromList) {
      routeNavigation.cancel()
      const token = crypto.randomUUID()
      listNavigation.begin(token, root.current); setListTarget({ token, id, version })
    }
    setSelection({ id, version }); setCompare(false)
  }
  const view = useRef({ events, source, active, visible }); view.current = { events, source, active, visible }
  const sourceFor = (id: string, seq: number) => {
    const value = view.current.source?.event_id === id && view.current.source.seq === seq ? view.current.source
      : view.current.events.find(event => event.event_id === id && event.seq === seq)
    return value && value.room_id === roomId ? value : undefined
  }
  useEffect(() => {
    if (!source || !sameOwner()) return
    const kind = resultSource(source, roomId)
    if (!kind) return
    edit(previous => {
      const known = previous.drafts[source.event_id]
      if (known && previous.activeSourceId === source.event_id) return previous
      const draft: ResultDraft = known || { editId: crypto.randomUUID(), sourceId: source.event_id, sourceSeq: source.seq,
        sourceKind: kind, kind, title: kind==='file' && typeof source.payload.action_id==='string' && validGeneratedFile(source.payload.generated_file,roomId,source.payload.action_id) ? source.payload.generated_file.filename : kind==='file' && validVoiceMemo(source.payload.voice_memo,ownerId,roomId) ? (locale==='ko'?'음성 메모':'Voice memo') : '', url: '', evidence: '', target: null, revision: 0, dirty: false }
      return { ...previous, activeSourceId: source.event_id, viewRevision: previous.viewRevision + 1,
        drafts: { ...previous.drafts, [source.event_id]: Object.freeze(draft) } }
    })
  }, [source?.event_id, source?.seq])
  const updateDraft = (sent: ResultDraft, patch: Partial<Pick<ResultDraft, 'kind' | 'title' | 'url' | 'evidence' | 'target'>>) => {
    if (!sameOwner() || !mounted.current || !view.current.active) return
    edit(previous => {
      const live = previous.drafts[sent.sourceId]
      if (!live || live.editId !== sent.editId || resultWorkspaceSending(previous)) return previous
      return { ...previous, drafts: { ...previous.drafts, [sent.sourceId]: Object.freeze({ ...live, ...patch, revision: live.revision + 1, dirty: true }) } }
    })
  }
  const closeDraft = (draft: ResultDraft, discard = false) => {
    if (!sameOwner() || !mounted.current) return
    if (discard && !window.confirm(t('results.draftDiscardConfirm'))) return
    if (!sameOwner()) return
    edit(previous => {
      const live = previous.drafts[draft.sourceId]
      if (!live || live.editId !== draft.editId || discard && live.revision !== draft.revision) return previous
      const drafts = { ...previous.drafts }
      if (discard || !live.dirty) delete drafts[draft.sourceId]
      return { ...previous, drafts, activeSourceId: previous.activeSourceId === draft.sourceId ? null : previous.activeSourceId,
        viewRevision: previous.viewRevision + 1 }
    })
    onClearSource(draft.sourceId)
  }
  const validReceipt = (request: ResultSaveRequest, value: ResultCommand) => {
    if (!value || value.room_id !== roomId || !canonicalEventId(value.result_id) || !canonicalEventId(value.activity_event_id)
      || !Number.isSafeInteger(value.activity_seq) || value.activity_seq < 1 || typeof value.replayed !== 'boolean'
      || value.operation !== (request.target ? 'version' : 'save') || value.state !== 'draft' || value.state_revision !== 1) return false
    return request.target ? value.result_id === request.target.id && value.result_version === request.target.version + 1 : value.result_version === 1
  }
  const launchSave = async (request: ResultSaveRequest, snapshot: ResultSaveWorkspace, acknowledgeEarlier = false) => {
    if (!sameOwner() || !mounted.current || !view.current.active || !view.current.visible || request.mismatch) return
    const boundSource = sourceFor(request.body.source_event_id, request.sourceSeq)
    if (!boundSource || resultSource(boundSource, roomId) !== (request.body.kind === 'file' ? 'file' : 'text')) return
    if (!edit(previous => previous !== snapshot || resultWorkspaceSending(previous) ? previous : {
      ...previous,
      commands: previous.commands.map(row => acknowledgeEarlier && (row.status === 'unknown' || row.mismatch) ? Object.freeze({ ...row, acknowledged: true }) : row),
      requests: previous.requests.some(row => row.key === request.key)
        ? previous.requests.map(row => row.key === request.key ? Object.freeze({ ...row, status: 'sending' as const }) : row)
        : [...previous.requests.filter(row => row.status === 'unknown' || row.mismatch).map(row => acknowledgeEarlier ? Object.freeze({ ...row, acknowledged: true }) : row), Object.freeze({ ...request, status: 'sending' as const })],
    })) return
    try {
      const receipt = request.target
        ? await roomResultsApi.version(roomId, request.target.id, { ...request.body, expected_current_version: request.target.version }, request.key)
        : await roomResultsApi.save(roomId, request.body, request.key)
      if (!sameOwner()) return
      if (!validReceipt(request, receipt)) {
        edit(previous => ({ ...previous, requests: previous.requests.map(row => row.key === request.key ? Object.freeze({ ...row, status: 'unknown' as const, mismatch: true }) : row) })); return
      }
      let consumed = false
      edit(previous => {
        const draft = previous.drafts[request.body.source_event_id]
        consumed = !!draft && draft.editId === request.draftId && draft.revision === request.draftRevision
        const drafts = { ...previous.drafts }
        if (consumed) delete drafts[request.body.source_event_id]
        const unchangedView = previous.viewRevision === request.viewRevision
        return { ...previous, drafts, activeSourceId: consumed && previous.activeSourceId === request.body.source_event_id ? null : previous.activeSourceId,
          selection: unchangedView ? { id: receipt.result_id, version: receipt.result_version } : previous.selection,
          compare: unchangedView ? false : previous.compare, viewRevision: previous.viewRevision + (unchangedView ? 1 : 0),
          requests: previous.requests.map(row => row.key === request.key ? Object.freeze({ ...row, status: 'accepted' as const, receipt }) : row) }
      })
      void cache.invalidateQueries({ queryKey: ['room-results', ownerId, authEpoch, roomId] })
      if (consumed) onClearSource(request.body.source_event_id)
    } catch (failure) {
      if (!sameOwner()) return
      const status = (failure as { response?: { status?: number } }).response?.status
      edit(previous => ({ ...previous, requests: previous.requests.map(row => row.key === request.key ? Object.freeze({ ...row,
        status: status === 409 ? 'conflict' as const : editorSaveFailure(failure) === 'unknown' ? 'unknown' as const : 'rejected' as const }) : row) }))
    }
  }
  const saveDraft = (body: ResultSave, draft: ResultDraft) => {
    if (!sameOwner()) return
    const snapshot = readResultWorkspace(identity, roomId)
    if (snapshot.drafts[draft.sourceId] !== draft) return
    const fingerprint = JSON.stringify([draft.target?.id || '', draft.target?.version || 0, body])
    const prior = [...snapshot.requests].reverse().find(request => request.fingerprint === fingerprint && request.status !== 'accepted' && !request.mismatch)
    const acknowledgeEarlier = !prior && resultWorkspaceUncertain(snapshot)
    if (acknowledgeEarlier && !window.confirm(t('results.newSaveConfirm'))) return
    const evidence = body.evidence.map(ref => Object.freeze({ ...ref })); Object.freeze(evidence)
    const request: ResultSaveRequest = prior || Object.freeze({ key: crypto.randomUUID(), fingerprint,
      body: Object.freeze({ ...body, evidence }),
      target: draft.target ? Object.freeze({ ...draft.target }) : null, sourceSeq: draft.sourceSeq,
      draftId: draft.editId, draftRevision: draft.revision, viewRevision: snapshot.viewRevision, status: 'unknown' as const })
    void launchSave(request, snapshot, acknowledgeEarlier)
  }
  const readTarget = (id: string): Promise<ResultTarget> => ownerRead(async () => {
    const value = await roomResultsApi.versions(roomId, id)
    const latest = value.items.find(item => item.version === value.current_version)
    if (value.room_id !== roomId || value.result_id !== id || !latest || latest.room_id !== roomId || latest.result_id !== id
      || !Number.isSafeInteger(latest.version) || latest.version < 1) throw new Error('Result target unavailable')
    return { id, version: latest.version, title: latest.title }
  })
  const requestSourceAvailable = (request: ResultSaveRequest) => { const bound = sourceFor(request.body.source_event_id, request.sourceSeq); return !!bound && resultSource(bound, roomId) === (request.body.kind === 'file' ? 'file' : 'text') }
  const activeDraft = workspace.activeSourceId ? workspace.drafts[workspace.activeSourceId] : undefined
  const content = (value: ResultDetail, canRead: () => boolean = () => true) => value.kind === 'file'
    ? value.file_ref?.adapter === 'voice_memo' && value.file_ref.room_id === roomId && value.file_ref.agent_id === value.author_agent_id && value.file_ref.event_seq === value.source.seq
      ? <VoiceMemoArtifact key={`${value.result_id}:${value.version}`} memo={value.file_ref} source={{kind:'result',resultId:value.result_id,version:value.version}} roomId={roomId} availability={value.file_status} ownerId={ownerId} epoch={authEpoch} canUse={()=>{if(!sameOwner()||!mounted.current||!view.current.visible||!canRead())return false;const live=readResultWorkspace(identity,roomId),chosen=live.selection;return !!chosen&&chosen.id===value.result_id&&chosen.version===selection?.version&&(chosen.version===value.version||live.compare&&(live.comparisonBases[value.result_id]??chosen.version-1)===value.version)}} />
      : value.file_ref?.adapter === 'generated_file'
      ? <GeneratedFileArtifact key={`${value.result_id}:${value.version}`} roomId={roomId} resultId={value.result_id} version={value.version} file={value.file_ref} fileStatus={value.file_status} readOnly={!active} canUse={()=>{if(!sameOwner()||!mounted.current||!view.current.visible||!canRead())return false;const live=readResultWorkspace(identity,roomId),chosen=live.selection;return !!chosen&&chosen.id===value.result_id&&chosen.version===selection?.version&&(chosen.version===value.version||live.compare&&(live.comparisonBases[value.result_id]??chosen.version-1)===value.version)}} onRemoved={()=>{void cache.invalidateQueries({queryKey:prefix})}} />
      : value.file_status === 'unchecked' && value.file_ref?.adapter === 'screenshot'
      ? <ScreenshotArtifact key={`${value.result_id}:${value.version}`} roomId={roomId} actionId={value.file_ref.action_id}
          eventSeq={value.file_ref.event_seq} width={value.file_ref.image_width || 0} height={value.file_ref.image_height || 0}
          createdAt={value.created_at} readOnly />
      : <p role="status">{t(value.file_status === 'removed' ? 'screenshot.removed' : 'screenshot.unavailable')}</p>
    : value.kind === 'page' ? <PageDocumentView key={`${value.result_id}:${value.version}`} content={value.content}/>
    : value.kind === 'link' ? resultHttpUrl(value.content)
      ? <a className="text-link" href={value.content} target="_blank" rel="noopener noreferrer">{value.content}</a>
      : <p>{t('results.unavailable')}</p>
    : <MessageContent content={value.content} copyScope={JSON.stringify([identity,roomId,value.result_id,value.version,selection?.version,value.version===selection?.version?null:[compare,comparisonVersion],visible])} copyAllowed={()=>{
        if(!canRead()||!sameOwner()||!mounted.current||!view.current.visible)return false
        const live=readResultWorkspace(identity,roomId),chosen=live.selection
        if(!chosen||chosen.id!==value.result_id||chosen.version!==selection?.version)return false
        return chosen.version===value.version||live.compare&&(live.comparisonBases[value.result_id]??chosen.version-1)===value.version
      }}/>
  const current = detail.data
  const comparisonSourceCurrent = (value: ResultDetail) => {
    if (!sameOwner() || !mounted.current || !view.current.visible) return false
    const live = readResultWorkspace(identity, roomId), chosen = live.selection
    return !!chosen && chosen.id === value.result_id && chosen.version === current?.version && live.compare
      && (live.comparisonBases[chosen.id] ?? chosen.version - 1) === value.version
  }
  const versionRows = versions.data?.pages.flatMap(page => page.items) || []
  const receivedVersions = versions.data?.pages.map(page => page.current_version).filter(value => Number.isSafeInteger(value) && value > 0) || []
  const detailLatest = current && Number.isSafeInteger(current.current_version) && current.current_version > 0 ? current.current_version : null
  const listLatest = receivedVersions.length ? Math.max(...receivedVersions) : null
  const knownLatest = listLatest === null ? detailLatest : detailLatest === null ? listLatest : Math.max(listLatest, detailLatest)
  const versionReceivedAt = Math.max(listLatest === knownLatest ? versions.dataUpdatedAt : 0, detailLatest === knownLatest ? detail.dataUpdatedAt : 0)
  const pageFollow=useRef<{id:string;version:number;follow:boolean}|null>(null)
  if(current?.kind==='page'&&selection?.id===current.result_id&&selection.version===current.version&&(!pageFollow.current||pageFollow.current.id!==current.result_id||pageFollow.current.version!==current.version))pageFollow.current={id:current.result_id,version:current.version,follow:current.version===current.current_version}
  const pageUpdate=useMemo(()=>{if(!current||current.kind!=='page'||!pageFollow.current?.follow)return null;return events.findLast(event=>{const data=event.payload.page_result as Record<string,unknown>|undefined,root=events.find(e=>e.event_id===event.payload.trigger_event_id);const target=root?.payload.page_target as Record<string,unknown>|undefined;return event.room_id===roomId&&(event.actor_type==='user'&&event.actor_id===ownerId&&event.kind==='activity'&&event.payload.category==='page_owner_rename'&&event.payload.version===1&&event.payload.result_id===current.result_id&&event.payload.base_version===current.version&&event.payload.page_version===current.version+1&&typeof event.payload.document_sha256==='string'&&/^[a-f0-9]{64}$/.test(event.payload.document_sha256)||event.actor_type==='tool'&&event.kind==='tool_result'&&event.payload.family==='write_page'&&event.payload.status==='completed'&&data?.version===1&&data.room_id===roomId&&data.result_id===current.result_id&&data.base_version===current.version&&data.page_version===current.version+1&&data.agent_id===current.author_agent_id&&typeof data.document_sha256==='string'&&/^[a-f0-9]{64}$/.test(data.document_sha256)&&root?.actor_type==='user'&&root.actor_id===ownerId&&root.kind==='message'&&target?.result_id===current.result_id&&target.version===current.version)})||null},[current,events,roomId,ownerId])
  useEffect(()=>{if(!pageUpdate||!current||!visible||compare)return;let live=true;const previous=current.version;void ownerRead(()=>roomResultsApi.pageCurrent(roomId,current.result_id)).then(next=>{const chosen=readResultWorkspace(identity,roomId).selection;if(live&&sameOwner()&&mounted.current&&view.current.visible&&chosen?.id===current.result_id&&chosen.version===previous&&next.room_id===roomId&&next.result_id===current.result_id&&next.kind==='page'&&next.version===previous+1&&next.current_version===next.version&&next.author_agent_id===current.author_agent_id&&next.source.event_id===pageUpdate.event_id&&next.source.seq===pageUpdate.seq){void cache.invalidateQueries({queryKey:prefix});choose(next.result_id,next.version)}}).catch(()=>{});return()=>{live=false}},[pageUpdate?.event_id,current?.result_id,current?.version,visible,compare])

  const lastRecent = useRef('')
  const receivedBase = compare && comparisonReady && previous.data?.result_id === current?.result_id && previous.data?.version === comparisonVersion ? comparisonVersion : null
  useEffect(() => {
    if (!visible || !current || current.result_id !== selection?.id || current.version !== selection?.version) { lastRecent.current = ''; return }
    if (!sameOwner() || detail.isPending || detail.isFetching || detail.isError) return
    const key = `${routeEntry || mount}:${current.result_id}:${current.version}:${receivedBase ?? ''}`
    if (lastRecent.current === key) return
    lastRecent.current = key
    rememberResultView(identity, ownerGeneration, { room: roomId, id: current.result_id, version: current.version, base: receivedBase, title: current.title })
  }, [visible, current, selection?.id, selection?.version, receivedBase, routeEntry, detail.isPending, detail.isFetching, detail.isError])
  const versionHref = current ? resultVersionHref(roomId, current.result_id, current.version, compare && comparisonReady ? comparisonVersion : null) : null
  useEffect(() => {
    if (!routePending.current || routeTarget?.state !== 'valid' || !visible) return
    const live = readResultWorkspace(identity, roomId).selection
    if (live?.id !== routeTarget.id || live.version !== routeTarget.version) { routePending.current = null; routeNavigation.cancel(); return }
    if (selection?.id !== routeTarget.id || selection.version !== routeTarget.version) return
    if (detail.isPending || detail.isFetching) return
    if (current?.result_id === routeTarget.id && current.version === routeTarget.version && sameOwner() && routeNavigation.current(routePending.current)) {
      heading.current?.focus({ preventScroll: true }); heading.current?.scrollIntoView({ block: 'start' })
    }
    routePending.current = null; routeNavigation.cancel()
  }, [current, detail.isPending, detail.isFetching, selection?.id, selection?.version, visible, routeEntry])
  const candidates = current ? resultOpinionCandidates(current, events) : []
  const linked = opinions.data?.pages.flatMap((page) => page.items) || []
  useEffect(() => {
    if (!listTarget) return
    if (!visible || !sameOwner() || selection?.id !== listTarget.id || selection.version !== listTarget.version) { listNavigation.cancel(); setListTarget(null); return }
    const node = detailRoot.current
    if (node?.isConnected && node.getClientRects().length && listNavigation.current(listTarget.token)) {
      node.focus({ preventScroll: true }); node.scrollIntoView({ block: 'start', inline: 'nearest' })
    }
    setListTarget(null)
  }, [listTarget, selection?.id, selection?.version, visible])
  useEffect(() => {
    if (!listReturnTarget) return
    const node = listReturnTarget.id ? listButtons.current.get(listReturnTarget.id) || listRoot.current : listRoot.current
    if (visible && sameOwner() && node?.isConnected && node.getClientRects().length && listNavigation.current(listReturnTarget.token)) {
      node.focus({ preventScroll: true }); node.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    }
    setListReturnTarget(null)
  }, [listReturnTarget, workspace.listStart, visible])
  const returnToList = () => {
    if (!sameOwner() || !mounted.current || !view.current.visible) return
    routeNavigation.cancel(); listNavigation.cancel(); setListTarget(null)
    const index = selection ? shownResults.findIndex(row => row.result_id === selection.id) : -1
    const token = crypto.randomUUID()
    listNavigation.begin(token, root.current)
    if (index >= 0) edit(previous => ({ ...previous, listStart: shownResults[Math.floor(index / 12) * 12].result_id }))
    setListReturnTarget({ token, id: index >= 0 ? selection!.id : null })
  }
  const readingPosition = useResultReadingPosition({ root, identity, generation: ownerGeneration, roomId,
    selection: selection ? `${selection.id}:${selection.version}` : 'list', visible,
    ready: !list.isPending && (!selection || !detail.isPending && !versions.isPending && (!compare || !comparisonReady || !previous.isPending)),
    explicitRoute: routeTarget?.state === 'valid', canUse: () => sameOwner() && mounted.current && view.current.visible })
  const closeResultDetail = (origin: HTMLButtonElement) => {
    if (!sameOwner() || !mounted.current || !view.current.visible || !selection
      || document.visibilityState !== 'visible' || !document.hasFocus()) return
    const chosen = readResultWorkspace(identity, roomId).selection, boundary = detailRoot.current
    const modal = document.querySelector('dialog[open], [aria-modal="true"]')
    if (!chosen || chosen.id !== selection.id || chosen.version !== selection.version || !boundary?.isConnected
      || !origin.isConnected || !boundary.contains(origin) || !origin.getClientRects().length
      || origin.closest('[hidden], [inert], [aria-hidden="true"]') || modal && !modal.contains(boundary)) return
    readingPosition.interrupt(); routePending.current = null
    // Preserve the normal ID-based list return, then clear only the reading
    // selection. Existing save receipts respect this newer viewRevision.
    returnToList()
    edit(previous => previous.selection?.id === selection.id && previous.selection.version === selection.version
      ? { ...previous, selection: null, compare: false, viewRevision: previous.viewRevision + 1 } : previous)
  }
  const moveWithinDetail = (destination: 'top' | 'end', origin: HTMLButtonElement) => {
    if (!sameOwner() || !mounted.current || !view.current.visible || !selection
      || document.visibilityState !== 'visible' || !document.hasFocus()) return
    const chosen = readResultWorkspace(identity, roomId).selection
    if (!chosen || chosen.id !== selection.id || chosen.version !== selection.version) return
    const boundary = detailRoot.current, target = destination === 'top' ? heading.current : detailEnd.current
    const modal = document.querySelector('dialog[open], [aria-modal="true"]')
    if (!boundary?.isConnected || !target?.isConnected || !origin.isConnected
      || !boundary.contains(origin) || !boundary.contains(target) || !origin.getClientRects().length || !target.getClientRects().length
      || target.closest('[hidden], [inert], [aria-hidden="true"]') || origin.closest('[hidden], [inert], [aria-hidden="true"]')
      || modal && !modal.contains(boundary)) return
    readingPosition.interrupt(); routeNavigation.cancel(); listNavigation.cancel()
    setListTarget(null); setListReturnTarget(null)
    // Focus a non-activating landmark so a held Enter cannot invoke an action.
    target.focus({ preventScroll: true }); target.scrollIntoView({ block: 'start', inline: 'nearest' })
  }
  const sourceButton = (seq: number, id: string) => <button type="button" className="text-link" onClick={() => onNavigate(seq, id)}>{t('results.source', { seq })}</button>
  useFindShortcut(visible, event => {
    if (event.target instanceof HTMLElement && event.target.closest('.result-previous')) return false
    return textFind.current?.focusQuery(event.target) ?? false
  })
  return <section ref={root} id={panelId} role={panelId ? 'tabpanel' : undefined} aria-labelledby={labelledBy} onScroll={readingPosition.onScroll} onPointerDownCapture={() => { readingPosition.interrupt(); listNavigation.cancel(); routeNavigation.cancel() }} onKeyDownCapture={() => { readingPosition.interrupt(); listNavigation.cancel(); routeNavigation.cancel() }} onWheelCapture={() => { readingPosition.interrupt(); listNavigation.cancel(); routeNavigation.cancel() }} className={`room-results${current?.kind==='page'?' room-results--page':''}`} hidden={!visible} aria-label={t('results.tab')}>
    <div hidden={current?.kind==='page'} style={{display:current?.kind==='page'?'none':'contents'}}><div className="page-actions"><h2 className="section-title">{t('results.tab')}</h2>
      <button type="button" className="button button--secondary button--compact" disabled={list.isFetching || pending}
        onClick={() => { void cache.invalidateQueries({ queryKey: prefix }, { cancelRefetch: false }) }}>{t('results.refresh')}</button></div>
    {routeTarget?.state === 'invalid' && <p role="alert" className="notice notice--warning">{t('results.versionLink.invalid')}</p>}
    {routeTarget?.state === 'valid' && selection?.id === routeTarget.id && selection.version === routeTarget.version && <p role="status" className="metadata">{t('results.versionLink.target', { version: routeTarget.version })}</p>}
    {Object.values(workspace.drafts).some(draft => draft.dirty) && <details className="notice notice--info"><summary>{t('results.sessionDrafts')}</summary><p>{t('results.sessionLimit')}</p><ul>{Object.values(workspace.drafts).filter(draft => draft.dirty).map(draft => <li key={draft.sourceId}><button type="button" className="text-link" onClick={() => edit(previous => ({ ...previous, activeSourceId: draft.sourceId, viewRevision: previous.viewRevision + 1 }))}>{draft.title || t('results.untitledDraft')} · #{draft.sourceSeq}</button></li>)}</ul></details>}
    {activeDraft && <ResultSaveForm key={activeDraft.editId} draft={activeDraft} results={results} disabled={!active || pending}
      sourceAvailable={!!sourceFor(activeDraft.sourceId, activeDraft.sourceSeq) && resultSource(sourceFor(activeDraft.sourceId, activeDraft.sourceSeq)!, roomId) === activeDraft.sourceKind}
      visible={visible} autoFocus={source?.event_id === activeDraft.sourceId && visible}
      targetQueryKey={[...prefix, 'save-target', activeDraft.target?.id]} readTarget={readTarget}
      onChange={patch => updateDraft(activeDraft, patch)} onClose={() => closeDraft(activeDraft)} onDiscard={() => closeDraft(activeDraft, true)} onSave={saveDraft} />}
    {workspace.requests.map(request => <section className="notice notice--warning stack stack--tight" key={request.key}>
      <p role={request.status === 'unknown' || request.mismatch ? 'alert' : 'status'}>{t(request.status === 'sending' ? 'results.saving' : request.status === 'accepted' ? 'results.saved' : request.mismatch ? 'results.receiptMismatch' : request.status === 'unknown' ? 'results.failure' : request.status === 'conflict' ? 'results.conflict' : 'editor.rejected')}</p>
      <strong>{request.body.title}</strong><p className="metadata">{t(request.target ? 'results.versionRequest' : 'results.new')} {request.target && ` · ${request.target.title} · v${request.target.version}`}</p>
      {request.body.kind === 'link' && <p>{request.body.url}</p>}
      <details><summary>{t('results.originalSave')}</summary>{sourceButton(request.sourceSeq, request.body.source_event_id)}<p>{t(`results.kind.${request.body.kind}`)}</p>{request.body.evidence.length > 0 && <ul>{request.body.evidence.map((ref, index) => <li key={index}>{'url' in ref ? ref.url : ref.event_id}</li>)}</ul>}<p className="metadata">{t('results.evidenceLimit')}</p></details>
      {request.status !== 'accepted' && !requestSourceAvailable(request) && <p role="status">{t('results.sourceMissing')}</p>}
      {request.receipt && <button type="button" className="button button--secondary" onClick={() => choose(request.receipt!.result_id, request.receipt!.result_version)}>{t('results.openSaved')}</button>}
      {request.status !== 'accepted' && request.status !== 'sending' && !request.mismatch && <button type="button" className="button button--secondary" disabled={!active || pending || !requestSourceAvailable(request)} onClick={() => { const snapshot = readResultWorkspace(identity, roomId); const live = snapshot.requests.find(row => row.key === request.key); if (live && live.status !== 'accepted') void launchSave(live, snapshot) }}>{t('results.retryOriginalSave')}</button>}
    </section>)}
    {workspace.commands.length > 0 && <section className="stack stack--tight" aria-label={t('results.commandHistory')}>
      <h3 className="section-title">{t('results.commandHistory')}</h3><p className="metadata">{t('results.commandLimit')}</p>
      {[...workspace.commands].reverse().map(request => <details key={request.key} className="notice notice--info" open={request.status !== 'accepted'}>
        <summary><span>{t(request.intent.operation === 'state' ? 'results.changeState' : request.intent.operation === 'review' ? 'results.linkOpinion' : 'results.report')} · {request.target.title} · v{request.target.version}</span>
          {' · '}{t(request.status === 'accepted' ? 'results.commandAccepted' : request.status === 'sending' ? 'results.commandSending' : request.mismatch ? 'results.receiptMismatch' : request.status === 'conflict' ? 'results.conflict' : request.status === 'rejected' ? 'editor.rejected' : 'results.failure')}</summary>
        <div className="stack stack--tight">
          <p role={request.status === 'unknown' || request.mismatch ? 'alert' : 'status'}>{t(request.status === 'unknown' ? 'editor.unknown' : request.status === 'conflict' ? 'results.commandConflict' : 'results.commandLimit')}</p>
          {request.intent.operation === 'state' && <p>{t('results.changeState')}: {t(`results.state.${request.intent.state}`)} · {t('results.originalRevision', { revision: request.intent.expected_state_revision })}</p>}
          {request.intent.operation === 'link-report' && <p>{t(request.intent.report === 'reported_bad' ? 'results.reportedBad' : 'results.unchecked')} · {t('results.originalRevision', { revision: request.intent.expected_state_revision })}</p>}
          {request.intent.operation === 'review' && sourceButton(request.intent.reviewSeq, request.intent.review_event_id)}
          <div className="page-actions"><button type="button" className="button button--secondary button--compact" onClick={() => {
            choose(request.target.id, request.target.version); void cache.invalidateQueries({ queryKey: prefix })
          }}>{t('results.openCommandTarget')}</button>
          {request.status === 'accepted' && request.receipt && sourceButton(request.receipt.activity_seq, request.receipt.activity_event_id)}
          {(request.status === 'unknown' || request.status === 'rejected') && !request.mismatch && <button type="button" className="button button--secondary button--compact" disabled={!active || pending} onClick={() => {
            const snapshot = readResultWorkspace(identity, roomId), live = snapshot.commands.find(row => row.key === request.key)
            if (live && (live.status === 'unknown' || live.status === 'rejected')) void launchCommand(live, snapshot)
          }}>{t('results.retryOriginalCommand')}</button>}</div>
        </div>
      </details>)}
    </section>}
    {pending && <p role="status">{t('results.saving')}</p>}
    {!active && <p className="metadata">{t('chat.closedReadOnly')}</p>}
    {list.isPending && <p role="status">{t('common.loading')}</p>}
    <ResultReadNotice failed={list.isError} fetching={list.isFetching} known={!!list.data} retryLabel="results.reads.list" onRetry={() => { void list.refetch({ cancelRefetch: false }) }} />
    <section className="stack stack--tight" aria-label={t('results.filters.title')}>
      <details className="compact-details"><summary>{t('results.about')}</summary><p>{t('results.filters.help')}</p><p>{t('results.identity.help')}</p></details>
      <DirectorySearch compact ref={listFinder} value={listFilter.query} onChange={query => changeListFilter({ query })} hint={t('results.filters.placeholder')}
        shown={shownResults.length} total={results.length} known={!!list.data} loading={list.isFetching} controls={`result-list-${mount}`} preventSubmit
        onSearchSubmit={listFilter.query.trim() && shownResults.length ? () => choose(shownResults[0].result_id, shownResults[0].version, true) : undefined} />
      {listFilter.query.trim() && <div className="stack stack--tight">
        <p className="sr-only">{t('results.filters.openMatchHelp')}</p>
        <div><button type="button" className="button button--secondary button--compact" disabled={!shownResults.length} onClick={() => {
          if (shownResults.length) choose(shownResults[0].result_id, shownResults[0].version, true)
        }}>{t('results.filters.openFirstMatch')}</button></div>
      </div>}
      <details className="compact-details"><summary>{t('results.filters.refine')}{listFilter.kind !== 'all' || listFilter.state !== 'all' ? ` · ${t('results.filters.applied')}` : ''}{listOrder === 'title' ? ` · ${t('results.filters.titleOrder')}` : ''}</summary>
        <div className="form-grid form-grid--two">
          <label className="field">{t('results.filters.kind')}<select value={listFilter.kind} onChange={event => changeListFilter({ kind: event.target.value as ResultListFilter['kind'] })}>
            <option value="all">{t('results.filters.allKinds')}</option>{(['text', 'link', 'file', 'page'] as const).map(kind => <option value={kind} key={kind}>{t(`results.kind.${kind}`)}</option>)}
          </select></label>
          <label className="field">{t('results.filters.state')}<select value={listFilter.state} onChange={event => changeListFilter({ state: event.target.value as ResultListFilter['state'] })}>
            <option value="all">{t('results.filters.allStates')}</option>{(['draft', 'reviewing', 'final'] as const).map(state => <option value={state} key={state}>{t(`results.state.${state}`)}</option>)}
          </select></label>
          <label className="field">{t('results.filters.order')}<select value={listOrder} onChange={event => {
            if (event.target.value === 'received' || event.target.value === 'title') changeListFilter({ order: event.target.value })
          }}>
            <option value="received">{t('results.filters.receivedOrder')}</option><option value="title">{t('results.filters.titleOrder')}</option>
          </select></label>
        </div>
        <p className="field-hint">{t('results.filters.orderHelp')}</p>
      </details>
      {hasListOptions && <button type="button" className="button button--ghost button--compact" onClick={() => changeListFilter({ query: '', kind: 'all', state: 'all', order: 'received' })}>{t('results.filters.reset')}</button>}
      {list.hasNextPage && <p className="notice notice--info">{t('results.filters.partial')}</p>}
      {list.data && results.length > 0 && shownResults.length === 0 && <p role="status">{t('results.filters.noMatches')}</p>}
      {list.data && results.length > 0 && shownResults.length === 0 && (listFilter.kind !== 'all' || listFilter.state !== 'all') && <div>
        <button type="button" className="button button--secondary button--compact" onClick={() => {
          if (!sameOwner() || !mounted.current || !view.current.visible) return
          changeListFilter({ kind: 'all', state: 'all' })
          // The action disappears after reset; focus the stable search before commit.
          listFinder.current?.focus()
        }}>{t('results.filters.widenKeepingQuery')}</button>
      </div>}
      {selection && hasListFilter && !shownResults.some(item => item.result_id === selection.id) && <p className="metadata">{t('results.filters.selectionKept')}</p>}
    </section>
    {!list.isPending && !list.isError && !results.length && <p className="section-copy">{t('results.empty')}</p>}
    {shownResults.length > 0 && <nav className="stack stack--tight" aria-label={t('results.segments.title')}>
      <p role="status" className="metadata">{t('results.segments.range', { from: listStart + 1, to: Math.min(listStart + 12, shownResults.length), total: shownResults.length })}</p>
      {shownResults.length > 12 && <div className="page-actions">
        <button type="button" className="text-link" disabled={listStart === 0} onClick={() => moveList(0)}>{t('results.segments.first')}</button>
        <button type="button" className="text-link" disabled={listStart === 0} onClick={() => moveList(Math.max(0, listStart - 12))}>{t('results.segments.previous')}</button>
        <button type="button" className="text-link" disabled={listStart + 12 >= shownResults.length} onClick={() => moveList(listStart + 12)}>{t('results.segments.next')}</button>
        <button type="button" className="text-link" disabled={listStart + 12 >= shownResults.length} onClick={() => moveList(Math.floor((shownResults.length - 1) / 12) * 12)}>{t('results.segments.last')}</button>
      </div>}
      <p className="field-hint">{t('results.segments.help')}</p>
      <p id={`result-list-keyboard-${mount}`} className="field-hint">{t('results.listKeyboard')}</p>
    </nav>}
    <ul ref={listRoot} tabIndex={-1} aria-label={t('results.filters.title')} id={`result-list-${mount}`} className="result-list">{listWindow.map((item, index) => <li key={item.result_id}><button type="button" className="result-list__item"
      ref={node => { if (node) listButtons.current.set(item.result_id, node); else listButtons.current.delete(item.result_id) }} aria-pressed={selection?.id === item.result_id} aria-describedby={`result-list-keyboard-${mount}`} aria-keyshortcuts="ArrowUp ArrowDown Home End Enter" onKeyDown={event => readListWithKeys(event, index)} onClick={() => choose(item.result_id, item.version, true)}>
      <strong>{item.title}</strong><span className="metadata">{t(`results.kind.${item.kind}`)} · v{item.version} · {t(`results.state.${item.state}`)} · {item.author_name_snapshot || t('results.unrecorded')} · Agent #{item.author_agent_id}</span>
      <span className="metadata" style={{ overflowWrap: 'anywhere' }}>{t('results.identity.reference', { id: item.result_id })}</span>
      <span className="metadata">{auditTime(item.created_at)}</span>
    </button></li>)}</ul>
    {list.hasNextPage && <button type="button" className="button button--secondary" disabled={list.isFetching} onClick={() => { void list.fetchNextPage({ cancelRefetch: false }) }}>{t(list.isFetchingNextPage ? 'common.loading' : list.isFetchNextPageError ? 'results.filters.retryPage' : 'results.more')}</button>}
    </div>
    {selection && current?.kind!=='page' && <><ResultVersionNavigator key={selection.id} resultId={selection.id} version={selection.version} rows={versionRows} latest={knownLatest} receivedAt={versionReceivedAt} loading={versions.isFetching} more={!!versions.hasNextPage} moreFailed={versions.isFetchNextPageError} canUse={() => sameOwner() && mounted.current && view.current.visible} onChoose={version => choose(selection.id, version)} onRefresh={() => { void versions.refetch({ cancelRefetch: false }) }} onMore={() => { void versions.fetchNextPage({ cancelRefetch: false }) }}/>
      {versions.isPending && <p role="status">{t('results.reads.loadingVersions')}</p>}
      <ResultReadNotice failed={versions.isError} fetching={versions.isFetching} known={!!versions.data} retryLabel="results.reads.versions" onRetry={() => { if (sameOwner() && mounted.current && view.current.visible) void versions.refetch({ cancelRefetch: false }) }} />
    </>}
    {selection && current?.kind==='page' && <div ref={detailRoot} tabIndex={-1} className="page-result" key={`${selection.id}:${selection.version}`}>
      <header className="page-result__header"><button type="button" className="text-link" onClick={()=>{if(sameOwner()&&mounted.current&&view.current.visible)props.onPageClose?.()}}>{t('pageView.backToChat')}</button><h2 ref={heading} id={`result-detail-heading-${mount}`} tabIndex={-1}>{current.title}</h2><PageRename key={`${identity}:${roomId}:${current.result_id}`} ownerId={ownerId} authEpoch={authEpoch} roomId={roomId} resultId={current.result_id} version={current.version} title={current.title} enabled={active&&visible&&current.version===current.current_version&&!detail.isFetching&&!detail.isError} canUse={()=>sameOwner()&&mounted.current&&view.current.visible} onSaved={version=>{if(sameOwner()&&mounted.current&&view.current.visible){void cache.invalidateQueries({queryKey:prefix});choose(current.result_id,version)}}}/><details className="compact-details"><summary>{t('results.versionNav.title')} · v{current.version}</summary><ResultVersionNavigator resultId={selection.id} version={selection.version} rows={versionRows} latest={knownLatest} receivedAt={versionReceivedAt} loading={versions.isFetching} more={!!versions.hasNextPage} moreFailed={versions.isFetchNextPageError} canUse={()=>sameOwner()&&mounted.current&&view.current.visible} onChoose={version=>choose(selection.id,version)} onRefresh={()=>{void versions.refetch({cancelRefetch:false})}} onMore={()=>{void versions.fetchNextPage({cancelRefetch:false})}}/><ResultReadNotice failed={versions.isError} fetching={versions.isFetching} known={!!versions.data} retryLabel="results.reads.versions" onRetry={()=>{void versions.refetch({cancelRefetch:false})}}/>{sourceButton(current.source.seq,current.source.event_id)}</details></header>
      <PageMarkdownExport key={`page-markdown:${identity}:${roomId}:${current.result_id}:${current.version}`} ownerId={ownerId} epoch={authEpoch} roomId={roomId} resultId={current.result_id} version={current.version} title={current.title} content={current.content} canUse={()=>sameOwner()&&mounted.current&&view.current.visible&&readResultWorkspace(identity,roomId).selection?.id===current.result_id&&readResultWorkspace(identity,roomId).selection?.version===current.version}/>
      <PageXlsxExport key={`page-xlsx:${identity}:${roomId}:${current.result_id}:${current.version}`} ownerId={ownerId} epoch={authEpoch} roomId={roomId} resultId={current.result_id} version={current.version} title={current.title} content={current.content} canUse={()=>sameOwner()&&mounted.current&&view.current.visible&&readResultWorkspace(identity,roomId).selection?.id===current.result_id&&readResultWorkspace(identity,roomId).selection?.version===current.version}/>
      <PageWorkspace key={`${identity}:${roomId}:${current.result_id}:${current.version}`} value={current} ownerId={ownerId} authEpoch={authEpoch} active={active&&visible} canUse={()=>sameOwner()&&mounted.current&&view.current.visible} events={events} actions={props.pageActions||[]} renderApproval={props.renderPageApproval} onStop={props.onPageStop} stopPending={props.pageStopPending} onNavigate={onNavigate} onEvent={event=>{props.onPageEvent?.(event);void cache.invalidateQueries({queryKey:prefix})}} onLatest={()=>{void ownerRead(()=>roomResultsApi.pageCurrent(roomId,current.result_id)).then(latest=>{if(sameOwner()&&mounted.current&&view.current.visible&&readResultWorkspace(identity,roomId).selection?.id===current.result_id&&readResultWorkspace(identity,roomId).selection?.version===current.version&&latest.room_id===roomId&&latest.result_id===current.result_id&&latest.kind==='page'&&latest.version===latest.current_version&&latest.author_agent_id===current.author_agent_id)choose(latest.result_id,latest.version)}).catch(()=>{})}} />
    </div>}
    {selection && current?.kind!=='page' && <div ref={detailRoot} tabIndex={-1} aria-label={t('results.details')} className="result-detail" key={`${selection.id}:${selection.version}`}>
      <div className="page-actions"><button type="button" className="text-link" onClick={returnToList}>{t('results.backToList')}</button>
        <button type="button" className="text-link" aria-describedby={`result-detail-close-help-${mount}`} onClick={event => closeResultDetail(event.currentTarget)}>{t('results.closeDetail')}</button>
        {current && <button type="button" className="text-link" aria-controls={`result-detail-end-${mount}`} onClick={event => moveWithinDetail('end', event.currentTarget)}>{t('results.readingNavigation.end')}</button>}</div>
      <p id={`result-detail-close-help-${mount}`} className="field-hint">{t('results.closeDetailHelp')}</p>
      {detail.isPending && <p role="status">{t('common.loading')}</p>}
      <ResultReadNotice failed={detail.isError} fetching={detail.isFetching} known={!!detail.data} retryLabel="results.reads.detail" onRetry={() => { void detail.refetch({ cancelRefetch: false }) }} />
      {current && <>
        <h3 ref={heading} id={`result-detail-heading-${mount}`} tabIndex={-1}>{current.title} · v{current.version}</h3>
        {versionHref && <ResultVersionLink key={versionHref} copyContext={`${identity}:${ownerGeneration}:${roomId}:${mount}:${current.result_id}:${current.version}`} title={current.title} version={current.version} href={versionHref} canUse={() => sameOwner() && mounted.current && view.current.visible && readResultWorkspace(identity, roomId).selection?.id === current.result_id && readResultWorkspace(identity, roomId).selection?.version === current.version}/>}
        <p className="metadata" style={{ overflowWrap: 'anywhere' }}>{t('results.identity.reference', { id: current.result_id })}</p>
        <p className="metadata">{current.author_name_snapshot || t('results.unrecorded')} · Agent #{current.author_agent_id} · {auditTime(current.created_at)} · {t(`results.state.${current.state}`)}</p>
        {sourceButton(current.source.seq, current.source.event_id)}
        {current.kind !== 'file' && <ResultContentCopy findMemory={{ identity, generation: ownerGeneration, key: `result-find:${roomId}:${current.result_id}:${current.version}` }} findRef={textFind} key={`${current.result_id}:${current.version}`} copyContext={`${identity}:${ownerGeneration}:${roomId}:${mount}:${current.result_id}:${current.version}`} content={current.content} version={current.version} canCopy={() => sameOwner() && mounted.current && view.current.visible && readResultWorkspace(identity, roomId).selection?.id === current.result_id && readResultWorkspace(identity, roomId).selection?.version === current.version}/>}
        {current.kind !== 'file' && <ResultFileSave key={`file:${current.result_id}:${current.version}`} ownerId={ownerId} epoch={authEpoch} roomId={roomId} resultId={current.result_id} version={current.version} title={current.title} content={current.content} canUse={() => sameOwner() && mounted.current && view.current.visible && readResultWorkspace(identity, roomId).selection?.id === current.result_id && readResultWorkspace(identity, roomId).selection?.version === current.version}/>}
        {content(current)}
        {current.kind === 'text' && <ResultRevision key={`${identity}:${roomId}:${current.result_id}`} value={current} ownerId={ownerId} identity={identity} active={active && visible} canUse={() => sameOwner() && mounted.current && view.current.visible && view.current.active} canStart={!pending && !detail.isFetching && !detail.isError && current.version === current.current_version && current.version === knownLatest} onNavigate={onNavigate} onOpenVersion={(id, version) => choose(id, version)} onSaved={() => { void cache.invalidateQueries({ queryKey: ['room-results', ownerId, authEpoch, roomId, mount] }) }} />}
        {current.kind === 'link' && <p role="status">{t(current.link_report === 'reported_bad' ? 'results.reportedBad' : 'results.unchecked')}</p>}
        {(current.current_version > 1 || current.version > 1 || compare) && <><button type="button" className="text-link" aria-expanded={compare} onClick={() => setCompare(!compare)}>{t('results.comparison.open')}</button>
          {compare && <section aria-label={t('results.comparison.base')} className="result-previous stack stack--tight">
            <label className="field">{t('results.comparison.base')}<select value={pinnedBase === undefined ? 'previous' : String(pinnedBase)} onChange={event => chooseComparison(event.target.value === 'previous' ? null : Number(event.target.value))}>
              <option value="previous">{t('results.comparison.previous')}</option>
              {[...comparisonOptions].sort((a, b) => b[0] - a[0]).map(([version, title]) => <option key={version} value={version} disabled={version === current.version}>v{version}{title ? ` · ${title}` : ''}</option>)}
            </select></label>
            <ResultComparisonJump key={`${current.result_id}:${current.version}`} resultId={current.result_id} version={current.version} latest={knownLatest}
              canUse={() => sameOwner() && mounted.current && view.current.visible && readResultWorkspace(identity, roomId).selection?.id === current.result_id && readResultWorkspace(identity, roomId).selection?.version === current.version}
              onChoose={version => chooseComparison(version, true)}/>
            <p className="field-hint">{t('results.comparison.retained')}</p>
            {versions.hasNextPage && <div><p className="metadata">{t('results.comparison.partial')}</p><button type="button" className="button button--secondary button--compact" disabled={versions.isFetching} onClick={() => { if (sameOwner() && mounted.current && view.current.visible) void versions.fetchNextPage({ cancelRefetch: false }) }}>{t(versions.isFetching ? 'common.loading' : 'results.comparison.more')}</button></div>}
            {!comparisonReady && <p role="status">{t('results.comparison.chooseOther')}</p>}
            {comparisonReady && <><h4>{t('results.comparison.reading', { version: comparisonVersion! })}</h4>
              {previous.isPending && <p role="status">{t('common.loading')}</p>}<ResultReadNotice failed={previous.isError} fetching={previous.isFetching} known={!!previous.data} retryLabel="results.comparison.retry" onRetry={() => { if (sameOwner() && mounted.current && view.current.visible) void previous.refetch({ cancelRefetch: false }) }} />
              {previous.data && <><ResultVersionDiff key={`${current.result_id}:${current.version}:${previous.data.version}`} before={previous.data} after={current} canChange={() => sameOwner() && mounted.current && view.current.visible}/>
                <ResultComparisonTools key={`${current.result_id}:${current.version}:${previous.data.version}`} value={previous.data} primaryVersion={current.version} ownerId={ownerId} epoch={authEpoch} roomId={roomId} canUse={() => !!previous.data && comparisonSourceCurrent(previous.data)}/>
                {previous.data.kind === 'file'
                  ? <><strong>{previous.data.title} · v{previous.data.version}</strong>{content(previous.data)}{sourceButton(previous.data.source.seq, previous.data.source.event_id)}</>
                  : <ResultComparisonOriginal key={`${current.result_id}:${current.version}:${previous.data.version}`} version={previous.data.version} title={previous.data.title}
                      canUse={() => !!previous.data && comparisonSourceCurrent(previous.data)}>
                      {canRead => <>{content(previous.data!, canRead)}{sourceButton(previous.data!.source.seq, previous.data!.source.event_id)}</>}
                    </ResultComparisonOriginal>}</>}
            </>}
          </section>}</>}
        <details className="compact-details"><summary>{t('results.details')}</summary><dl className="result-metadata">
          <dt>{t('results.author')}</dt><dd>{current.author_name_snapshot || t('results.unrecorded')} · Agent #{current.author_agent_id}</dd>
          <dt>{t('results.run')}</dt><dd>{current.execution.trigger_event_id || t('results.unrecorded')} · {current.execution.round ?? '—'} / {current.execution.generation ?? '—'}</dd>
          <dt>{t('review.model')}</dt><dd>{current.execution.model_use ? `${current.execution.model_use.model_id} · #${current.execution.model_use.connection_id}` : t('results.unrecorded')}</dd>
          <dt>{t('results.ownerState')}</dt><dd>{current.state_owner_id ? <>Owner #{current.state_owner_id} · {auditTime(current.state_changed_at)}</> : t('results.unrecorded')}</dd>
          {current.kind === 'link' && <><dt>{t('results.report')}</dt><dd>{current.link_report_owner_id ? <>Owner #{current.link_report_owner_id} · {auditTime(current.link_reported_at)}</> : t('results.unrecorded')}</dd></>}
          <dt>{t('results.evidence')}</dt><dd>{!current.evidence.length ? t('results.noEvidence') : <ul>{current.evidence.map((ref, i) => <li key={i}>
            {ref.event_id && ref.seq ? sourceButton(ref.seq, ref.event_id) : ref.url && resultHttpUrl(ref.url) ? <a className="text-link" href={ref.url} target="_blank" rel="noopener noreferrer">{ref.url}</a> : t('results.unavailable')}
          </li>)}</ul>}</dd>
        </dl><p className="metadata">{t('results.evidenceLimit')}</p></details>
        <div className="page-actions" aria-label={t('results.changeState')}>{(['draft', 'reviewing', 'final'] as ResultState[]).map((state) => <button type="button" key={state}
          className="button button--secondary button--compact" disabled={!active || pending || detail.isFetching || detail.isError || current.state === state}
          onClick={() => startCommand(current, { operation: 'state', state, expected_state_revision: current.state_revision })}>{t(`results.state.${state}`)}</button>)}</div>
        {current.kind === 'link' && <button type="button" className="text-link" disabled={!active || pending || detail.isFetching || detail.isError} onClick={() => startCommand(current, {
          operation: 'link-report', report: current.link_report === 'reported_bad' ? 'unchecked' : 'reported_bad', expected_state_revision: current.state_revision,
        })}>{t(current.link_report === 'reported_bad' ? 'results.clearReport' : 'results.report')}</button>}
        <section aria-label={t('results.opinions')} className="stack stack--tight"><h4>{t('results.opinions')}</h4><p className="metadata">{t(current.review_support === 'unsupported' ? 'results.fileReviewUnsupported' : 'results.opinionLimit')}</p>
          {opinions.isPending && <p role="status">{t('results.reads.loadingOpinions')}</p>}
          <ResultReadNotice failed={opinions.isError} fetching={opinions.isFetching} known={!!opinions.data} retryLabel="results.reads.opinions" onRetry={() => { void opinions.refetch({ cancelRefetch: false }) }} />
          {!opinions.isPending && !opinions.isError && !linked.length && <p>{t('results.noOpinions')}</p>}
          {linked.map((opinion) => <p key={opinion.review_id}>{opinion.reviewer_name_snapshot || t('results.unrecorded')} · v{opinion.version} · {auditTime(opinion.created_at)} · {sourceButton(opinion.review_seq, opinion.review_event_id)}</p>)}
          {opinions.hasNextPage && <button type="button" disabled={opinions.isFetching} onClick={() => { void opinions.fetchNextPage({ cancelRefetch: false }) }}>{t(opinions.isFetchingNextPage ? 'common.loading' : opinions.isFetchNextPageError ? 'results.reads.nextOpinions' : 'results.more')}</button>}
          {candidates.filter((event) => !linked.some((item) => item.review_event_id === event.event_id)).map((event) => <div key={event.event_id} className="result-opinion-candidate">
            {sourceButton(event.seq, event.event_id)}<span>{event.actor_name_snapshot} · {replyExcerpt(String(event.payload.content))}</span>
            <button type="button" className="button button--secondary button--compact" disabled={!active || pending || detail.isFetching || detail.isError}
              onClick={() => startCommand(current, { operation: 'review', review_event_id: event.event_id, reviewSeq: event.seq })}>{t('results.linkOpinion')}</button>
          </div>)}
        </section>
        <nav ref={detailEnd} id={`result-detail-end-${mount}`} tabIndex={-1} className="page-actions" aria-label={t('results.readingNavigation.title')}>
          <button type="button" className="text-link" onClick={returnToList}>{t('results.backToList')}</button>
          <button type="button" className="text-link" aria-describedby={`result-detail-close-help-${mount}`} onClick={event => closeResultDetail(event.currentTarget)}>{t('results.closeDetail')}</button>
          <button type="button" className="text-link" aria-controls={`result-detail-heading-${mount}`} onClick={event => moveWithinDetail('top', event.currentTarget)}>{t('results.readingNavigation.top')}</button>
        </nav>
      </>}
    </div>}
  </section>
}

function ResultSaveForm({ draft, results, disabled, sourceAvailable, visible, autoFocus, targetQueryKey, readTarget, onChange, onClose, onDiscard, onSave }: {
  draft: ResultDraft; results: import('../api/roomResults').ResultSummary[]; disabled: boolean; sourceAvailable: boolean; visible: boolean; autoFocus: boolean
  targetQueryKey: readonly unknown[]; readTarget: (id: string) => Promise<ResultTarget>
  onChange: (patch: Partial<Pick<ResultDraft, 'kind' | 'title' | 'url' | 'evidence' | 'target'>>) => void
  onClose: () => void; onDiscard: () => void; onSave: (body: ResultSave, draft: ResultDraft) => void
}) {
  const { t } = useI18n()
  const titleRef = useRef<HTMLInputElement>(null)
  const ime = useImeFormGuard(`${draft.sourceId}:${draft.editId}`)
  useEffect(() => { if (autoFocus) titleRef.current?.focus({ preventScroll: true }) }, [])
  const target = useQuery({ queryKey: targetQueryKey, queryFn: () => readTarget(draft.target!.id), enabled: visible && !!draft.target, retry: false, gcTime: 0 })
  const targetReady = !draft.target || target.isSuccess && !target.isFetching && target.data.id === draft.target.id && target.data.version === draft.target.version
  const changedTarget = !!(draft.target && target.data && target.data.version !== draft.target.version)
  const validKind = draft.sourceKind === 'file' ? draft.kind === 'file' : draft.kind === 'text' || draft.kind === 'link'
  const useLatest = () => {
    if (disabled || !draft.target || !target.isSuccess || target.isFetching || !window.confirm(t('results.targetRebaseConfirm'))) return
    onChange({ target: Object.freeze({ ...target.data }) })
  }
  return <form {...ime.handlers} className="result-save stack stack--tight" aria-label={t('results.save')} onSubmit={event => {
    event.preventDefault()
    if (ime.isSubmitBlocked(event) || disabled || !sourceAvailable || !validKind || !targetReady || !draft.title.trim()
      || draft.kind === 'link' && !resultHttpUrl(draft.url) || draft.evidence && !resultHttpUrl(draft.evidence)) return
    onSave({ source_event_id: draft.sourceId, kind: draft.kind, title: draft.title.trim(),
      ...(draft.kind === 'link' ? { url: draft.url } : {}), evidence: draft.evidence ? [{ url: draft.evidence }] : [] }, draft)
  }}>
    <h3>{t('results.save')}</h3><details className="compact-details"><summary>{t('common.inputNotes')}</summary><p>{t('results.saveHelp')}</p><p>{t('results.sessionLimit')}</p><p className="metadata">#{draft.sourceSeq}</p></details>
    {!sourceAvailable && <p role="status" className="notice notice--warning">{t('results.sourceMissing')}</p>}
    <fieldset disabled={disabled} className="stack stack--tight">
      <label className="field">{t('results.title')}<input ref={titleRef} value={draft.title} maxLength={200} required onChange={event => onChange({ title: event.target.value })} /></label>
      {draft.sourceKind === 'text' && <label className="field">{t('results.kindLabel')}<select value={draft.kind} onChange={event => onChange({ kind: event.target.value as 'text' | 'link' })}><option value="text">{t('results.kind.text')}</option><option value="link">{t('results.kind.link')}</option></select></label>}
      {draft.kind === 'link' && <label className="field">{t('results.url')}<input type="url" value={draft.url} required maxLength={2048} onChange={event => onChange({ url: event.target.value })} /></label>}
      <label className="field">{t('results.target')}<select value={draft.target?.id || ''} onChange={event => {
        const item = results.find(row => row.result_id === event.target.value)
        if (event.target.value && !item) return
        onChange({ target: item ? Object.freeze({ id: item.result_id, version: item.version, title: item.title }) : null })
      }}><option value="">{t('results.new')}</option>
        {draft.target && <option value={draft.target.id}>{draft.target.title} · v{draft.target.version} · {t('results.identity.reference', { id: draft.target.id })}</option>}
        {results.filter(item => item.result_id !== draft.target?.id).map(item => <option key={item.result_id} value={item.result_id}>{item.title} · v{item.version} · Agent #{item.author_agent_id} · {t('results.identity.reference', { id: item.result_id })}</option>)}
      </select></label>
      {(draft.target || results.length > 1) && <p className="field-hint">{t('results.targetIdentityHelp')}</p>}
      {draft.target && <div className="notice notice--info stack stack--tight">
        <strong>{draft.target.title} · v{draft.target.version}</strong>
        <p className="metadata" style={{ overflowWrap: 'anywhere' }}>{t('results.identity.reference', { id: draft.target.id })}</p>
        <p>{t('results.pinnedTarget', { version: draft.target.version })}</p>
        {target.isPending && <p role="status">{t('common.loading')}</p>}
        {target.isError && <p role="status">{t('results.targetUnavailable')}</p>}
        {changedTarget && <p role="alert">{t('results.targetChanged', { base: draft.target.version, current: target.data!.version })}</p>}
        <button type="button" className="button button--secondary" disabled={target.isFetching} onClick={() => { void target.refetch({ cancelRefetch: false }) }}>{t('results.readTarget')}</button>
        {changedTarget && <button type="button" className="button button--secondary" disabled={target.isFetching} onClick={useLatest}>{t('results.useLatestTarget')}</button>}
      </div>}
      <details><summary>{t('results.evidenceOptional')}</summary><label className="field">{t('results.evidence')}<input type="url" value={draft.evidence} maxLength={2048} onChange={event => onChange({ evidence: event.target.value })} /></label></details>
      <button type="submit" className="button button--primary" disabled={!sourceAvailable || !validKind || !targetReady || !draft.title.trim() || draft.kind === 'link' && !resultHttpUrl(draft.url) || !!draft.evidence && !resultHttpUrl(draft.evidence)}>{t('results.save')}</button>
    </fieldset><div className="page-actions"><button type="button" className="text-link" onClick={onClose}>{t('results.closeKeepDraft')}</button>
      {draft.dirty && <button type="button" className="text-link danger-link" onClick={onDiscard}>{t('results.discardDraft')}</button>}</div>
  </form>
}

function ResultReadNotice({ failed, fetching, known, retryLabel, onRetry }: {
  failed: boolean; fetching: boolean; known: boolean; retryLabel: MessageKey; onRetry: () => void
}) {
  const { t } = useI18n()
  if (!failed) return null
  return <div className="notice notice--warning stack stack--tight">
    <p role="alert">{t('results.unavailable')}</p>
    {known && <p className="metadata">{t('results.reads.retained')}</p>}
    <button type="button" className="button button--secondary button--compact" disabled={fetching} onClick={onRetry}>{t(fetching ? 'common.loading' : retryLabel)}</button>
  </div>
}
