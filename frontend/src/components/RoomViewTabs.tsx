import { useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'

export type RoomView = 'conversation' | 'results' | 'memory'
export default function RoomViewTabs({ value, baseId, onSelect, canUse, resultHint, memoryHint }: {
  value: RoomView; baseId: string; onSelect: (value: RoomView) => void; canUse: () => boolean; resultHint?: string; memoryHint?: string
}) {
  const { t } = useI18n(), root = useRef<HTMLDivElement>(null)
  const buttons = useRef<Partial<Record<RoomView, HTMLButtonElement | null>>>({})
  const [focused, setFocused] = useState<RoomView>(value)
  const items: Array<{ view: RoomView; label: string; hint?: string }> = [
    { view: 'conversation', label: t('results.conversation') },
    { view: 'results', label: t('results.tab'), hint: resultHint },
    { view: 'memory', label: t('memory.tab'), hint: memoryHint },
  ]
  useEffect(() => { if (!root.current?.contains(document.activeElement)) setFocused(value) }, [value])
  const valid = () => canUse() && document.visibilityState === 'visible' && document.hasFocus()
    && !!root.current?.isConnected && root.current.getClientRects().length > 0
    && !root.current.closest('[hidden],[inert],[aria-hidden="true"]')
  return <div ref={root} className="room-view-tabs" role="tablist" aria-label={t('results.views.label')}
    aria-describedby={`${baseId}-help`} onBlur={event => {
      if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) setFocused(value)
    }} onKeyDown={event => {
      if (event.defaultPrevented || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229
        || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || !valid()) return
      if ((event.key === 'Enter' || event.key === ' ') && event.repeat) { event.preventDefault(); event.stopPropagation(); return }
      const index = items.findIndex(item => buttons.current[item.view] === document.activeElement)
      if (index < 0) return
      const next = event.key === 'ArrowRight' ? (index + 1) % items.length
        : event.key === 'ArrowLeft' ? (index + items.length - 1) % items.length
        : event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : -1
      if (next < 0) return
      event.preventDefault(); event.stopPropagation()
      const target = buttons.current[items[next].view]
      if (target?.isConnected && !target.disabled && target.getClientRects().length) {
        setFocused(items[next].view); target.focus({ preventScroll: true })
      }
    }}>
    <span id={`${baseId}-help`} className="sr-only">{t('results.views.keyboard')}</span>
    {items.map(item => <button key={item.view} ref={node => { buttons.current[item.view] = node }}
      id={`${baseId}-tab-${item.view}`} type="button" role="tab" className="room-view-tab"
      aria-label={item.label} aria-selected={value === item.view} aria-controls={`${baseId}-panel-${item.view}`}
      tabIndex={focused === item.view ? 0 : -1} onFocus={() => setFocused(item.view)} onClick={() => {
        if (!valid()) return
        setFocused(item.view); onSelect(item.view)
      }}>{item.label}{item.hint && <span className="metadata" role="status" title={item.hint}
        style={{ maxWidth: '24vw', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}> · {item.hint}</span>}</button>)}
  </div>
}
