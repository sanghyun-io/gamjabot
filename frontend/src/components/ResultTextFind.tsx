import { forwardRef, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import MatchNumberJump from './MatchNumberJump'
import { readDirectoryView, writeDirectoryView } from '../lib/directoryViewMemory'

type FindSelection = {context: object; query: string; caseSensitive: boolean; index: number}

export type ResultFindMemory = { identity: string; generation: number; key: string }
type RememberedFind = { query: string; caseSensitive: boolean }

export type ResultTextFindHandle = { focusQuery: (origin: EventTarget | null) => boolean }

// A local plain-text view of the exact received version; no rendered-Markdown
// rewriting, body truncation, additional API read or external URL access.
export default forwardRef<ResultTextFindHandle, { content: string; version: number; canUse: () => boolean; memory?: ResultFindMemory }>(function ResultTextFind({ content, version, canUse, memory }, ref) {
  const { t } = useI18n()
  const [restored] = useState(() => memory ? readDirectoryView<RememberedFind | null>(memory.identity, memory.key, null) : null)
  const [expanded, setExpanded] = useState(false), [query, setQuery] = useState(typeof restored?.query === 'string' ? restored.query.slice(0, 200) : '')
  const [caseSensitive, setCaseSensitive] = useState(restored?.caseSensitive !== false)
  const remember = (nextQuery: string, nextCase: boolean) => {
    if (memory) writeDirectoryView(memory.identity, memory.key, { query: nextQuery.slice(0, 200), caseSensitive: nextCase }, memory.generation)
  }
  const context = useMemo(() => ({}), [content, version])
  const jumpContext = useMemo(() => ({}), [context, query, caseSensitive])
  const [selection, setSelection] = useState<FindSelection | null>(null)
  const active = selection?.context === context && selection.query === query && selection.caseSensitive === caseSensitive ? selection.index : -1
  const input = useRef<HTMLInputElement>(null), toggle = useRef<HTMLButtonElement>(null), mark = useRef<HTMLElement>(null)
  const mounted = useRef(false), live = useRef(canUse), composing = useRef(false), imeAfter = useRef(0), frame = useRef<number | null>(null)
  const intent = useRef<FindSelection | null>(null)
  live.current = canUse
  const valid = () => mounted.current && live.current()
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; intent.current = null; if (frame.current !== null) cancelAnimationFrame(frame.current) }
  }, [])
  const focusQuery = (origin: EventTarget | null) => {
    if (!valid() || composing.current || performance.now() < imeAfter.current) return false
    const editor = origin instanceof HTMLElement ? origin.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])') : null
    if (editor && editor !== input.current) return false
    if (frame.current !== null) { cancelAnimationFrame(frame.current); frame.current = null }
    if (expanded) { input.current?.focus({ preventScroll: true }); input.current?.select(); input.current?.scrollIntoView({ block: 'nearest', inline: 'nearest' }) }
    else setExpanded(true)
    return true
  }
  useImperativeHandle(ref, () => ({ focusQuery }))
  // Keep only the count and selected offset, rather than an unbounded match array.
  const matches = useMemo(() => {
    let count = 0, start = -1, length = 0, offset = 0
    if (expanded && query) {
      if (caseSensitive) while (offset <= content.length - query.length) {
        const found = content.indexOf(query, offset)
        if (found < 0) break
        if (count === active) {start = found; length = query.length}
        count++; offset = found + query.length
      }
      else {
        // Escaped literal only; no user regex or lowercase copy with shifted offsets.
        const literal = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
        const pattern = new RegExp(literal, 'giu')
        let match: RegExpExecArray | null
        while ((match = pattern.exec(content)) !== null) {
          if (count === active) {start = match.index; length = match[0].length}
          count++
        }
      }
    }
    return { count, start, length }
  }, [expanded, content, query, caseSensitive, active])
  useLayoutEffect(() => { if (expanded && valid()) { input.current?.focus({ preventScroll: true }); input.current?.select(); input.current?.scrollIntoView({ block: 'nearest', inline: 'nearest' }) } }, [expanded])
  useLayoutEffect(() => {
    const requested = intent.current; intent.current = null
    if (expanded && requested?.context === context && requested.query === query && requested.caseSensitive === caseSensitive && requested.index === active && valid()) mark.current?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [context, query, caseSensitive, active, matches.start])
  const goTo = (index: number) => {
    if (!expanded || !valid() || !Number.isSafeInteger(index) || index < 0 || index >= matches.count) return
    if (index === active) { mark.current?.scrollIntoView({ block: 'nearest', inline: 'nearest' }); return }
    intent.current = { context, query, caseSensitive, index }; setSelection({ context, query, caseSensitive, index })
  }
  const move = (direction: number) => {
    if (!matches.count) return
    goTo(active < 0 ? direction > 0 ? 0 : matches.count - 1 : (active + direction + matches.count) % matches.count)
  }
  const clearQuery = () => {
    if (!valid()) return
    intent.current = null; setQuery(''); setSelection(null); remember('', caseSensitive)
    input.current?.focus({ preventScroll: true })
  }
  const close = () => {
    if (!valid()) return
    intent.current = null; setExpanded(false)
    frame.current = requestAnimationFrame(() => {
      frame.current = null
      if (valid() && (document.activeElement === document.body || document.activeElement === toggle.current)) toggle.current?.focus({ preventScroll: true })
    })
  }
  return <section className="stack stack--tight">
    <button ref={toggle} type="button" className="text-link" aria-keyshortcuts="Meta+F Control+F" title={`${t('results.textFind.open', { version })} · ⌘/Ctrl F`} aria-expanded={expanded} onClick={() => {
      if (!valid()) return
      if (frame.current !== null) { cancelAnimationFrame(frame.current); frame.current = null }
      if (expanded) close(); else focusQuery(null)
    }}>{t('results.textFind.open', { version })}</button>
    {expanded && <div className="notice stack stack--tight" onKeyDown={event => {
      if (event.key !== 'Escape') return
      event.stopPropagation()
      const imeActive = composing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229
      if (imeActive || performance.now() < imeAfter.current) {
        if (!imeActive && event.target === input.current) event.preventDefault()
        return
      }
      event.preventDefault()
      if (event.repeat || !valid()) return
      if (event.target === input.current && query) clearQuery()
      else close()
    }}>
      <label className="field">{t('results.textFind.query', { version })}
        <input ref={input} type="search" value={query} maxLength={200} autoComplete="off"
          onCompositionStart={() => { composing.current = true; imeAfter.current = 0 }} onCompositionEnd={() => { composing.current = false; imeAfter.current = performance.now() + 120 }}
          onBlur={() => { composing.current = false; imeAfter.current = 0 }}
          onChange={event => { if (valid()) { intent.current = null; setQuery(event.target.value); setSelection(null); remember(event.target.value, caseSensitive) } }}
          onKeyDown={event => {
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
      <p className="field-hint">{t('results.textFind.escapeHelp')}</p>
      <div className="page-actions">
        {query && <button type="button" className="button button--ghost button--compact" onClick={clearQuery}>{t('directory.clear')}</button>}
        <button type="button" className="button button--secondary button--compact" disabled={!matches.count} onClick={() => goTo(0)}>{t('results.textFind.first')}</button>
        <button type="button" className="button button--secondary button--compact" disabled={!matches.count} onClick={() => move(-1)}>{t('switcher.draftReader.previous')}</button>
        <button type="button" className="button button--secondary button--compact" disabled={!matches.count} onClick={() => move(1)}>{t('switcher.draftReader.next')}</button>
        <button type="button" className="button button--secondary button--compact" disabled={!matches.count} onClick={() => goTo(matches.count - 1)}>{t('results.textFind.last')}</button>
        <span role="status" aria-live="polite">{query ? !matches.count ? t('results.textFind.none') : active < 0 ? t('switcher.draftReader.found', { count: matches.count }) : t('switcher.draftReader.count', { current: active + 1, count: matches.count }) : t('results.textFind.idle')}</span>
      </div>
      
      <details className="compact-details">
        <summary>{t('results.textFind.options')}{!caseSensitive && <span className="metadata"> · {t('results.textFind.ignoreCase')}</span>}</summary>
        <div className="stack stack--tight">
<label className="checkbox-row"><input type="checkbox" checked={caseSensitive} onChange={event => {
        if (!valid()) return
        intent.current = null; setSelection(null); setCaseSensitive(event.target.checked); remember(query, event.target.checked)
      }}/>{t('results.textFind.caseSensitive')}</label>
      <p className="field-hint">{t('results.textFind.help')}</p>
      {memory && <div className="stack stack--tight"><p className="field-hint">{t('results.textFind.memoryHelp')}</p>
        <div><button type="button" className="text-link" disabled={!query && caseSensitive} onClick={() => {
          if (!valid()) return
          intent.current = null; setQuery(''); setSelection(null); setCaseSensitive(true); remember('', true)
          input.current?.focus({ preventScroll: true })
        }}>{t('results.textFind.resetMemory')}</button></div>
      </div>}
      {matches.count > 0 && <MatchNumberJump context={jumpContext} count={matches.count} active={active} canUse={valid} onJump={goTo}/>}
          <p className="field-hint">{t('results.textFind.optionsRetained')}</p>
        </div>
      </details>
      <pre tabIndex={0} aria-label={t('results.textFind.original', { version })} style={{ maxHeight: '24rem', overflow: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', userSelect: 'text', margin: 0, padding: '.75rem' }}>{matches.start < 0 ? content : <>{content.slice(0, matches.start)}<mark ref={mark}>{content.slice(matches.start, matches.start + matches.length)}</mark>{content.slice(matches.start + matches.length)}</>}</pre>
      <p className="field-hint">{t('results.textFind.limit')}</p>
      <button type="button" className="text-link" onClick={close}>{t('common.close')}</button>
    </div>}
  </section>
})
