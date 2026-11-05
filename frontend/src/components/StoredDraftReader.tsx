import { useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { useI18n } from '../i18n'
import MatchNumberJump from './MatchNumberJump'
import ClipboardTextFallback from './ClipboardTextFallback'
import { useClipboardText } from '../lib/useClipboardText'
import { useFindShortcut } from '../lib/useFindShortcut'

export type ReceivedDraft = { readId: number; room: number; title: string; project: string; listedVersion: number; version: number; content: string }

// This view only searches a received server draft or an explicit local input snapshot. It never changes the composer.
export default function StoredDraftReader({ value, initialQuery, local, canUse, onClose, onOpen, onBack, focusScope }: {
  focusScope?: RefObject<HTMLElement | null>; value: ReceivedDraft; initialQuery: string; local?: { at: string; state: 'same' | 'changed' | 'unavailable'; refresh: () => void }; canUse: () => boolean; onClose: () => void; onOpen: () => void; onBack?: () => boolean
}) {
  const { t, formatDateTime } = useI18n()
  const [backFailed,setBackFailed]=useState(false)
  const [query, setQuery] = useState(initialQuery), [chosen, setChosen] = useState(-1)
  const [caseSensitive, setCaseSensitive] = useState(true)
  const jumpContext = useMemo(() => ({}), [value.readId, value.room, value.version, value.content, query, caseSensitive])
  const alive = useRef(false), live = useRef(canUse), text = useRef<HTMLPreElement>(null), mark = useRef<HTMLElement>(null)
  const root = useRef<HTMLElement>(null), queryInput = useRef<HTMLInputElement>(null)
  const composing = useRef(false), imeAfter = useRef(0)
  const scrollIntent = useRef<{ query: string; caseSensitive: boolean; index: number } | null>(null)
  live.current = canUse
  useEffect(() => { alive.current = true; return () => { alive.current = false; scrollIntent.current = null } }, [])
  const valid = () => alive.current && live.current()
  const clipboardContext = useMemo(() => ({}), [value.readId, value.room, value.version, value.content, local?.at])
  const clipboard = useClipboardText<{ text: string; readId: number; room: number; version: number }>(clipboardContext,
    item => valid() && item.text === value.content && item.readId === value.readId && item.room === value.room && item.version === value.version)
  const copySnapshot = () => ({ text: value.content, readId: value.readId, room: value.room, version: value.version })
  useFindShortcut(true, event => {
    const boundary = root.current, input = queryInput.current
    const origin = event.target instanceof HTMLElement ? event.target : document.activeElement
    if (!valid() || composing.current || performance.now() < imeAfter.current || !boundary?.isConnected || !input?.isConnected
      || !boundary.getClientRects().length || !input.getClientRects().length || input.matches(':disabled')
      || boundary.closest('[hidden], [inert], [aria-hidden="true"]') || !(origin instanceof HTMLElement)
      || !boundary.contains(origin) && focusScope?.current !== origin) return false
    const editor = origin.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])')
    if (editor && editor !== input) return false
    if (!event.repeat) { input.focus({ preventScroll: true }); input.select(); input.scrollIntoView({ block: 'nearest', inline: 'nearest' }) }
    return true
  }, { modalBoundary: () => root.current?.closest<HTMLElement>('[aria-modal="true"]') ?? null })
  const matches = useMemo(() => {
    let count = 0, start = -1, length = 0, offset = 0
    if (!query) return { count, start, length }
    if (caseSensitive) while (offset <= value.content.length - query.length) {
      const found = value.content.indexOf(query, offset)
      if (found < 0) break
      if (count === chosen) { start = found; length = query.length }
      count++; offset = found + query.length
    }
    else {
      // Literal Unicode case matching preserves offsets into the original text.
      const literal = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      const pattern = new RegExp(literal, 'giu')
      let match: RegExpExecArray | null
      while ((match = pattern.exec(value.content)) !== null) {
        if (count === chosen) { start = match.index; length = match[0].length }
        count++
      }
    }
    return { count, start, length }
  }, [query, caseSensitive, value.content, chosen])
  const active = chosen >= 0 && chosen < matches.count ? chosen : -1
  const start = matches.start
  useLayoutEffect(() => {
    const intent = scrollIntent.current
    scrollIntent.current = null
    if (intent && intent.query === query && intent.caseSensitive === caseSensitive && intent.index === active && valid()) mark.current?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [active, query, caseSensitive])
  const goTo = (index: number) => {
    if (!valid() || !Number.isSafeInteger(index) || index < 0 || index >= matches.count) return
    scrollIntent.current = { query, caseSensitive, index }
    if (index === active) { scrollIntent.current = null; mark.current?.scrollIntoView({ block: 'nearest', inline: 'nearest' }) }
    else setChosen(index)
  }
  const move = (direction: number) => {
    if (!matches.count) return
    goTo(active < 0 ? direction > 0 ? 0 : matches.count - 1 : (active + direction + matches.count) % matches.count)
  }
  const selectAll = () => {
    if (!valid() || !text.current) return
    text.current.focus({ preventScroll: true })
    const selection = window.getSelection(), range = document.createRange()
    range.selectNodeContents(text.current); selection?.removeAllRanges(); selection?.addRange(range)
  }
  return <section ref={root} className="notice notice--info stack stack--tight" onKeyDown={event => {
    if (event.key !== 'Escape') return
    // Consume the reader's Escape before the containing quick switcher handles it.
    // During IME composition leave its native cancellation alone.
    event.stopPropagation()
    if (composing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229 || performance.now() < imeAfter.current) return
    event.preventDefault()
    if (!event.repeat && valid()) onClose()
  }} aria-label={t(local ? 'switcher.localReader.read' : 'switcher.storedDrafts.read')}>
    {onBack&&<button type="button" className="text-link" onClick={()=>{if(valid())setBackFailed(!onBack())}}>{t('switcher.readerNav.back')}</button>}
    {backFailed&&<p role="status">{t('switcher.readerNav.missing')}</p>}
    <strong>{value.title} · #{value.room}</strong>{value.project && <p className="metadata">{value.project}</p>}
    <p>{local ? t('switcher.localReader.snapshot', { time: formatDateTime(local.at, { dateStyle: 'medium', timeStyle: 'medium' }) }) : t('switcher.storedDrafts.body', { room: value.room, version: value.version })}</p>
    {local && <p className="field-hint">{t('switcher.localReader.scope')}</p>}
    {local && local.state !== 'same' && <p role="status">{t(local.state === 'changed' ? 'switcher.localReader.changed' : 'switcher.localReader.unavailable')}</p>}
    {local?.state === 'changed' && <button type="button" className="button button--secondary button--compact" onClick={() => { if (valid()) local.refresh() }}>{t('switcher.localReader.refresh')}</button>}
    {!local && value.listedVersion !== value.version && <p role="status">{t('switcher.storedDrafts.changed')}</p>}
    <label className="field">{t('switcher.draftReader.find')}
      <input ref={queryInput} type="search" aria-keyshortcuts="Meta+F Control+F" value={query} maxLength={200} autoComplete="off" onCompositionStart={() => { composing.current = true; imeAfter.current = 0 }} onCompositionEnd={() => { composing.current = false; imeAfter.current = performance.now() + 120 }}
          onBlur={() => { composing.current = false; imeAfter.current = 0 }} onChange={event => {
        if (!valid()) return
        scrollIntent.current = null; setQuery(event.target.value); setChosen(-1)
      }} onKeyDown={event => {
        if (event.key === 'Escape' && query) {
          event.stopPropagation()
          const imeActive = composing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229
          if (imeActive) { imeAfter.current = performance.now() + 120; return }
          if (performance.now() < imeAfter.current) { event.preventDefault(); return }
          event.preventDefault()
          if (!event.repeat && valid()) { scrollIntent.current = null; setQuery(''); setChosen(-1) }
          return
        }
        if (event.key !== 'Enter') return
        const imeActive = composing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229
        if (imeActive) imeAfter.current = performance.now() + 120
        event.stopPropagation()
        // Native composition keeps its Enter; post-confirmation Enter must not
        // submit a surrounding form or immediately navigate the reader.
        if (imeActive || performance.now() < imeAfter.current) {
          if (!imeActive) event.preventDefault()
          return
        }
        event.preventDefault()
        if (!event.repeat && !event.altKey && !event.ctrlKey && !event.metaKey) move(event.shiftKey ? -1 : 1)
      }}/>
    </label>
    <p className="field-hint">{t('switcher.draftReader.findShortcutHelp')} {t('switcher.draftReader.escapeHelp')}</p>
    <div className="page-actions">
      <button type="button" className="button button--secondary button--compact" disabled={!matches.count} onClick={() => goTo(0)}>{t('results.textFind.first')}</button>
      <button type="button" className="button button--secondary button--compact" disabled={!matches.count} onClick={() => move(-1)}>{t('switcher.draftReader.previous')}</button>
      <button type="button" className="button button--secondary button--compact" disabled={!matches.count} onClick={() => move(1)}>{t('switcher.draftReader.next')}</button>
      <button type="button" className="button button--secondary button--compact" disabled={!matches.count} onClick={() => goTo(matches.count - 1)}>{t('results.textFind.last')}</button>
      <span role="status" aria-live="polite">{query ? matches.count ? active < 0 ? t('switcher.draftReader.found', { count: matches.count }) : t('switcher.draftReader.count', { current: active + 1, count: matches.count }) : t('switcher.draftReader.none') : t('switcher.draftReader.idle')}</span>
    </div>
    
    <details className="compact-details">
        <summary>{t('results.textFind.options')}{!caseSensitive && <span className="metadata"> · {t('results.textFind.ignoreCase')}</span>}</summary>
        <div className="stack stack--tight">
<label className="checkbox-row"><input type="checkbox" checked={caseSensitive} onChange={event => {
      if (!valid()) return
      scrollIntent.current = null; setChosen(-1); setCaseSensitive(event.target.checked)
    }}/>{t('results.textFind.caseSensitive')}</label>
    <p className="field-hint">{t('results.textFind.help')}</p>
    {matches.count > 0 && <MatchNumberJump context={jumpContext} count={matches.count} active={active} canUse={valid} onJump={goTo}/>}
          <p className="field-hint">{t('results.textFind.optionsRetained')}</p>
        </div>
      </details>
      <pre ref={text} tabIndex={0} aria-label={t(local ? 'switcher.localReader.read' : 'switcher.storedDrafts.read')} style={{ maxHeight: '18rem', overflow: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', userSelect: 'text', margin: 0, padding: '.75rem' }}>{start < 0 ? value.content : <>{value.content.slice(0, start)}<mark ref={mark}>{value.content.slice(start, start + matches.length)}</mark>{value.content.slice(start + matches.length)}</>}</pre>
    <div className="page-actions">
      <button type="button" className="button button--secondary button--compact" disabled={clipboard.busy} onClick={() => void clipboard.copy(copySnapshot())}>{t('chat.messageAction.copy')}</button>
      <button type="button" className="text-link" onClick={selectAll}>{t('switcher.draftReader.select')}</button>
      <button type="button" className="text-link" onClick={() => { if (valid()) onOpen() }}>{t('switcher.storedDrafts.open')}</button>
      <button type="button" className="text-link" onClick={() => { if (valid()) onClose() }}>{t('common.close')}</button>
    </div>
    {clipboard.result && <p role={clipboard.result.status === 'copied' ? 'status' : 'alert'}>{t(clipboard.result.status === 'copied' ? 'chat.code.copied' : 'switcher.storedDrafts.copyFailed')}</p>}
    {clipboard.manual && <ClipboardTextFallback text={clipboard.manual.text} label={t(local ? 'switcher.localReader.read' : 'switcher.storedDrafts.read')} caption={t('chat.clipboard.manualHelp')}
      canUse={clipboard.manualAllowed} onClose={clipboard.closeManualAndRestore} onInteract={clipboard.touchManual} busy={clipboard.busy}
      onRetry={() => { if (clipboard.manual) void clipboard.copy(clipboard.manual) }}/>}
  </section>
}
