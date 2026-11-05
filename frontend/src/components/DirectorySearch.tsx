import { CircleHelp } from 'lucide-react'
import { forwardRef, useImperativeHandle, useId, useRef } from 'react'
import { useI18n } from '../i18n'

export function directoryMatcher(query: string) {
  const normalize = (value: string) => value.normalize('NFKC').toLowerCase()
  const terms = normalize(query).trim().split(/\s+/).filter(Boolean)
  // Only callers providing their canonical numeric row ID opt in to id:N.
  // Other directories keep treating every term as ordinary literal text.
  return (fields: Array<string | null | undefined>, itemId?: number) => {
    const text = normalize(fields.filter(Boolean).join(' '))
    return terms.every(term => {
      const exact = itemId !== undefined ? /^id:([0-9]+)$/.exec(term) : null
      if (!exact) return text.includes(term)
      const requested = Number(exact[1])
      return Number.isSafeInteger(requested) && requested > 0 && Number.isSafeInteger(itemId) && itemId === requested
    })
  }
}

export type DirectorySearchHandle = { focus: () => boolean }
type DirectorySearchProps = {
  compact?: boolean; maxLength?: number; onSearchPrevious?: () => void; exactId?: boolean; exactIdHelp?: string; exactIdTitle?: string; value: string; onChange: (value: string) => void; hint: string; shown: number; total: number; loading: boolean; known: boolean; controls?: string; label?: string; preventSubmit?: boolean; disabled?: boolean; initialFocus?: boolean; onSearchSubmit?: () => void
}

export default forwardRef<DirectorySearchHandle, DirectorySearchProps>(function DirectorySearch({ compact = false, value, onChange, hint, shown, total, loading, known, controls, label, preventSubmit = false, disabled = false, initialFocus = false, onSearchSubmit, onSearchPrevious, maxLength, exactId = false, exactIdHelp, exactIdTitle }, ref) {
  const { t } = useI18n()
  const inputRef = useRef<HTMLInputElement>(null), exactHelpId = useId()
  useImperativeHandle(ref, () => ({ focus: () => {
    const field = inputRef.current
    if (disabled || !field?.isConnected || field.matches(':disabled') || !field.getClientRects().length
        || field.closest('[hidden], [inert], [aria-hidden="true"]') || document.visibilityState !== 'visible' || !document.hasFocus()) return false
    field.focus({ preventScroll: true })
    return document.activeElement === field
  } }))
  const composing = useRef(false), imeUntil = useRef(0)
  const clearSearch = () => {
    if (disabled || inputRef.current?.disabled) return
    onChange('')
    inputRef.current?.focus({ preventScroll: true })
  }
  return <div className={`directory-search stack stack--tight${compact ? ' directory-search--compact' : ''}`}>
    <label className="field"><span className={compact ? 'sr-only' : undefined}>{label || t('directory.search')}</span>
      <input ref={inputRef} data-dialog-search-clear data-dialog-initial={initialFocus || undefined} type="search" maxLength={maxLength} disabled={disabled} value={value} placeholder={hint} aria-controls={controls} aria-describedby={exactId ? exactHelpId : undefined} onChange={event => { if (!disabled) onChange(event.target.value) }}
        onCompositionStart={() => { composing.current = true; imeUntil.current = 0 }}
        onCompositionEnd={() => { composing.current = false; imeUntil.current = performance.now() + 120 }}
        onBlur={() => { composing.current = false; imeUntil.current = 0 }}
        onKeyDown={event => {
          const active = composing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229
          if (active) imeUntil.current = performance.now() + 120
          if (active || performance.now() < imeUntil.current) {
            // Let the IME consume its keys without triggering app shortcuts or clearing the query.
            if (['Escape', 'Enter', 'ArrowUp', 'ArrowDown'].includes(event.key)) event.stopPropagation()
            if (preventSubmit && event.key === 'Enter' && !active) event.preventDefault()
            return
          }
          const submit = event.shiftKey ? onSearchPrevious : onSearchSubmit
          if (submit && event.key === 'Enter' && !event.altKey && !event.ctrlKey && !event.metaKey) {
            event.preventDefault(); event.stopPropagation()
            if (!event.repeat && !disabled && !inputRef.current?.disabled) submit()
            return
          }
          if (preventSubmit && event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); return }
          if (event.key === 'Escape' && value) { event.preventDefault(); event.stopPropagation(); clearSearch() }
        }} />
    </label>
    {compact ? <details className="compact-details directory-search__details">
      <summary aria-label={t('directory.ui.searchDetails')} title={t('directory.ui.searchDetails')}><CircleHelp size={16} aria-hidden="true"/></summary>
      {exactId && <p id={exactHelpId} className="field-hint"><kbd>id:12</kbd> · {exactIdHelp || t('directory.exactIdHelp')}</p>}
      <p className="metadata">{known ? t('directory.matches', { shown, total }) : ''}</p>
    </details> : <>{exactId && <details className="compact-details">
      <summary id={exactHelpId}>{exactIdTitle || t('directory.exactIdTitle')} · <kbd>id:12</kbd></summary>
      <p className="field-hint">{exactIdHelp || t('directory.exactIdHelp')}</p>
    </details>}</>}

    <div className="split"><p role="status" className={compact ? 'sr-only' : 'metadata'}>{known ? t('directory.matches', { shown, total }) : ''}{loading && <span>{known ? ' · ' : ''}{t('common.loading')}</span>}</p>
      {value && <button type="button" className="button button--ghost button--compact" disabled={disabled} onClick={clearSearch}>{t('directory.clear')}</button>}
    </div>
  </div>
})
