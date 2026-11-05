import { useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useInfiniteQuery } from '@tanstack/react-query'
import { Activity, X } from 'lucide-react'
import { Link } from 'react-router-dom'
import { agentWorkloadsApi, type WorkloadState } from '../api/agentWorkloads'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { AgentCharacter } from './AgentCharacter'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import { useDirectoryValue } from '../lib/useDirectoryView'
import { useDialogFocus } from '../lib/useDialogFocus'
import { directoryViewGeneration } from '../lib/directoryViewMemory'

const states: WorkloadState[] = ['running', 'waiting', 'paused', 'failed', 'unknown', 'idle']

export default function AgentWorkloads() {
  const identity = useAuthStore((value) => `${value.user?.id || 0}:${value.epoch}`)
  return <WorkloadBrowser key={`${identity}:${directoryViewGeneration()}`} identity={identity}/>
}
function WorkloadDialog({ onClose, label, children }: { onClose: () => void; label: string; children: ReactNode }) {
  const dialog = useRef<HTMLElement>(null)
  useDialogFocus(dialog, onClose)
  return <div className="modal-backdrop workloads-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
    <section ref={dialog} role="dialog" aria-modal="true" aria-label={label} className="workloads-dialog">{children}</section>
  </div>
}
function WorkloadBrowser({ identity }: { identity: string }) {
  const [open, setOpen] = useState(false)
  const [pageAnchor, setPageAnchor] = useState<number | null>(null)
  const firstArticle = useRef<HTMLElement>(null), pageFocus = useRef<{ id: number; origin: HTMLButtonElement } | null>(null)
  const resultId = useId()
  const [search, setSearch] = useDirectoryValue<string>(identity, 'workloads.search', '')
  const [savedState, setStateFilter] = useDirectoryValue<string>(identity, 'workloads.state', 'all')
  const stateFilter = states.find(state => state === savedState) || 'all'
  const generation = useRef(directoryViewGeneration()).current
  const { t, formatDateTime } = useI18n()
  const sameOwner = () => {
    const auth = useAuthStore.getState()
    return !!auth.token && identity === `${auth.user?.id || 0}:${auth.epoch}` && generation === directoryViewGeneration()
  }
  const close = () => { if (sameOwner()) { pageFocus.current = null; setOpen(false) } }
  const activity = useInfiniteQuery({
    queryKey: ['workload-browser', identity], enabled: open, initialPageParam: undefined as string | undefined,
    retry: false, staleTime: Infinity, refetchOnWindowFocus: false, refetchOnReconnect: false,
    queryFn: async ({ pageParam }) => {
      if (!sameOwner()) throw new Error('Owner changed')
      const page = await agentWorkloadsApi.list(pageParam)
      if (!sameOwner()) throw new Error('Owner changed')
      return page
    },
    getNextPageParam: page => page.next_cursor || undefined,
  })
  const items = useMemo(() => {
    const rows = new Map<number, NonNullable<typeof activity.data>['pages'][number]['items'][number]>()
    for (const page of activity.data?.pages || []) for (const item of page.items) rows.set(item.agent_id, item)
    return [...rows.values()]
  }, [activity.data])
  const visible = useMemo(() => {
    const matches = directoryMatcher(search)
    return items.flatMap(item => {
      const agentFields = [String(item.agent_id), `#${item.agent_id}`, item.agent_name]
      const agentMatch = matches(agentFields)
      const rooms = item.rooms.filter(room => (stateFilter === 'all' || room.state === stateFilter)
        && (agentMatch || matches([...agentFields, String(room.room_id), `#${room.room_id}`, room.room_title, String(room.project_id), `#${room.project_id}`, room.project_name])))
      const summaryMatch = stateFilter === 'all' || item.overall_state === stateFilter || item.counts[stateFilter] > 0
      return (agentMatch && summaryMatch) || rooms.length > 0 ? [{ item, rooms }] : []
    })
  }, [items, search, stateFilter])
  // Page only the received, filtered view; server cursors and summary counts stay unchanged.
  const pageStart = pageAnchor === null ? 0 : Math.max(0, visible.findIndex(row => row.item.agent_id === pageAnchor))
  const pageEnd = Math.min(pageStart + 12, visible.length)
  const pageItems = visible.slice(pageStart, pageEnd)
  const resetPage = () => { pageFocus.current = null; setPageAnchor(null) }
  const movePage = (index: number, origin: HTMLButtonElement) => {
    if (!open || !sameOwner() || !visible.length || document.visibilityState !== 'visible' || !document.hasFocus() || !origin.isConnected) return
    const target = visible[Math.max(0, Math.min(index, visible.length - 1))]
    if (target.item.agent_id === pageItems[0]?.item.agent_id) return
    origin.focus({ preventScroll: true })
    pageFocus.current = { id: target.item.agent_id, origin }; setPageAnchor(target.item.agent_id)
  }
  useLayoutEffect(() => {
    const intent = pageFocus.current; pageFocus.current = null
    if (!intent || !open || !sameOwner() || intent.id !== pageItems[0]?.item.agent_id
      || document.visibilityState !== 'visible' || !document.hasFocus() || document.activeElement !== intent.origin && document.activeElement !== document.body) return
    const article = firstArticle.current
    if (!article?.isConnected || !article.getClientRects().length || article.closest('[hidden], [inert], [aria-hidden="true"]')) return
    // A reading target cannot open a room when the paging Enter key repeats.
    article.focus({ preventScroll: true }); article.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [pageAnchor, pageItems[0]?.item.agent_id, open])
  const pager = () => visible.length > 12 && <nav className="page-actions" aria-label={t('workloads.browse.pages')}>
    <button type="button" className="text-link" disabled={pageStart === 0} onClick={event => movePage(0, event.currentTarget)}>{t('attention.pages.first')}</button>
    <button type="button" className="text-link" disabled={pageStart === 0} onClick={event => movePage(Math.max(0, pageStart - 12), event.currentTarget)}>{t('attention.pages.previous')}</button>
    <button type="button" className="text-link" disabled={pageEnd >= visible.length} onClick={event => movePage(pageStart + 12, event.currentTarget)}>{t('attention.pages.next')}</button>
    <button type="button" className="text-link" disabled={pageEnd >= visible.length} onClick={event => movePage(Math.floor((visible.length - 1) / 12) * 12, event.currentTarget)}>{t('attention.pages.last')}</button>
  </nav>
  const refresh = () => { if (sameOwner() && !activity.isFetching) void activity.refetch({ cancelRefetch: false }) }
  const more = () => { if (sameOwner() && !activity.isFetching && activity.hasNextPage) void activity.fetchNextPage({ cancelRefetch: false }) }
  return <><button type="button" className="button button--secondary" onClick={event => { if (sameOwner()) { event.currentTarget.focus({ preventScroll: true }); setOpen(true) } }}><Activity size={18}/>{t('workloads.open')}</button>
    {open && <WorkloadDialog onClose={close} label={t('workloads.title')}>
      <header><div><h2>{t('workloads.title')}</h2><p>{t('workloads.help')}</p></div><button type="button" className="icon-button" aria-label={t('common.close')} onClick={close}><X size={18}/></button></header>
      <div className="workloads-dialog__body stack">
        <div className="stack stack--tight">
          <button type="button" className="button button--secondary" disabled={activity.isFetching} onClick={refresh}>{t('workloads.browse.refresh')}</button>
          {activity.dataUpdatedAt > 0 && <p className="metadata">{t('workloads.browse.lastReceived', { time: formatDateTime(new Date(activity.dataUpdatedAt).toISOString(), { dateStyle: 'medium', timeStyle: 'short' }) })}</p>}
          <p className="field-hint">{t('workloads.browse.scope')}</p>
          {(activity.isFetching || activity.isError) && activity.data && <p className="notice notice--info">{t('workloads.browse.retained')}</p>}
          {activity.isError && <p role="alert" className="notice notice--warning">{t(activity.isFetchNextPageError ? 'workloads.browse.moreFailure' : 'workloads.browse.refreshFailure')}</p>}
          <DirectorySearch initialFocus value={search} onChange={value => { if (sameOwner()) { resetPage(); setSearch(value) } }} label={t('workloads.browse.searchLabel')} hint={t('workloads.browse.searchHint')}
            shown={visible.length} total={items.length} loading={activity.isFetching} known={activity.data !== undefined} controls={resultId}/>
          <label className="field">{t('workloads.browse.stateFilter')}
            <select value={stateFilter} aria-controls={resultId} onChange={event => { if (sameOwner()) { resetPage(); setStateFilter(event.target.value) } }}>
              <option value="all">{t('workloads.browse.stateAll')}</option>
              {states.map(state => <option key={state} value={state}>{t(`workloads.state.${state}`)}</option>)}
            </select>
          </label>
          <p className="field-hint">{t('workloads.browse.matchHelp')}</p>
          {(search.trim() || stateFilter !== 'all') && <button type="button" className="button button--ghost button--compact" onClick={() => { if (sameOwner()) { resetPage(); setSearch(''); setStateFilter('all') } }}>{t('workloads.browse.resetFilters')}</button>}
        </div>
        {activity.isPending ? <p role="status">{t('common.loading')}</p>
          : !activity.data ? null : !items.length ? <p className="section-copy">{t('workloads.empty')}</p>
            : !visible.length ? <p className="section-copy">{t('directory.noMatches')}</p> : null}
        {visible.length > 0 && <><p className="metadata" role="status">{t('workloads.browse.range', { from: pageStart + 1, to: pageEnd, total: visible.length })}</p><p className="field-hint">{t('workloads.browse.pageHelp')}</p></>}
        {pager()}
        <div id={resultId} className="workloads-list">{pageItems.map(({ item, rooms }, index) => <article key={item.agent_id} ref={index === 0 ? firstArticle : undefined} tabIndex={0} aria-labelledby={`${resultId}-${item.agent_id}`} className="workload-agent">
          <header><div><h3 id={`${resultId}-${item.agent_id}`} className="heading-with-icon"><AgentCharacter agentId={item.agent_id}/>{item.agent_name} · #{item.agent_id}</h3>{!item.agent_active && <small>{t('workloads.inactive')}</small>}</div><span className={`workload-state workload-state--${item.overall_state}`}>{t(`workloads.state.${item.overall_state}`)}</span></header>
          <div className="workload-counts" aria-label={t('workloads.counts', { agent: item.agent_name })}>{states.map(state => item.counts[state] > 0 && <span key={state}>{t(`workloads.count.${state}`, { count: item.counts[state] })}</span>)}</div>
          {rooms.length < item.rooms.length && <p className="metadata">{t('workloads.browse.matchedRooms', { shown: rooms.length, total: item.rooms.length })}</p>}
          {rooms.length ? <ul>{rooms.map(room => <li key={room.room_id}><Link to={room.href} className="workload-room" onClick={event => {
            if (!sameOwner()) { event.preventDefault(); return }
            if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) setOpen(false)
          }}>
            <span><strong>{room.room_title} · #{room.room_id}</strong><small>{room.project_name} · #{room.project_id}</small></span><span><small>{t(`workloads.state.${room.state}`)}</small><time dateTime={room.last_activity_at}>{formatDateTime(room.last_activity_at, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</time></span>
          </Link></li>)}</ul> : <p className="section-copy">{t(stateFilter !== 'all' && (item.rooms.length > 0 || item.counts.total_rooms > 0) ? 'workloads.browse.stateNoRooms' : 'workloads.noRooms')}</p>}
          {item.rooms_truncated && <p className="metadata">{t(search.trim() || stateFilter !== 'all' ? 'workloads.browse.searchTruncated' : 'workloads.roomsTruncated', { count: item.counts.total_rooms })}</p>}
        </article>)}</div>
        {pager()}
        {activity.hasNextPage && <button type="button" className="button button--secondary" disabled={activity.isFetching} onClick={more}>{t(activity.isFetchNextPageError ? 'workloads.browse.retryMore' : 'workloads.more')}</button>}
      </div>
    </WorkloadDialog>}</>
}
