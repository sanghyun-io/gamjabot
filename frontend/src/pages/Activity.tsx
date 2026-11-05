import RoomStopNotice, { type RoomStopState } from '../components/RoomStopNotice'
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useIsMutating, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Settings2 } from 'lucide-react'
import type { Agent } from '../api/agents'
import { toolsApi } from '../api/tools'
import GrantPrompt from '../components/GrantPrompt'
import ToolActivityRecord, { toolActivitySearchFields } from '../components/ToolActivityRecord'
import DirectorySearch, { directoryMatcher } from '../components/DirectorySearch'
import { useDirectoryValue } from '../lib/useDirectoryView'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { editorSaveFailure } from '../components/EditorSaveNotice'
import { broadModeKey, desktopLeaseKey, readBroadModeForOwner, readOwnerToolData, toolActivityKey, toolGrantsKey, toolOwnerCurrent, useBroadModeAction } from '../lib/useBroadModeAction'

interface Props { roomId: number; roomName: string; agents: Agent[]; onStop: () => void; stopPending?: boolean; stopState?: RoomStopState; onInspect?: (id: string) => void }
export default function Activity(props: Props) {
  const owner = useAuthStore(state => state.user?.id ?? 0), epoch = useAuthStore(state => state.epoch)
  return <ActivityView key={`${owner}:${epoch}:${props.roomId}`} {...props} owner={owner} epoch={epoch} />
}
function ActivityView({ roomId, roomName, agents, onStop, stopPending, stopState, onInspect, owner, epoch }: Props & { owner: number; epoch: number }) {
  const cache = useQueryClient()
  const [minutes, setMinutes] = useState('15')
  const trimmed = minutes.trim()
  const duration = /^\d+$/.test(trimmed) ? Number(trimmed) : NaN
  const validMinutes = Number.isSafeInteger(duration) && duration >= 1 && duration <= 60
  const [error, setError] = useState<'revoke' | 'unknown' | ''>('')
  const [revokeTarget, setRevokeTarget] = useState<number | null>(null)
  const [revoked, setRevoked] = useState<number | null>(null)
  const alive = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const current = () => toolOwnerCurrent(owner, epoch)
  const broadAction = useBroadModeAction(owner, epoch)
  const revokeKey = ['grant-revoke', roomId, owner, epoch] as const
  const revokeBusy = useIsMutating({ mutationKey: revokeKey, exact: true }) > 0
  const { t, formatDateTime } = useI18n()
  const [grantQuery, setGrantQuery] = useDirectoryValue<string>(`${owner}:${epoch}`, `activity:grants:${roomId}`, '')
  const grantListId = useId()
  const [historyQuery, setHistoryQuery] = useDirectoryValue<string>(`${owner}:${epoch}`, `activity:history:${roomId}`, '')
  const [historyAnchor, setHistoryAnchor] = useState<string | null>(null), [anchorLost, setAnchorLost] = useState(false)
  const settingsRoot = useRef<HTMLDetailsElement>(null), firstHistoryRow = useRef<HTMLLIElement>(null), reviewListId = useId()
  const historyIntent = useRef<{ anchor: string; query: string; origin: HTMLButtonElement } | null>(null)
  const changeHistoryQuery = (value: string) => {
    if (!current()) return
    historyIntent.current = null; setHistoryAnchor(null); setAnchorLost(false); setHistoryQuery(value)
  }
  const activity = useQuery({ queryKey: toolActivityKey(roomId, owner, epoch), retry: false,
    queryFn: () => readOwnerToolData(owner, epoch, async () => {
      const rows = await toolsApi.activity(roomId)
      if (rows.some(row => row.room_id !== roomId)) throw new Error('Activity room changed')
      return rows
    }), refetchInterval: 2000 })
  const grants = useQuery({ queryKey: toolGrantsKey(roomId, owner, epoch), retry: false,
    queryFn: () => readOwnerToolData(owner, epoch, async () => {
      const rows = await toolsApi.grants(roomId)
      if (rows.some(row => row.room_id !== roomId)) throw new Error('Grant room changed')
      return rows
    }), refetchInterval: 3000 })
  const broad = useQuery({ queryKey: broadModeKey(owner, epoch), queryFn: () => readBroadModeForOwner(owner, epoch), retry: false, refetchInterval: 3000 })
  const lease = useQuery({ queryKey: desktopLeaseKey(owner, epoch), queryFn: () => readOwnerToolData(owner, epoch, async () => { const value = await toolsApi.lease(); if (typeof value?.active !== 'boolean') throw new Error('Desktop status unavailable'); return value }), retry: false, refetchInterval: 3000 })
  const refresh = useCallback(() => {
    if (!toolOwnerCurrent(owner, epoch)) return
    void cache.invalidateQueries({ queryKey: toolActivityKey(roomId, owner, epoch), exact: true })
    void cache.invalidateQueries({ queryKey: toolGrantsKey(roomId, owner, epoch), exact: true })
    void cache.invalidateQueries({ queryKey: desktopLeaseKey(owner, epoch), exact: true })
  }, [cache, roomId, owner, epoch])
  const revoke = useMutation({ mutationKey: revokeKey, retry: false,
    mutationFn: async (id: number) => {
      if (!current()) throw new Error('Grant Owner changed')
      await toolsApi.revoke(roomId, id)
    },
    onSuccess: (_, id) => { if (!current()) return; refresh(); if (alive.current) { setError(''); setRevoked(id) } },
    onError: failure => { if (alive.current && current()) setError(editorSaveFailure(failure) === 'unknown' ? 'unknown' : 'revoke') },
  })
  const revokeGrant = (id: number) => {
    if (!current() || !Number.isSafeInteger(id) || id < 1 || !grants.data?.some(grant => grant.id === id && grant.room_id === roomId)
      || cache.getMutationCache().find({ mutationKey: revokeKey, exact: true, status: 'pending' })) return
    setRevokeTarget(id); setRevoked(null); setError(''); revoke.mutate(id)
  }
  const allowEnable = broad.isSuccess && !broad.isFetching
  const requestLabel = (mode: 'off' | 'timed' | 'always', value?: number) => mode === 'off' ? t('activity.broadOff')
    : mode === 'always' ? t('activity.enablePermanent') : t('activity.enableTimed', { count: value ?? 0 })
  const name = (id: number) => agents.find((agent) => agent.id === id)?.name || t('members.fallbackName', { id })
  const pending = activity.isSuccess ? activity.data.filter(item => item.state === 'waiting') : []
  const approvedGrants = useMemo(() => grants.data?.filter(grant => grant.decision === 'approved') || [], [grants.data])
  const grantSearch = useMemo(() => {
    const matches = directoryMatcher(grantQuery), names = new Map(agents.map(agent => [agent.id, agent.name]))
    const matching = new Set(approvedGrants.filter(grant => matches([
      names.get(grant.agent_id) || t('members.fallbackName', { id: grant.agent_id }),
      String(grant.id), `#${grant.id}`, String(grant.agent_id), `#${grant.agent_id}`,
      grant.scope, ...grant.families, ...grant.targets,
    ], grant.id)).map(grant => grant.id))
    const visible = approvedGrants.filter(grant => matching.has(grant.id)
      || grant.id === revokeTarget && (revokeBusy || !!error))
    return { visible, matching: matching.size, pinned: visible.filter(grant => !matching.has(grant.id)).length }
  }, [approvedGrants, agents, grantQuery, revokeTarget, revokeBusy, error, t])
  const history = useMemo(() => {
    const rows = (activity.data || []).slice().reverse(), matches = directoryMatcher(historyQuery)
    const names = new Map(agents.map(agent => [agent.id, agent.name]))
    const matched = new Set(rows.filter(item => matches(toolActivitySearchFields(item, names.get(item.agent_id) || t('members.fallbackName', { id: item.agent_id }), t))).map(item => item.action_id))
    // Uncertain and unknown states stay visible even though they may be terminal for dispatch.
    const settled = new Set(['completed', 'failed', 'denied', 'cancelled'])
    const visible = rows.filter(item => matched.has(item.action_id) || !settled.has(item.state))
    return { review: visible.filter(item => !settled.has(item.state)), settled: visible.filter(item => settled.has(item.state)), matching: matched.size, total: rows.length, pinned: visible.length - matched.size }
  }, [activity.data, agents, historyQuery, t])

  const historyStart = historyAnchor ? Math.max(0, history.settled.findIndex(item => item.action_id === historyAnchor)) : 0
  const historyRows = history.settled.slice(historyStart, historyStart + 12)
  // Polling may add newer rows, but an explicitly chosen segment stays anchored
  // to its first received ID. Missing anchors reset, never revive on later polls.
  useEffect(() => {
    if (historyAnchor && !history.settled.some(item => item.action_id === historyAnchor)) {
      historyIntent.current = null; setHistoryAnchor(null); setAnchorLost(true)
    }
  }, [historyAnchor, history.settled])
  useLayoutEffect(() => {
    const intent = historyIntent.current; historyIntent.current = null
    const root = settingsRoot.current, target = firstHistoryRow.current
    if (!intent || intent.anchor !== historyAnchor || intent.query !== historyQuery || !alive.current || !current()
      || !root?.open || !root.isConnected || root.closest('[hidden],[inert],[aria-hidden="true"]')
      || document.visibilityState !== 'visible' || !document.hasFocus() || !target?.isConnected || !target.getClientRects().length
      || (document.activeElement !== intent.origin && document.activeElement !== document.body)) return
    target.focus({ preventScroll: true }); target.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [historyAnchor, historyQuery, history.settled])
  const moveHistory = (index: number, origin: HTMLButtonElement) => {
    const root = settingsRoot.current
    if (!alive.current || !current() || !root?.open || !root.isConnected || !root.getClientRects().length
      || root.closest('[hidden],[inert],[aria-hidden="true"]') || document.visibilityState !== 'visible'
      || !document.hasFocus() || !Number.isSafeInteger(index)) return
    const target = history.settled[Math.max(0, Math.min(index, history.settled.length - 1))]
    if (!target) return
    setAnchorLost(false)
    if (target.action_id === historyAnchor) {
      if (firstHistoryRow.current?.isConnected && firstHistoryRow.current.getClientRects().length
        && !firstHistoryRow.current.closest('[hidden],[inert],[aria-hidden="true"]')
        && (document.activeElement === origin || document.activeElement === document.body)) {
        firstHistoryRow.current.focus({ preventScroll: true }); firstHistoryRow.current.scrollIntoView({ block: 'nearest', inline: 'nearest' })
      }
      return
    }
    historyIntent.current = { anchor: target.action_id, query: historyQuery, origin }; setHistoryAnchor(target.action_id)
  }
  const historyPager = (history.settled.length > 12 || historyStart > 0) && <nav className="page-actions" aria-label={t('activity.historyPages.navigation')}>
    <button type="button" className="button button--secondary button--compact" aria-controls={`tool-history-${roomId}`} disabled={historyStart === 0} onClick={event => moveHistory(0, event.currentTarget)}>{t('attention.pages.first')}</button>
    <button type="button" className="button button--secondary button--compact" aria-controls={`tool-history-${roomId}`} disabled={historyStart === 0} onClick={event => moveHistory(historyStart - 12, event.currentTarget)}>{t('attention.pages.previous')}</button>
    <button type="button" className="button button--secondary button--compact" aria-controls={`tool-history-${roomId}`} disabled={historyStart + 12 >= history.settled.length} onClick={event => moveHistory(historyStart + 12, event.currentTarget)}>{t('attention.pages.next')}</button>
    <button type="button" className="button button--secondary button--compact" aria-controls={`tool-history-${roomId}`} disabled={historyStart + 12 >= history.settled.length} onClick={event => moveHistory(Math.max(0, history.settled.length - 12), event.currentTarget)}>{t('attention.pages.last')}</button>
  </nav>

  return <div aria-label={t('activity.region')}>
      <RoomStopNotice state={stopState || null}/>
    <details ref={settingsRoot} className="rail-section compact-details room-settings" onToggle={event => {
      if (event.target === event.currentTarget && !event.currentTarget.open) historyIntent.current = null
    }}>
      <summary><Settings2 size={16} aria-hidden="true" />{t('activity.title')}</summary>
      <div className="room-settings__body stack">
        <section className="stack stack--tight"><h2 className="section-title">{t('activity.desktop')}</h2>
          {lease.isPending && <p role="status">{t('common.loading')}</p>}
          <button type="button" className="text-link" disabled={lease.isFetching} onClick={() => { void lease.refetch({ cancelRefetch: false }) }}>{t('activity.retryDesktop')}</button>
          {lease.isError && <p role="alert">{t('activity.desktopUnknown')}</p>}
          {lease.isSuccess && lease.data && <>{lease.data.active ? lease.data.owner ? <>
            <p className="section-copy">{t('activity.desktopUsedBy', { name: name(lease.data.owner.agent_id) })}</p>
            <details className="compact-details"><summary>{t('common.technicalDetails')}</summary><p className="technical">{t('activity.roomTask', { room: String(lease.data.owner.room_id), action: lease.data.owner.workflow_id })}</p></details>
          </> : <p className="section-copy">{t('activity.desktopPrivate')}</p> :
            <p className="section-copy">{t('activity.desktopAvailable')}</p>}</>}
        </section>
        <section className="stack stack--tight"><h2 className="section-title">{t('activity.broad')}</h2>
          <button type="button" className="text-link" disabled={broad.isFetching || broadAction.busy} onClick={() => { void broad.refetch({ cancelRefetch: false }) }}>{t('activity.retryBroad')}</button>
          {broad.isPending && <p role="status">{t('common.loading')}</p>}
          {broad.isError && <p role="alert">{t('activity.broadUnknown')}</p>}
          {broad.isSuccess && broad.data.enabled ? <>
            <p className="section-copy">{t('activity.broadOn')}</p>
            <p role="status" className="status-text status-text--error">{broad.data.expires_at ? t('activity.expires', { time: formatDateTime(broad.data.expires_at) }) : t('activity.enablePermanent')}</p>
          </> : broad.isSuccess && <>
            <p className="section-copy">{t('activity.broadWarning')}</p>
            <button type="button" aria-label={t('activity.enablePermanent')} className="button button--danger" disabled={broadAction.busy || !allowEnable} onClick={() => broadAction.send('always')}>{t('activity.enablePermanent')}</button>
            <details className="compact-details"><summary>{t('activity.timedOptions')}</summary>
              <label className="field" htmlFor="broad-minutes">{t('activity.minutes')}<input id="broad-minutes" type="text" inputMode="numeric" value={minutes} disabled={broadAction.busy} aria-invalid={!validMinutes} aria-describedby={!validMinutes ? 'broad-minutes-error' : undefined} onChange={event => setMinutes(event.target.value)} /></label>
              {!validMinutes && <p id="broad-minutes-error" role="status">{t('activity.minutesInvalid')}</p>}
              <button type="button" aria-label={validMinutes ? t('activity.enableTimed', { count: duration }) : t('activity.enableTimedPlain')} className="button button--secondary" disabled={broadAction.busy || !allowEnable || !validMinutes} onClick={() => broadAction.send('timed', duration)}>{validMinutes ? t('activity.enableTimed', { count: duration }) : t('activity.enableTimedPlain')}</button>
            </details>
          </>}
          {(broad.isPending || broad.isError || broad.data?.enabled || broadAction.unknown.length > 0) && <button type="button" aria-label={t('activity.broadOff')} className="button button--danger" disabled={broadAction.busy} onClick={() => broadAction.send('off')}>{t('activity.broadOff')}</button>}
          {broadAction.busy && <p role="status">{t('activity.changePending')}</p>}
          {broadAction.latest?.status === 'success' && broadAction.latest.request && <p role="status">{t('activity.changeAccepted')} · {requestLabel(broadAction.latest.request.mode, broadAction.latest.request.minutes)}</p>}
          {broadAction.latest?.status === 'error' && broadAction.latest.failure === 'rejected' && <p role="alert">{t('activity.broadFailure')}</p>}
          {broadAction.unknown.length > 0 && <details className="notice notice--warning" open><summary>{t('activity.unconfirmedRequests', { count: broadAction.unknown.length })}</summary>
            <p role="alert">{t('activity.changeUnconfirmed')}</p><p>{t('activity.readNotReceipt')}</p><ul>{broadAction.unknown.map(row => <li key={row.id}>{requestLabel(row.request!.mode, row.request!.minutes)}</li>)}</ul></details>}

        </section>
        {pending.length > 0 && <section className="stack stack--tight"><h2 className="section-title">{t('activity.pending')} · {t('activity.pendingCount', { count: pending.length })}</h2>
          {pending.map((action) => <GrantPrompt key={action.action_id} roomId={roomId} roomName={roomName} agentName={name(action.agent_id)} action={action} onRefresh={refresh} onStop={onStop} stopPending={stopPending} interactive={false} />)}
        </section>}
        <section className="stack stack--tight"><h2 className="section-title">{t('activity.grants')} · {grants.isSuccess ? t('activity.grantCount', { count: approvedGrants.length }) : t('activity.countUnknown')}</h2>
          <button type="button" className="text-link" disabled={grants.isFetching || revokeBusy} onClick={() => { void grants.refetch({ cancelRefetch: false }) }}>{t('activity.retryGrants')}</button>
          {grants.isPending && <p role="status">{t('common.loading')}</p>}
          {grants.isError && grants.data && <p className="metadata">{t('activity.lastReceived')}</p>}
          {grants.isError && <p role="alert">{t('activity.grantsFailure')}</p>}
          {grants.isSuccess && approvedGrants.length === 0 && <p className="section-copy">{t('activity.noGrants')}</p>}
          <DirectorySearch exactId exactIdTitle={t('activity.grantsFind.exactTitle')} exactIdHelp={t('activity.grantsFind.exactScope')} value={grantQuery} onChange={value => { if (current()) setGrantQuery(value) }}
            label={t('activity.grantsFind.title')} hint={t('activity.grantsFind.placeholder')}
            shown={grantSearch.matching} total={approvedGrants.length} known={grants.data !== undefined} loading={grants.isPending} controls={grantListId} preventSubmit/>
          <p className="field-hint">{t('activity.grantsFind.scope')}</p>
          {grantSearch.pinned > 0 && <p role="status" className="notice notice--info">{t('activity.grantsFind.pinned', { count: grantSearch.pinned })}</p>}
          {approvedGrants.length > 0 && grantQuery.trim() && grantSearch.matching === 0 && <p role="status">{t('directory.noMatches')}</p>}
          <div id={grantListId} className="stack stack--tight">
          {grantSearch.visible.map((grant) => <div key={grant.id} className="section-copy">
            <p className="metadata">{t('activity.grantsFind.reference', { id: grant.id, agentId: grant.agent_id })}</p>
            <p>{name(grant.agent_id)} · {grant.scope} · {grant.families.join(', ')} · {grant.targets.join(', ')}</p>
            {grant.expires_at && <p>{t('activity.expires', { time: formatDateTime(grant.expires_at) })}</p>}
            <button aria-label={`${t('activity.revoke')} #${grant.id} · ${name(grant.agent_id)}`} className="button button--ghost button--compact danger-link" type="button" disabled={revokeBusy} onClick={() => revokeGrant(grant.id)}>{t('activity.revoke')}</button>
          </div>)}
          </div>
        </section>
        <section className="stack stack--tight"><h2 className="section-title">{t('activity.history')} · {activity.isSuccess ? t('activity.historyCount', { count: activity.data.length }) : t('activity.countUnknown')}</h2>
          <button type="button" className="text-link" disabled={activity.isFetching} onClick={() => { void activity.refetch({ cancelRefetch: false }) }}>{t('activity.retryHistory')}</button>
          <DirectorySearch compact value={historyQuery} onChange={changeHistoryQuery} label={t('activity.find.title')} hint={t('activity.find.placeholder')}
            shown={history.matching} total={history.total} known={activity.data !== undefined} loading={activity.isPending} controls={`${history.review.length ? reviewListId + ' ' : ''}tool-history-${roomId}`}/>
          <p className="field-hint">{t('activity.find.scope')}</p>
          {history.pinned > 0 && <p role="status" className="notice notice--info">{t('activity.find.pinned', { count: history.pinned })}</p>}
          {history.total > 0 && historyQuery.trim() && !history.matching && <p role="status">{t('directory.noMatches')}</p>}
          {activity.isPending && <p role="status">{t('common.loading')}</p>}
          {activity.isError && activity.data && <p className="metadata">{t('activity.lastReceived')}</p>}
          {activity.isError && <p role="alert">{t('activity.historyFailure')}</p>}
          {activity.isSuccess && activity.data.length === 0 && <p className="section-copy">{t('activity.noHistory')}</p>}
          {history.review.length > 0 && <div className="stack stack--tight">
            <h3 className="section-title">{t('activity.historyPages.review')}</h3>
            <ol id={reviewListId} className="activity-list">{history.review.map(item => <li key={item.action_id} className="activity-row">
              <ToolActivityRecord action={item} agentName={name(item.agent_id)} onInspect={onInspect ? () => { if (current()) onInspect(item.action_id) } : undefined}/>
            </li>)}</ol>
          </div>}
          {(history.review.length > 0 || history.settled.length > 12 || historyStart > 0) && <h3 className="section-title">{t('activity.historyPages.settled')}</h3>}
          {(history.settled.length > 12 || historyStart > 0) && <><p className="field-hint">{t('activity.historyPages.scope')}</p>{historyPager}</>}
          {anchorLost && <p role="status" className="notice notice--info">{t('activity.historyPages.anchorLost')}</p>}
          {history.settled.length > 0 && <p role="status" className="metadata">{t('attention.pages.range', { total: history.settled.length, from: historyStart + 1, to: historyStart + historyRows.length })}</p>}
          <ol id={`tool-history-${roomId}`} className="activity-list" start={historyStart + 1}>{historyRows.map((item, index) => <li key={item.action_id} ref={index === 0 ? firstHistoryRow : undefined} tabIndex={-1} className="activity-row">
            <ToolActivityRecord action={item} agentName={name(item.agent_id)} onInspect={onInspect ? () => { if (current()) onInspect(item.action_id) } : undefined}/>
          </li>)}</ol>
          {historyPager}
        </section>
      </div>
    </details>
    {error && <p role="alert" className="notice notice--danger">{t(error === 'revoke' ? 'activity.revokeFailure' : 'activity.revokeUnconfirmed')} · #{revokeTarget}</p>}
    {revoked !== null && <p role="status">{t('activity.revokeAccepted', { id: revoked })}</p>}
  </div>
}
