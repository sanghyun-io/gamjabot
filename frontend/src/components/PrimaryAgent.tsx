import { useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Star, X } from 'lucide-react'
import { botsApi, type PrimaryBot } from '../api/bots'
import { agentsApi, type Agent } from '../api/agents'
import { useBotScope } from '../lib/useBotScope'
import { useDialogFocus } from '../lib/useDialogFocus'
import { useI18n } from '../i18n'
import { AgentCharacter } from './AgentCharacter'
import AgentConversationButton from './AgentConversationButton'
import PrimaryCheckins from './PrimaryCheckins'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'

function usePrimarySelection() {
  const scope = useBotScope(), cache = useQueryClient(), lock = useRef(false)
  const [busy, setBusy] = useState(false), [error, setError] = useState<'changed' | 'unknown' | null>(null)
  const key = ['primary-bot', scope.ownerId, scope.epoch]
  const query = useQuery({ queryKey: key, enabled: scope.ownerId > 0, retry: false, queryFn: async () => {
    const value = await botsApi.primary()
    if (!scope.canUse()) throw new Error('Bot selection changed')
    return value
  } })
  const read = async () => {
    if (!scope.canUse() || lock.current) return
    const result = await query.refetch({ cancelRefetch: false })
    if (scope.canUse() && result.isSuccess) setError(null)
  }
  const select = async (agentId: number): Promise<boolean> => {
    if (!scope.canUse() || lock.current || !query.isSuccess || query.isFetching || error) return false
    const revision = query.data.revision
    lock.current = true; setBusy(true)
    try {
      await cache.cancelQueries({ queryKey: key, exact: true })
      if (!scope.canUse()) return false
      const result = await botsApi.setPrimary(agentId, revision)
      if (!scope.canUse()) return false
      cache.setQueryData<PrimaryBot>(key, result)
      return true
    } catch (failure) {
      if (scope.canUse()) {
        const code = (failure as { response?: { data?: { detail?: { code?: string } } } }).response?.data?.detail?.code
        setError(code === 'bot_primary_changed' ? 'changed' : 'unknown')
      }
      return false
    } finally { lock.current = false; if (scope.canUse()) setBusy(false) }
  }
  return { scope, query, busy, error, read, select }
}

export function PrimaryAgentAction({ agent }: { agent: Agent }) {
  const { t } = useI18n(), selection = usePrimarySelection()
  const selected = selection.query.data?.agent_id === agent.id
  return <div className="primary-bot-action">
    <button type="button" className="button button--ghost button--compact" disabled={selected || selection.busy || !agent.is_active || !selection.query.isSuccess || !!selection.error}
      onClick={() => { void selection.select(agent.id) }}>
      <Star size={16} aria-hidden="true" fill={selected ? 'currentColor' : 'none'} />{t(selected ? 'bots.primary' : selection.busy ? 'bots.savingPrimary' : 'bots.makePrimary')}
    </button>
    {(selection.query.isError || selection.error) && <p className="metadata" role="alert">{t(selection.error === 'changed' ? 'bots.primaryChanged' : selection.error ? 'bots.primarySaveUnknown' : 'bots.primaryReadFailed')}{' '}
      <button type="button" className="text-link" disabled={selection.query.isFetching || selection.busy} onClick={() => { void selection.read() }}>{t('bots.readSelection')}</button>
    </p>}
  </div>
}

export default function PrimaryAgent() {
  const { t } = useI18n(), selection = usePrimarySelection(), [choosing, setChoosing] = useState(false)
  const value = selection.query.data, agent = value?.agent
  const pick = async (id: number) => { if (await selection.select(id)) setChoosing(false) }
  return <section className="primary-bot-row" aria-label={t('bots.primary')}>
    <div className="primary-bot-row__identity">
      {agent ? <AgentCharacter agentId={agent.id} avatar={agent.avatar || undefined} size="medium" /> : <Star size={20} aria-hidden="true" />}
      <div><span className="metadata">{t('bots.primary')}</span><strong>{agent?.name || t(selection.query.isPending ? 'common.loading' : 'bots.noPrimary')}</strong></div>
    </div>
    <div className="primary-bot-row__actions">
      {agent && agent.is_active && <AgentConversationButton key={`${selection.scope.identity}:${agent.id}`} agentId={agent.id} name={agent.name} primary />}
      <button type="button" className="button button--ghost button--compact" disabled={selection.busy || selection.query.isPending || selection.query.isError}
        onClick={() => { if (selection.scope.canUse()) setChoosing(true) }}>{t(agent ? 'bots.changePrimary' : 'bots.choosePrimary')}</button>
    </div>
    <PrimaryCheckins primary={value} />
    {value?.availability === 'agent_inactive' && <p className="metadata primary-bot-row__notice" role="status">{t('bots.unavailable')}</p>}
    {(selection.query.isError || selection.error) && <p className="metadata primary-bot-row__notice" role="alert">{t(selection.error === 'changed' ? 'bots.primaryChanged' : selection.error ? 'bots.primarySaveUnknown' : 'bots.primaryReadFailed')}{' '}
      <button type="button" className="text-link" disabled={selection.query.isFetching || selection.busy} onClick={() => { void selection.read() }}>{t('bots.readSelection')}</button>
    </p>}
    {choosing && <PrimaryAgentChooser selectedId={value?.agent_id || null} busy={selection.busy} blocked={!!selection.error || !selection.query.isSuccess || selection.query.isFetching}
      error={selection.error} reading={selection.query.isFetching} onRead={() => { void selection.read() }}
      onSelect={pick} onClose={() => { if (selection.scope.canUse()) setChoosing(false) }} />}
  </section>
}

