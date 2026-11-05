import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useNavigate } from 'react-router-dom'
import { Pin, Search, X } from 'lucide-react'
import { roomIndexApi, type RoomIndexItem, type RoomIndexStatus } from '../api/roomIndex'
import { projectsApi, type Project } from '../api/projects'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { createPortal } from 'react-dom'
import { readPastedChatReference } from '../lib/resultLink'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { readRecentResults, subscribeRecentResults, recentResultRevision, forgetRecentResult, recentResultUndoCount, restoreForgottenRecentResults, type RecentResultView } from '../lib/recentResultViews'
import { directoryMatcher } from './DirectorySearch'
import { useExplicitReaderReveal } from '../lib/useExplicitReaderReveal'
import StoredDraftReader, { type ReceivedDraft } from './StoredDraftReader'
import StoredDraftSearch, { type StoredDraftSearchHandle } from './StoredDraftSearch'
import { listUnsavedDrafts, subscribeUnsavedDrafts, unsavedDraftRevision } from '../lib/unsavedComposerDrafts'
import { observedDraftRows, draftRoomName, subscribeDraftIndex, draftIndexRevision } from '../lib/composerDraftIndex'

const canonicalOrder = (left: RoomIndexItem, right: RoomIndexItem) => Number(right.pinned) - Number(left.pinned)
  || right.last_activity_at.localeCompare(left.last_activity_at) || right.room_id - left.room_id

type RoomSelection = { scope: string; id: number | null; index: number }

