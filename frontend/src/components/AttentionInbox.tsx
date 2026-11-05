import { reviewTimestamp, reviewTimeOptions } from '../lib/reviewTime'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Bell, CircleAlert, ShieldCheck, X } from 'lucide-react'
import { Link } from 'react-router-dom'
import { attentionApi, type AttentionItem, type AttentionSummaryCode } from '../api/attention'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { createPortal } from 'react-dom'
import type { MessageKey } from '../i18n/locales/en'

const summaryKeys: Record<AttentionSummaryCode, MessageKey> = {
  approval_process: 'attention.summary.approvalProcess', approval_files: 'attention.summary.approvalFiles',
  approval_browser: 'attention.summary.approvalBrowser', approval_screen: 'attention.summary.approvalScreen',
  approval_input: 'attention.summary.approvalInput', approval_other: 'attention.summary.approvalOther',
  failure_connection: 'attention.summary.failureConnection', failure_context: 'attention.summary.failureContext',
  failure_tool: 'attention.summary.failureTool', failure_permission: 'attention.summary.failurePermission',
  failure_uncertain: 'attention.summary.failureUncertain', failure_other: 'attention.summary.failureOther',
}

export default function AttentionInbox() {
  const [open, setOpen] = useState(false), [items, setItems] = useState<AttentionItem[]>([])
  const [next, setNext] = useState<string | null>(null), [resultOwner, setResultOwner] = useState('')
  const [loading, setLoading] = useState(false), [error, setError] = useState<'first' | 'more' | null>(null)
  const [refresh, setRefresh] = useState(0)
  const [query, setQuery] = useState(''), [kind, setKind] = useState<'all' | 'approval' | 'failure'>('all')
  const [order, setOrder] = useState<'received' | 'oldest'>('received')
  const [pageAnchor, setPageAnchor] = useState<string | null>(null)
  const [receivedAt, setReceivedAt] = useState<number | null>(null), [moreReceivedAt, setMoreReceivedAt] = useState<number | null>(null)
  const identity = useAuthStore((value) => `${value.user?.id || 0}:${value.epoch}`)
  const generation = useRef(0), pending = useRef<number | null>(null), opened = useRef(false), mounted = useRef(true)
  const focusVersion = useRef(0), focusFrame = useRef<number | null>(null), trigger = useRef<HTMLButtonElement>(null), panel = useRef<HTMLElement>(null)
  const firstMatch = useRef<HTMLAnchorElement>(null)
  const pageFocus = useRef<{ id: string; origin: HTMLButtonElement } | null>(null)
  const { t, formatDateTime } = useI18n()
  const sameOwner = () => identity === `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}`
  const current = (run: number) => mounted.current && opened.current && run === generation.current && sameOwner()
  const cancelFocus = () => { pageFocus.current = null; focusVersion.current += 1; if (focusFrame.current !== null) cancelAnimationFrame(focusFrame.current); focusFrame.current = null }
  const close = (restore = true) => {
    cancelFocus(); const version = focusVersion.current; opened.current = false; generation.current += 1; pending.current = null
    setOpen(false); setLoading(false)
    if (restore) focusFrame.current = requestAnimationFrame(() => {
      focusFrame.current = null
      if (mounted.current && !opened.current && version === focusVersion.current && sameOwner()
          && !document.querySelector('[aria-modal="true"]') && trigger.current?.isConnected) trigger.current.focus()
    })
  }
  const show = () => {
    if (!sameOwner() || opened.current || document.querySelector('[aria-modal="true"]')) return
    cancelFocus(); opened.current = true; setOpen(true)
  }
  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; opened.current = false; generation.current += 1; pending.current = null; cancelFocus()
  } }, [])
  useEffect(() => {
    cancelFocus(); opened.current = false; generation.current += 1; pending.current = null
    setOpen(false); setItems([]); setQuery(''); setKind('all'); setOrder('received'); setPageAnchor(null); setReceivedAt(null); setMoreReceivedAt(null); setResultOwner(''); setNext(null); setLoading(false); setError(null)
  }, [identity])
  useEffect(() => {
    if (!open || !opened.current || !sameOwner()) return
    const run = ++generation.current
    pending.current = run; setLoading(true); setError(null)
    attentionApi.list().then((page) => {
      if (!current(run)) return
      setItems([...new Map(page.items.map((item) => [item.id, item])).values()])
      setResultOwner(identity); setNext(page.next_cursor); setReceivedAt(Date.now()); setMoreReceivedAt(null)
      setPageAnchor(previous => previous && page.items.some(item => item.id === previous) ? previous : null)
    }).catch(() => { if (current(run)) setError('first') })
      .finally(() => { if (current(run)) { pending.current = null; setLoading(false) } })
    return () => { generation.current += 1; if (pending.current === run) pending.current = null }
  }, [open, identity, refresh])
  const reload = () => {
    if (pending.current !== null || !opened.current || !sameOwner()) return
    pending.current = ++generation.current; setLoading(true); setError(null); setRefresh((value) => value + 1)
  }
  const loadMore = async () => {
    if (!next || pending.current !== null || !opened.current || resultOwner !== identity || !sameOwner()) return
    // Stabilize the first displayed item before adding older rows to a sorted list.
    setPageAnchor(previous => previous ?? pageItems[0]?.id ?? null)
    const cursor = next, run = ++generation.current
    pending.current = run; setLoading(true); setError(null)
    try {
      const page = await attentionApi.list(cursor)
      if (!current(run)) return
      setItems((old) => { const known = new Set(old.map((item) => item.id)); return [...old, ...page.items.filter((item) => {
        if (known.has(item.id)) return false
        known.add(item.id); return true
      })] })
      setNext(page.next_cursor); setMoreReceivedAt(Date.now())
    } catch { if (current(run)) setError('more') }
    finally { if (current(run)) { pending.current = null; setLoading(false) } }
  }
  const visible = resultOwner === identity ? items : []
  const visibleNext = resultOwner === identity ? next : null
  const matches = directoryMatcher(query)
  const matching = visible.filter(item => (kind === 'all' || item.kind === kind)
    && matches([item.room_title, item.project_name, item.agent_name, String(item.room_id), `#${item.room_id}`,
      t(summaryKeys[item.summary_code])]))

  const filtered = order === 'oldest' ? matching.map((item, index) => {
    const stamp = reviewTimestamp(item.occurred_at)
    return { item, index, time: stamp === null ? null : Date.parse(stamp) }
  }).sort((a, b) => a.time === null ? b.time === null ? a.index - b.index : 1
    : b.time === null ? -1 : a.time - b.time || a.index - b.index).map(row => row.item) : matching
  const pageStart = pageAnchor ? Math.max(0, filtered.findIndex(item => item.id === pageAnchor)) : 0
  const pageEnd = Math.min(pageStart + 12, filtered.length)
  const pageItems = filtered.slice(pageStart, pageEnd)
  const resetPage = () => { pageFocus.current = null; setPageAnchor(null) }
  const movePage = (index: number, origin: HTMLButtonElement) => {
    if (!mounted.current || !opened.current || !sameOwner() || !filtered.length || document.visibilityState !== 'visible') return
    const target = filtered[Math.max(0, Math.min(index, filtered.length - 1))]
    if (target.id === pageItems[0]?.id) return
    pageFocus.current = { id: target.id, origin }; setPageAnchor(target.id)
  }
  useLayoutEffect(() => {
    const requested = pageFocus.current; pageFocus.current = null
    if (!requested || requested.id !== pageItems[0]?.id || !mounted.current || !opened.current || !sameOwner()
        || document.visibilityState !== 'visible' || document.activeElement !== requested.origin && document.activeElement !== document.body) return
    const row = firstMatch.current?.closest('li')
    if (row?.isConnected && row.getClientRects().length && !row.closest('[hidden], [inert]')) {
      // A non-activating row avoids opening a link if the paging Enter repeats.
      row.focus({ preventScroll: true }); row.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    }
  }, [pageAnchor, pageItems[0]?.id, open, identity])
  const pager = () => filtered.length > 12 && <nav className="page-actions" aria-label={t('attention.pages.title')}>
    <button type="button" className="text-link" disabled={pageStart === 0} onClick={event => movePage(0, event.currentTarget)}>{t('attention.pages.first')}</button>
    <button type="button" className="text-link" disabled={pageStart === 0} onClick={event => movePage(Math.max(0, pageStart - 12), event.currentTarget)}>{t('attention.pages.previous')}</button>
    <button type="button" className="text-link" disabled={pageEnd >= filtered.length} onClick={event => movePage(pageStart + 12, event.currentTarget)}>{t('attention.pages.next')}</button>
    <button type="button" className="text-link" disabled={pageEnd >= filtered.length} onClick={event => movePage(Math.floor((filtered.length - 1) / 12) * 12, event.currentTarget)}>{t('attention.pages.last')}</button>
  </nav>
  const itemTime = (item: AttentionItem) => {
    const stamp = reviewTimestamp(item.occurred_at)
    return stamp === null ? t('attention.order.unknownTime') : formatDateTime(stamp, reviewTimeOptions)
  }

  const openFirstMatch = () => {
    if (!query.trim() || !pageItems.length || !mounted.current || !opened.current || !sameOwner()
        || resultOwner !== identity || document.visibilityState !== 'visible') return
    const link = firstMatch.current
    // Use the same mounted Link, route and Owner check as a direct row click.
    if (link?.isConnected && link.getClientRects().length && !link.closest('[hidden], [inert]')) link.click()
  }
  return <><button ref={trigger} type="button" className="icon-button attention-trigger" aria-label={t('attention.open')} onClick={show}><Bell size={18}/></button>
    {open && createPortal(<div className="modal-backdrop attention-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) close() }}>
      <section ref={panel} role="dialog" aria-modal="true" aria-label={t('attention.title')} className="attention-inbox" onKeyDown={(event) => {
        if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return }
        if (event.key !== 'Tab') return
        const controls = [...(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled)') || [])].filter(control => control.getClientRects().length > 0 && !control.closest('[hidden], [inert]'))
        if (!controls.length) return
        if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1)?.focus() }
        else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0].focus() }
      }}>
        <header><div><h2>{t('attention.title')}</h2><p>{t('attention.help')}</p></div><button autoFocus className="icon-button" aria-label={t('common.close')} onClick={() => close()}><X size={18}/></button></header>
        <div className="attention-inbox__body">
          <button type="button" className="button button--secondary button--compact" disabled={loading} onClick={reload}>{t('attention.refresh')}</button>
          {loading && <p role="status">{t('common.loading')}</p>}
          {resultOwner === identity && receivedAt !== null && <div className="stack stack--tight">
            <p className="metadata">{t('attention.order.receivedAt', { time: formatDateTime(receivedAt, reviewTimeOptions) })}</p>
            {moreReceivedAt !== null && <p className="metadata">{t('attention.order.moreReceivedAt', { time: formatDateTime(moreReceivedAt, reviewTimeOptions) })}</p>}
          </div>}
          {(loading || error === 'first') && visible.length > 0 && <p className="metadata">{t('common.listSnapshot')}</p>}
          {error === 'first' && <div className="stack stack--tight"><p role="alert" className="notice notice--warning">{t('attention.unavailable')}</p>
            <button type="button" className="button button--secondary" disabled={loading} onClick={reload}>{t('common.retry')}</button></div>}
          {!loading && !error && !visible.length && <p className="section-copy">{t('attention.empty')}</p>}
          <section className="stack stack--tight" aria-label={t('attention.find.title')}>
            <DirectorySearch value={query} onChange={value => { if (opened.current && sameOwner()) { resetPage(); setQuery(value) } }}
              label={t('attention.find.title')} hint={t('attention.find.placeholder')} shown={filtered.length} total={visible.length}
              known={resultOwner === identity} loading={loading} controls="attention-received-list" preventSubmit
              onSearchSubmit={query.trim() && pageItems.length ? openFirstMatch : undefined} />
            {query.trim() && <div className="stack stack--tight">
              <p className="field-hint">{t('attention.find.openHelp')}</p>
              <div><button type="button" className="button button--secondary button--compact" disabled={!pageItems.length} onClick={openFirstMatch}>{t('attention.find.openFirst')}{pageItems[0] && <> · {pageItems[0].room_title} · {pageItems[0].agent_name}</>}</button></div>
            </div>}
            <div role="group" aria-label={t('attention.find.kind')} className="page-actions">
              {(['all', 'approval', 'failure'] as const).map(value => <button key={value} type="button"
                className={`button button--compact ${kind === value ? 'button--primary' : 'button--secondary'}`} aria-pressed={kind === value}
                onClick={() => { if (opened.current && sameOwner()) { resetPage(); setKind(value) } }}>{t(`attention.find.${value}`)}</button>)}
            </div>
            <div role="group" aria-label={t('attention.order.title')} className="page-actions">
              {(['received', 'oldest'] as const).map(value => <button key={value} type="button"
                className={`button button--compact ${order === value ? 'button--primary' : 'button--secondary'}`} aria-pressed={order === value}
                onClick={() => { if (opened.current && sameOwner()) { resetPage(); setOrder(value) } }}>{t(`attention.order.${value}`)}</button>)}
            </div>
            <p className="field-hint">{t('attention.order.scope')}</p>
            <p className="field-hint">{t('attention.find.scope')}</p>
            {visible.length > 0 && !filtered.length && <p role="status" className="notice notice--info">{t('attention.find.empty')}</p>}
          </section>
          <p className="field-hint">{t('attention.pages.scope')}</p>
          {filtered.length > 0 && <p role="status" className="metadata">{t('attention.pages.range', { from: pageStart + 1, to: pageEnd, total: filtered.length })}</p>}
          {pager()}
          {<ul id="attention-received-list" className="attention-list" aria-busy={loading}>{pageItems.map((item, index) => <li tabIndex={-1} key={item.id}><Link ref={index === 0 ? firstMatch : undefined} to={item.href} onClick={(event) => {
            if (!sameOwner() || !opened.current || resultOwner !== identity) { event.preventDefault(); return }
            close(false)
          }} className="attention-row">
            <span className={`attention-row__icon attention-row__icon--${item.kind}`}>{item.kind === 'approval' ? <ShieldCheck size={18}/> : <CircleAlert size={18}/>}</span>
            <span><strong>{t(summaryKeys[item.summary_code])}</strong><small>{item.room_title} · {item.project_name}</small><small>{item.agent_name} · {itemTime(item)}</small></span>
          </Link></li>)}</ul>}
          {pager()}
          {error === 'more' && <p role="alert" className="notice notice--warning">{t('attention.moreFailure')}</p>}
          {visibleNext && error !== 'first' && <button type="button" className="button button--secondary" disabled={loading} onClick={() => void loadMore()}>{t(error === 'more' ? 'common.retry' : 'attention.more')}</button>}
        </div>
      </section>
    </div>, document.body)}</>
}