function PrimaryAgentChooser({ selectedId, busy, blocked, error, reading, onRead, onSelect, onClose }: {
  selectedId: number | null; busy: boolean; blocked: boolean; error: 'changed' | 'unknown' | null; reading: boolean; onRead: () => void
  onSelect: (id: number) => Promise<void>; onClose: () => void
}) {
  const { t } = useI18n(), scope = useBotScope(), title = useId(), root = useRef<HTMLDivElement>(null), backdrop = useRef<HTMLDivElement>(null)
  const [search, setSearch] = useState('')
  const query = useQuery({ queryKey: ['agents', scope.identity], retry: false, queryFn: async () => {
    const value = await agentsApi.list()
    if (!scope.canUse()) throw new Error('Bot roster changed')
    return value
  } })
  useDialogFocus(root, onClose)
  useLayoutEffect(() => {
    const overflow = document.body.style.overflow
    const background = [...document.body.children].filter(node => node !== backdrop.current)
      .map(node => ({ node, inert: node.hasAttribute('inert'), hidden: node.getAttribute('aria-hidden') }))
    for (const { node } of background) { node.setAttribute('inert', ''); node.setAttribute('aria-hidden', 'true') }
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = overflow
      for (const item of background) {
        if (!item.inert) item.node.removeAttribute('inert')
        if (item.hidden === null) item.node.removeAttribute('aria-hidden'); else item.node.setAttribute('aria-hidden', item.hidden)
      }
    }
  }, [])
  const available = (query.data || []).filter(agent => agent.is_active)
  const matches = directoryMatcher(search), visible = available.filter(agent => matches([agent.name, agent.description], agent.id))
  return createPortal(<div ref={backdrop} className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
    <div ref={root} className="modal-panel stack primary-bot-chooser" role="dialog" aria-modal="true" aria-labelledby={title}>
      <div className="split"><h2 id={title} className="section-title">{t('bots.choosePrimary')}</h2><button type="button" className="icon-button" aria-label={t('common.close')} onClick={onClose}><X size={20} /></button></div>
      <DirectorySearch compact value={search} onChange={setSearch} label={t('bots.search')} hint={t('bots.searchHint')}
        shown={visible.length} total={available.length} loading={query.isFetching} known={query.isSuccess} />
      {error && <p className="metadata" role="alert">{t(error === 'changed' ? 'bots.primaryChanged' : 'bots.primarySaveUnknown')}{' '}
        <button type="button" className="text-link" disabled={reading || busy} onClick={onRead}>{t('bots.readSelection')}</button>
      </p>}
      {query.isPending && <p role="status">{t('common.loading')}</p>}
      {query.isError && <p role="alert">{t('bots.rosterReadFailed')}{' '}<button type="button" className="text-link" disabled={query.isFetching} onClick={() => { if (scope.canUse()) void query.refetch({ cancelRefetch: false }) }}>{t('common.retry')}</button></p>}
      {query.isSuccess && !available.length && <p className="section-copy">{t('bots.noBots')}{' '}<Link className="text-link" to="/agents">{t('bots.manage')}</Link></p>}
      {query.isSuccess && available.length > 0 && !visible.length && <p role="status">{t('directory.noMatches')}</p>}
      <div className="primary-bot-chooser__list">{visible.map(agent => <button key={agent.id} type="button" className="button button--ghost primary-bot-choice"
        disabled={busy || blocked || query.isFetching} aria-pressed={selectedId === agent.id} onClick={() => { if (scope.canUse()) void onSelect(agent.id) }}>
        <AgentCharacter agentId={agent.id} avatar={agent.avatar} /><span><strong>{agent.name}</strong>{agent.description && <span className="metadata">{agent.description}</span>}</span>
        {selectedId === agent.id && <Star size={16} aria-hidden="true" />}
      </button>)}</div>
    </div>
  </div>, document.body)
}