export default function QuickRoomSwitcher() {
  useSyncExternalStore(subscribeRecentResults, recentResultRevision)
  useSyncExternalStore(subscribeUnsavedDrafts, unsavedDraftRevision)
  useSyncExternalStore(subscribeDraftIndex, draftIndexRevision)
  const [localRead,setLocalRead]=useState<{identity:string;generation:number;draft:ReceivedDraft;editId:string;at:string}|null>(null), localReadId=useRef(0)
  const [draftSource,setDraftSource]=useState<'local'|'stored'>('local'), storedSearch=useRef<StoredDraftSearchHandle>(null)
  const [mode, setMode] = useState<'rooms' | 'recent' | 'drafts'>('rooms'), [recentSelection, setRecentSelection] = useState<string | null>(null), [draftSelection, setDraftSelection] = useState<number | null>(null)
  const [open, setOpen] = useState(false), [query, setQuery] = useState(''), [searchQuery, setSearchQuery] = useState('')
  const [status, setStatus] = useState<RoomIndexStatus>('active'), [pinnedOnly, setPinnedOnly] = useState(false)
  const [items, setItems] = useState<RoomIndexItem[]>([]), [resultScope, setResultScope] = useState('')
  const [projectId, setProjectId] = useState(0), [projects, setProjects] = useState<Project[]>([])
  const [next, setNext] = useState<string | null>(null), [refresh, setRefresh] = useState(0), [projectRefresh, setProjectRefresh] = useState(0)
  const [loading, setLoading] = useState(false), [loadFailure, setLoadFailure] = useState<'first' | 'more' | null>(null)
  const [projectsLoading, setProjectsLoading] = useState(false), [projectsError, setProjectsError] = useState(false)
  const [pinError, setPinError] = useState(false), [selection, setSelection] = useState<RoomSelection>({ scope: '', id: null, index: 0 }), [pinning, setPinning] = useState<number[]>([])
  const [composing, setComposing] = useState(false), composingRef = useRef(false), imeNavigationAfter = useRef(0)
  const identity = useAuthStore((value) => `${value.user?.id || 0}:${value.epoch}`)
  const recentGeneration = useMemo(() => directoryViewGeneration(), [identity])
  const sameOwner = () => recentGeneration === directoryViewGeneration() && identity === `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}`
  const previous = useRef<{ element: HTMLElement; start?: number; end?: number; direction?: 'forward' | 'backward' | 'none' } | null>(null)
  const input = useRef<HTMLInputElement>(null), dialog = useRef<HTMLElement>(null), generation = useRef(0)
  const opened = useRef(false), mounted = useRef(true), dialogSession = useRef(0), focusFrame = useRef<number | null>(null), listPending = useRef<number | null>(null)
  const viewScope = useRef('')
  const pinningRef = useRef(new Set<number>()), lastScope = useRef(''), navigate = useNavigate(), { t, formatDateTime } = useI18n()
  const resultLink = useMemo(() => readPastedChatReference(query), [query])
  const searchTooLong = mode === 'rooms' && resultLink.state === 'none' && query.trim().length > 200
  const referenceMode = resultLink.state !== 'none' || searchTooLong
  const suppressSearch = referenceMode || mode !== 'rooms'
  const recent = readRecentResults(identity)
  const undoRecentCount = recentResultUndoCount(identity, recentGeneration)
  const recentRows = mode === 'recent' && !referenceMode ? recent.filter(item => directoryMatcher(query)([item.title, String(item.room), `#${item.room}`, `v${item.version}`, item.base === null ? '' : `v${item.base}`])) : []
  const recentIndex = Math.max(0, recentRows.findIndex(item => item.key === recentSelection))
  const recentActive = recentRows[recentIndex]
  const recentDOM = (item: RecentResultView) => `recent-result-${item.room}-${item.id}-${item.version}`
  const localDraftMap = new Map(listUnsavedDrafts(identity).map(row => [row.room,row]))
  const readLocalCurrent = localRead ? localDraftMap.get(localRead.draft.room) : undefined
  const localReadState: 'same'|'changed'|'unavailable' = !readLocalCurrent ? 'unavailable' : localRead?.editId===readLocalCurrent.editId && localRead.draft.content===readLocalCurrent.content ? 'same' : 'changed'
  const allDraftRows = mode === 'drafts' && !referenceMode ? (() => {
    const rows = new Map(observedDraftRows(identity).filter(row => row.chars > 0).map(row => [row.room, { ...row, source: 'received' as 'received' | 'local' }]))
    for (const row of localDraftMap.values()) {
      if (!row.content.trim()) rows.delete(row.room)
      else rows.set(row.room, { room: row.room, chars: row.content.length, preview: Array.from(row.content.replace(/\s+/g, ' ').slice(0, 201)).slice(0, 100).join(''), version: 0, observedAt: row.updatedAt, source: 'local' })
    }
    const match = directoryMatcher(query)
    return [...rows.values()].filter(row => match([draftRoomName(identity, row.room), String(row.room), `#${row.room}`, row.source === 'local' ? localDraftMap.get(row.room)?.content : row.preview])).sort((a,b) => b.observedAt.localeCompare(a.observedAt) || b.room - a.room)
  })() : []
  const draftRows = draftSource === 'local' ? allDraftRows : []
  const storedMode = mode === 'drafts' && draftSource === 'stored' && !referenceMode
  const draftIndex = Math.max(0, draftRows.findIndex(row => row.room === draftSelection)), draftActive = draftRows[draftIndex]
  const scope = JSON.stringify([identity, status, projectId, query.trim()])
  viewScope.current = scope
  const selectionScope = JSON.stringify([scope, pinnedOnly])
  const requestScope = JSON.stringify([identity, status, projectId, searchQuery])
  const pendingSearch = !suppressSearch && (composing || query.trim() !== searchQuery)
  const resultsCurrent = !suppressSearch && resultScope === scope && !pendingSearch
  const visible = useMemo(() => !resultsCurrent ? [] : pinnedOnly ? items.filter((item) => item.pinned) : items, [items, pinnedOnly, resultsCurrent])
  const indexFor = (value: RoomSelection) => {
    if (value.scope !== selectionScope || !visible.length) return 0
    const index = visible.findIndex((item) => item.room_id === value.id)
    return index >= 0 ? index : Math.max(0, Math.min(value.index, visible.length - 1))
  }
  const active = indexFor(selection)
  const remember = (index: number) => {
    const item = visible[index]
    if (!item || !opened.current || viewScope.current !== scope || !sameOwner() || composingRef.current || !resultsCurrent) return
    setSelection({ scope: selectionScope, id: item.room_id, index })
  }
  const move = (delta: number) => {
    if (!visible.length || !resultsCurrent || !opened.current || !sameOwner()) return
    setSelection((old) => { const index = Math.max(0, Math.min(indexFor(old) + delta, visible.length - 1))
      return { scope: selectionScope, id: visible[index].room_id, index }
    })
  }
  const cancelFocus = () => { if (focusFrame.current !== null) cancelAnimationFrame(focusFrame.current); focusFrame.current = null }
  const close = (restoreFocus = true) => {
    cancelFocus(); opened.current = false; const session = ++dialogSession.current
    generation.current += 1; listPending.current = null; setLoading(false)
    setLocalRead(null); setOpen(false); composingRef.current = false; setComposing(false); imeNavigationAfter.current = 0
    if (!restoreFocus) return
    const saved = previous.current
    focusFrame.current = requestAnimationFrame(() => {
      focusFrame.current = null
      if (!mounted.current || opened.current || session !== dialogSession.current || !sameOwner()
          || document.querySelector('[aria-modal="true"]') || !saved?.element.isConnected) return
      saved.element.focus()
      if ((saved.element instanceof HTMLTextAreaElement || saved.element instanceof HTMLInputElement)
          && saved.start !== undefined) {
        try { saved.element.setSelectionRange(saved.start, saved.end ?? saved.start, saved.direction) } catch { /* Non-text input types cannot restore selection. */ }
      }
    })
  }
  const show = () => {
    if (opened.current || !sameOwner() || document.querySelector('[aria-modal="true"]')) return
    cancelFocus(); const element = document.activeElement as HTMLElement
    const text = element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement && element.type !== 'password' ? element : null
    previous.current = { element, start: text?.selectionStart ?? undefined, end: text?.selectionEnd ?? undefined,
      direction: text?.selectionDirection ?? undefined }
    dialogSession.current += 1; opened.current = true; setOpen(true)
  }
  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; opened.current = false; dialogSession.current += 1; generation.current += 1; listPending.current = null; cancelFocus()
  } }, [])
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.keyCode === 229 || event.altKey || event.shiftKey
          || event.metaKey === event.ctrlKey || event.key.toLowerCase() !== 'k'
          || document.visibilityState !== 'visible' || !document.hasFocus()) return
      const foreignModal = Array.from(document.querySelectorAll('[aria-modal="true"]')).find((m) => m !== dialog.current && !dialog.current?.contains(m))
      if (foreignModal) return
      event.preventDefault()
      // Holding the shortcut must not repeatedly open and close the dialog.
      if (event.repeat) return
      if (open) close(); else show()
    }
    window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key)
  }, [open, identity])
  useEffect(() => {
    cancelFocus(); opened.current = false; dialogSession.current += 1; generation.current += 1; listPending.current = null
    setOpen(false); setLocalRead(null); setMode('rooms'); setDraftSource('local'); setRecentSelection(null); setDraftSelection(null); setQuery(''); setSearchQuery(''); setStatus('active'); setPinnedOnly(false); setItems([]); setProjects([])
    setProjectId(0); setNext(null); setLoading(false); setLoadFailure(null); setProjectsError(false); setPinError(false); setSelection({ scope: '', id: null, index: 0 })
    setPinning([]); pinningRef.current.clear(); previous.current = null; lastScope.current = ''; setResultScope('')
    composingRef.current = false; setComposing(false); imeNavigationAfter.current = 0
  }, [identity])
  useEffect(() => { if (open) input.current?.focus() }, [open])
  useEffect(() => {
    if (!open || composing) return
    const timer = window.setTimeout(() => setSearchQuery(query.trim()), 180)
    return () => window.clearTimeout(timer)
  }, [query, composing, open])
  useEffect(() => {
    if (!resultsCurrent || !visible.length) return
    setSelection((old) => { const index = indexFor(old), id = visible[index].room_id
      return old.scope === selectionScope && old.id === id && old.index === index ? old : { scope: selectionScope, id, index }
    })
  }, [visible, resultsCurrent, selectionScope])
  useEffect(() => {
    if (!open) return
    const selected = visible[active]
    if (selected) document.getElementById(`quick-room-${selected.room_id}`)?.scrollIntoView?.({ block: 'nearest' })
  }, [active, visible, open])
  useEffect(() => {
    if (suppressSearch) { setLoading(false); setLoadFailure(null); return }
    if (!open || !opened.current || pendingSearch || !sameOwner()) return
    const run = ++generation.current
    listPending.current = run; setLoading(true); setLoadFailure(null)
    if (lastScope.current !== requestScope) { setItems([]); setNext(null) }
    roomIndexApi.list({ status, project_id: projectId || undefined, q: searchQuery, limit: 50 }).then((page) => {
      if (run !== generation.current || !opened.current || !mounted.current || !sameOwner()) return
      lastScope.current = requestScope; setResultScope(requestScope); setItems(page.items); setNext(page.next_cursor)
      listPending.current = null; setLoading(false); setPinError(false)
    }).catch(() => { if (run === generation.current && opened.current && mounted.current && sameOwner()) { listPending.current = null; setLoading(false); setLoadFailure('first') } })
    return () => { generation.current += 1; if (listPending.current === run) listPending.current = null }
  }, [open, searchQuery, pendingSearch, status, projectId, identity, refresh, suppressSearch])
  useEffect(() => {
    if (!open || !opened.current || !sameOwner() || suppressSearch) return
    let cancelled = false
    setProjectsLoading(true); setProjectsError(false)
    projectsApi.list('all').then((value) => { if (!cancelled && sameOwner()) { setProjects(value); setProjectsLoading(false) } })
      .catch(() => { if (!cancelled && sameOwner()) { setProjectsError(true); setProjectsLoading(false) } })
    return () => { cancelled = true }
  }, [open, identity, projectRefresh, suppressSearch])
  const loadMore = async () => {
    if (!next || listPending.current !== null || loading || !opened.current || !resultsCurrent || !sameOwner()) return
    const cursor = next, run = ++generation.current
    listPending.current = run
    setLoading(true); setLoadFailure(null)
    try {
      const page = await roomIndexApi.list({ status, project_id: projectId || undefined, q: searchQuery, limit: 50, cursor })
      if (run !== generation.current || !opened.current || !mounted.current || !sameOwner()) return
      setItems((old) => { const known = new Set(old.map((item) => item.room_id)); return [...old, ...page.items.filter((item) => { if (known.has(item.room_id)) return false; known.add(item.room_id); return true })] })
      setNext(page.next_cursor); listPending.current = null; setLoading(false)
    } catch { if (run === generation.current && opened.current && mounted.current && sameOwner()) { listPending.current = null; setLoading(false); setLoadFailure('more') } }
  }
  const choose = (item: RoomIndexItem) => { if (!opened.current || viewScope.current !== scope || !sameOwner() || !resultsCurrent || composingRef.current) return; close(false); navigate(`/chat/${item.room_id}`) }
  const recentCurrent = () => opened.current && mounted.current && sameOwner() && viewScope.current === scope && input.current?.value === query && mode === 'recent' && !referenceMode && !composingRef.current && performance.now() >= imeNavigationAfter.current
  const openRecent = (item: RecentResultView) => {
    if (!recentCurrent() || !readRecentResults(identity).some(value => value.key === item.key && value.href === item.href)) return
    close(false); navigate(item.href)
  }
  const moveRecent = (delta: number) => {
    if (!recentCurrent() || !recentRows.length) return
    const item = recentRows[Math.max(0, Math.min(recentIndex + delta, recentRows.length - 1))]
    setRecentSelection(item.key); document.getElementById(recentDOM(item))?.scrollIntoView({ block: 'nearest' })
  }
  const forgetRecent = (key?: string) => {
    if (!recentCurrent()) return
    forgetRecentResult(identity, recentGeneration, key)
    requestAnimationFrame(() => { if (opened.current && sameOwner() && document.activeElement === document.body) input.current?.focus() })
  }
  const draftCurrent = () => opened.current && mounted.current && sameOwner() && viewScope.current === scope && input.current?.value === query && mode === 'drafts' && !referenceMode && !composingRef.current && performance.now() >= imeNavigationAfter.current
  const storedCurrent = () => draftCurrent() && draftSource === 'stored'
  const localReaderCurrent = () => draftCurrent() && draftSource === 'local' && localRead?.identity===identity && localRead.generation===directoryViewGeneration()
  const localReveal = useExplicitReaderReveal(localRead?.draft.readId??null,localReaderCurrent)
  const closeLocalReader=()=>{if(localReaderCurrent()){localReveal.back();setLocalRead(null)}}
  const readLocalDraft = (room: number, origin?:HTMLButtonElement) => {
    if (!draftCurrent() || draftSource !== 'local') return
    const original = listUnsavedDrafts(identity).find(row => row.room === room)
    if (!original) return
    if(origin)localReveal.begin(origin)
    setLocalRead({ identity, generation:recentGeneration, draft: { readId: ++localReadId.current, room, title: draftRoomName(identity,room) || t('chat.roomTitle',{id:String(room)}), project: '', content: original.content, listedVersion: 0, version: 0 }, editId: original.editId, at: original.updatedAt })
  }
  const openDraft = (room: number) => {
    if (!draftCurrent() || draftSource !== 'local') return
    const local = listUnsavedDrafts(identity).find(row => row.room === room)
    if (local ? !local.content.trim() : !observedDraftRows(identity).some(row => row.room === room && row.chars > 0)) return
    close(false); navigate(`/chat/${room}?compose=1`)
  }
  const moveDraft = (delta: number) => {
    if (!draftCurrent() || draftSource !== 'local' || !draftRows.length) return
    const row = draftRows[Math.max(0, Math.min(draftIndex + delta, draftRows.length - 1))]
    setDraftSelection(row.room); document.getElementById(`composer-draft-${row.room}`)?.scrollIntoView({ block: 'nearest' })
  }
  const openLocalReadRoom = () => { if (!localRead || !localReaderCurrent()) return; const room=localRead.draft.room; close(false); navigate(`/chat/${room}?compose=1`) }
  const openStoredDraft = (room: number) => { if (!storedCurrent() || !Number.isSafeInteger(room) || room < 1) return; close(false); navigate(`/chat/${room}?compose=1`) }
  const openResultLink = () => {
    if (!opened.current || !mounted.current || !sameOwner() || viewScope.current !== scope || composingRef.current || performance.now() < imeNavigationAfter.current || input.current?.value !== query) return
    const target = readPastedChatReference(query)
    if (target.state !== 'valid') return
    close(false); navigate(target.href)
  }
  const pin = async (item: RoomIndexItem) => {
    if (!opened.current || viewScope.current !== scope || !resultsCurrent || !sameOwner() || pinningRef.current.has(item.room_id)) return
    const target = !item.pinned, session = dialogSession.current, focused = document.activeElement
    pinningRef.current.add(item.room_id); setPinning((old) => [...old, item.room_id]); setPinError(false)
    try {
      const result = target ? await roomIndexApi.pin(item.room_id) : (await roomIndexApi.unpin(item.room_id), null)
      if (!sameOwner() || !mounted.current || !opened.current || session !== dialogSession.current) return
      setItems((old) => old.map((row) => row.room_id === item.room_id ? { ...row, pinned: target, pinned_at: result?.pinned_at || null } : row).sort(canonicalOrder))
      setNext(null); setRefresh((value) => value + 1)
      requestAnimationFrame(() => {
        if (!mounted.current || !opened.current || session !== dialogSession.current || !sameOwner()
            || document.activeElement !== document.body || !(focused instanceof HTMLElement)) return
        if (focused.isConnected && dialog.current?.contains(focused) && !focused.matches(':disabled')) focused.focus()
        else if (!focused.isConnected) input.current?.focus()
      })
    } catch { if (sameOwner() && mounted.current && opened.current && session === dialogSession.current) setPinError(true) }
    finally { if (sameOwner()) { pinningRef.current.delete(item.room_id); if (mounted.current) { setPinning((old) => old.filter((id) => id !== item.room_id)); if (opened.current && session !== dialogSession.current) { setNext(null); setRefresh((value) => value + 1) } } } }
  }
  return <><button type="button" className="icon-button quick-switch-trigger" aria-label={t('switcher.open')} aria-keyshortcuts="Meta+K Control+K" title={`${t('switcher.open')} · ⌘/Ctrl+K`} onClick={show}>
    <Search size={18}/><span className="quick-switch-shortcut" aria-hidden="true">⌘/Ctrl K</span>
  </button>
    {open && createPortal(<div className="modal-backdrop quick-switch-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) close() }}>
      <section ref={dialog} role="dialog" aria-modal="true" aria-label={t('switcher.reference.title')} className="quick-switch" onKeyDown={(event) => {
        if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229 || composingRef.current || performance.now() < imeNavigationAfter.current) return
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); if (!event.repeat) close() }
        if (event.key !== 'Tab') return
        const controls = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), pre[tabindex="0"]') || [])].filter(element => element.getClientRects().length > 0)
        if (!controls.length) return
        if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1)?.focus() }
        else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0].focus() }
      }}>
        <header><label className="field"><span className="sr-only">{t(mode === 'drafts' ? 'switcher.drafts.search' : mode === 'recent' ? 'switcher.recent.search' : 'switcher.reference.search')}</span><input ref={input} type="search" role={storedMode ? 'searchbox' : 'combobox'} aria-expanded={storedMode ? undefined : !referenceMode || resultLink.state === 'valid'} aria-controls={storedMode ? 'quick-stored-draft-list' : referenceMode && resultLink.state !== 'valid' ? undefined : 'quick-room-list'}
          aria-activedescendant={resultLink.state === 'valid' ? 'quick-result-link' : mode === 'drafts' && draftActive ? `composer-draft-${draftActive.room}` : mode === 'recent' && recentActive ? recentDOM(recentActive) : visible[active] ? `quick-room-${visible[active].room_id}` : undefined} maxLength={2048} placeholder={t(mode === 'drafts' ? 'switcher.drafts.search' : mode === 'recent' ? 'switcher.recent.search' : 'switcher.reference.search')} value={query}
          onChange={(event) => setQuery(event.target.value)} onCompositionStart={() => { composingRef.current = true; setComposing(true) }}
          onCompositionEnd={(event) => { imeNavigationAfter.current = performance.now() + 100; composingRef.current = false; setComposing(false); setQuery(event.currentTarget.value) }} onKeyDown={(event) => {
            if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229 || composingRef.current || performance.now() < imeNavigationAfter.current) return
            if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); if (!event.repeat) close() }
            else if (event.key === 'ArrowDown') { event.preventDefault(); if (mode === 'drafts' && !referenceMode) moveDraft(1); else if (mode === 'recent' && !referenceMode) moveRecent(1); else move(1) }
            else if (event.key === 'ArrowUp') { event.preventDefault(); if (mode === 'drafts' && !referenceMode) moveDraft(-1); else if (mode === 'recent' && !referenceMode) moveRecent(-1); else move(-1) }
            else if (event.key === 'Enter' && resultLink.state === 'valid') { event.preventDefault(); openResultLink() }
            else if (event.key === 'Enter' && storedMode) { event.preventDefault(); storedSearch.current?.search() }
            else if (event.key === 'Enter' && mode === 'drafts' && draftActive) { event.preventDefault(); openDraft(draftActive.room) }
            else if (event.key === 'Enter' && mode === 'recent' && recentActive) { event.preventDefault(); openRecent(recentActive) }
            else if (event.key === 'Enter' && visible[active]) { event.preventDefault(); choose(visible[active]) }
          }} /></label><button type="button" className="icon-button" aria-label={t('common.close')} onClick={() => close()}><X size={18}/></button></header>
        <div className="quick-switch-content">
        <p className="field-hint">{t('switcher.reference.help')}</p>
        <div className="page-actions"><button type="button" className="button button--ghost button--compact" aria-pressed={mode === 'rooms'} onClick={() => { if (sameOwner()) { setMode('rooms'); input.current?.focus() } }}>{t('switcher.recent.rooms')}</button><button type="button" className="button button--ghost button--compact" aria-pressed={mode === 'recent'} onClick={() => { if (sameOwner()) { setMode('recent'); input.current?.focus() } }}>{t('switcher.recent.title')}</button><button type="button" className="button button--ghost button--compact" aria-pressed={mode === 'drafts'} onClick={() => { if (sameOwner()) { setMode('drafts'); input.current?.focus() } }}>{t('switcher.drafts.title')}</button></div>
        {referenceMode ? <div className="stack stack--tight">
          {resultLink.state === 'valid' ? <><p className="metadata">{t('switcher.reference.unchecked')}</p><ul id="quick-room-list" role="listbox" className="quick-switch-list"><li role="none"><button id="quick-result-link" type="button" role="option" aria-selected="true" className="quick-switch-row is-active" onClick={openResultLink}><span><strong>{resultLink.kind === 'event' ? t('switcher.reference.eventTarget', { room: resultLink.room, seq: resultLink.seq }) : t('switcher.reference.target', { room: resultLink.room, version: resultLink.version })}</strong>{resultLink.kind === 'result' && <small>{resultLink.id}{resultLink.base !== null && <> · {t('switcher.reference.base', { version: resultLink.base })}</>}</small>}</span></button></li></ul></> : <p role="alert" className="notice notice--warning">{t(searchTooLong ? 'switcher.reference.tooLong' : 'switcher.reference.invalid')}</p>}
          <button type="button" className="button button--ghost button--compact" onClick={() => { if (sameOwner() && opened.current) { setQuery(''); input.current?.focus() } }}>{t('directory.clear')}</button>
        </div> : mode === 'drafts' ? <div className="stack stack--tight">
          <div className="page-actions" role="group" aria-label={t('switcher.draftSources.choose')}>
            <button type="button" className="button button--ghost button--compact" aria-pressed={draftSource==='local'} onClick={()=>{if(draftCurrent()){setDraftSource('local');input.current?.focus()}}}>{t('switcher.draftSources.local',{count:allDraftRows.length})}</button>
            <button type="button" className="button button--ghost button--compact" aria-pressed={draftSource==='stored'} onClick={()=>{if(draftCurrent()){setDraftSource('stored');input.current?.focus()}}}>{t('switcher.draftSources.stored')}</button>
          </div>
          <div hidden={draftSource!=='local'} className="stack stack--tight"><p className="field-hint">{t('switcher.drafts.scope')}</p><p className="metadata">{t('switcher.drafts.sendLimit')}</p>
          {localRead && localRead.identity===identity && localRead.generation===recentGeneration && <div ref={localReveal.target} tabIndex={-1} onKeyDown={event=>{if(event.target!==event.currentTarget||event.key!=='Escape')return;event.stopPropagation();if(event.nativeEvent.isComposing||event.nativeEvent.keyCode===229)return;event.preventDefault();closeLocalReader()}} aria-label={t('switcher.localReader.read')}><StoredDraftReader key={localRead.draft.readId} focusScope={localReveal.target} value={localRead.draft} initialQuery={query.trim().slice(0,200)} canUse={localReaderCurrent} local={{at:localRead.at,state:localReadState,refresh:()=>readLocalDraft(localRead.draft.room)}} onBack={localReveal.back} onOpen={openLocalReadRoom} onClose={closeLocalReader}/></div>}
          {!draftRows.length && <p role="status">{t('switcher.drafts.empty')}</p>}
          <ul id="quick-room-list" role="listbox" className="quick-switch-list">{draftRows.map((row,index) => <li role="none" key={row.room}><button id={`composer-draft-${row.room}`} type="button" role="option" aria-selected={index === draftIndex} className={index === draftIndex ? 'quick-switch-row is-active' : 'quick-switch-row'} onMouseEnter={() => { if (draftCurrent()) setDraftSelection(row.room) }} onFocus={() => { if (draftCurrent()) setDraftSelection(row.room) }} onClick={() => openDraft(row.room)}><span><strong>{draftRoomName(identity,row.room) || t('chat.roomTitle',{id:String(row.room)})} · #{row.room}</strong><small>{row.preview}</small>{row.source==='local' && query.trim() && !directoryMatcher(query)([draftRoomName(identity,row.room),String(row.room),`#${row.room}`,row.preview]) && <small>{t('switcher.localReader.fullMatch')}</small>}<small>{t(row.source === 'local' ? 'switcher.drafts.local' : 'switcher.drafts.received', { version: row.version, count: row.chars })} · {formatDateTime(row.observedAt,{dateStyle:'medium',timeStyle:'short'})}</small></span></button>{row.source==='local' && <div className="page-actions"><button type="button" className="button button--ghost button--compact" aria-label={t('switcher.localReader.named',{room:draftRoomName(identity,row.room)||String(row.room)})} onClick={event=>readLocalDraft(row.room,event.currentTarget)}>{t('switcher.localReader.read')}</button>{localRead?.draft.room===row.room&&<button type="button" className="text-link" onClick={()=>localReveal.jump()}>{t('switcher.readerNav.view')}</button>}</div>}</li>)}</ul>
          <p className="field-hint">{t('switcher.localReader.searchScope')}</p>

          </div>
          <div hidden={draftSource!=='stored'}><p className="field-hint">{t('switcher.draftSources.enter')}</p><StoredDraftSearch ref={storedSearch} identity={identity} query={query} canUse={storedCurrent} onOpen={openStoredDraft}/></div>
        </div> : mode === 'recent' ? <div className="stack stack--tight"><p className="field-hint">{t('switcher.recent.scope')}</p><button type="button" className="button button--ghost button--compact" disabled={!recent.length} onClick={() => forgetRecent()}>{t('switcher.recent.clear')}</button>
          {undoRecentCount > 0 && <div className="notice notice--info stack stack--tight">
            <p role="status">{t('switcher.recent.undoHelp')}</p>
            <div><button type="button" className="button button--secondary button--compact" onClick={() => {
              if (!recentCurrent()) return
              restoreForgottenRecentResults(identity, recentGeneration)
              // Undo removes this button; keep keyboard navigation in the search.
              input.current?.focus({ preventScroll: true })
            }}>{t('switcher.recent.undo', { count: undoRecentCount })}</button></div>
          </div>}
          {!recentRows.length && <p role="status">{t(recent.length ? 'switcher.empty' : 'switcher.recent.empty')}</p>}
          <ul id="quick-room-list" role="listbox" className="quick-switch-list">{recentRows.map((item, index) => <li role="none" key={item.key}><button id={recentDOM(item)} type="button" role="option" aria-selected={index === recentIndex} className={index === recentIndex ? 'quick-switch-row is-active' : 'quick-switch-row'} onMouseEnter={() => { if (recentCurrent()) setRecentSelection(item.key) }} onFocus={() => { if (recentCurrent()) setRecentSelection(item.key) }} onClick={() => openRecent(item)}><span><strong>{item.title} · v{item.version}</strong><small>{t('switcher.reference.target', { room: item.room, version: item.version })}{item.base !== null && <> · {t('switcher.reference.base', { version: item.base })}</>}</small><small>{formatDateTime(item.openedAt, { dateStyle: 'medium', timeStyle: 'short' })}</small></span></button><button type="button" className="button button--ghost button--compact" onClick={() => forgetRecent(item.key)} aria-label={t('switcher.recent.removeNamed', { title: item.title })}>{t('switcher.recent.remove')}</button></li>)}</ul>
        </div> : <>
        <div className="quick-switch-filters"><select aria-label={t('switcher.status')} value={status} onChange={(event) => setStatus(event.target.value as RoomIndexStatus)}>
          <option value="active">{t('switcher.active')}</option><option value="closed">{t('switcher.closed')}</option><option value="all">{t('switcher.all')}</option></select>
          <select aria-label={t('switcher.project')} value={projectId} onChange={(event) => setProjectId(Number(event.target.value))}><option value="0">{t('switcher.allProjects')}</option>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select>
          <button type="button" className="button button--ghost button--compact" aria-pressed={pinnedOnly} onClick={() => { setPinnedOnly((value) => !value); if (!pinnedOnly) setStatus('all') }}><Pin size={14}/>{t('switcher.pinned')}</button>
        </div>
        {projectsLoading && !projects.length && <p role="status">{t('common.loading')}</p>}
        {projectsError && <div className="notice notice--warning stack stack--tight"><p role="alert">{t('switcher.projectsFailure')}{projects.length > 0 && <> {t('common.listSnapshot')}</>}</p>
          <button type="button" className="button button--secondary button--compact" disabled={projectsLoading} onClick={() => setProjectRefresh((value) => value + 1)}>{t('common.retry')}</button></div>}
        {pinError && <div className="notice notice--warning stack stack--tight"><p role="alert">{t('switcher.pinFailure')}</p>
          <button type="button" className="button button--secondary button--compact" disabled={loading || pendingSearch} onClick={() => setRefresh((value) => value + 1)}>{t('common.retry')}</button></div>}
        {loadFailure && !pendingSearch && <div className="notice notice--warning stack stack--tight"><p role="alert">{t('switcher.listFailure')}{resultsCurrent && items.length > 0 && <> {t('common.listSnapshot')}</>}</p>
          <button type="button" className="button button--secondary button--compact" disabled={loading} onClick={() => { if (loadFailure === 'more') void loadMore(); else setRefresh((value) => value + 1) }}>{t('common.retry')}</button></div>}
        {(loading || pendingSearch) && <p role="status">{t('common.loading')}</p>}
        {!loading && !pendingSearch && !loadFailure && !visible.length && <p>{t('switcher.empty')}</p>}
        <ul id="quick-room-list" role="listbox" aria-busy={loading || pendingSearch} className="quick-switch-list">{visible.map((item, index) => <li role="none" key={item.room_id}>
          <button type="button" id={`quick-room-${item.room_id}`} role="option" aria-selected={index === active} className={index === active ? 'quick-switch-row is-active' : 'quick-switch-row'} onMouseEnter={() => remember(index)} onFocus={() => remember(index)} onClick={() => choose(item)}>
            <span><strong>{item.room_title}</strong><small>{item.project_name}{item.read_only ? ` · ${t('switcher.readOnly')}` : ''}</small></span>
            <span className="quick-switch-row__signals">{item.unread_authored > 0 && <small>{t('switcher.unread', { count: item.unread_authored })}</small>}{item.attention_count > 0 && <small>{t('switcher.attention', { count: item.attention_count })}</small>}{item.work_status !== 'idle' && <small>{t(`switcher.${item.work_status}`)}</small>}</span>
          </button><button type="button" className="icon-button" disabled={pinning.includes(item.room_id)} onFocus={() => remember(index)} aria-label={t(item.pinned ? 'switcher.unpin' : 'switcher.pin', { room: item.room_title })} onClick={() => void pin(item)}><Pin size={15} fill={item.pinned ? 'currentColor' : 'none'}/></button>
        </li>)}</ul>
        {resultsCurrent && next && loadFailure !== 'more' && <button type="button" className="button button--secondary" disabled={loading} onClick={() => void loadMore()}>{t('switcher.more')}</button>}
        </>}
        </div>
      </section>
    </div>, document.body)}</>
}
