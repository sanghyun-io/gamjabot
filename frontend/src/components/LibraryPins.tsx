import type { ReactNode } from 'react'

export type LibraryPinView = {
  key: string
  basis?: string
  title: string
  detail: string
  availability: 'available' | 'removed' | 'unavailable' | 'unknown'
  open?: ReactNode
  unknownMessage?: string
}

/** Displays the server snapshot; actions and recovery stay owned by the caller. */
export default function LibraryPins({ items, loading, error, busy, onRefresh, onRemove, ko }: {
  items: LibraryPinView[]; loading: boolean; error: boolean; busy: boolean; ko: boolean
  onRefresh: () => void; onRemove: (key: string) => void
}) {
  const text = (k: string, e: string) => ko ? k : e
  const availability = {
    available: '',
    removed: text('원본이 삭제되었습니다.', 'The original was removed.'),
    unavailable: text('이 원본에 접근할 수 없습니다.', 'The original is unavailable.'),
    unknown: text('원본 상태를 확인할 수 없습니다. 다시 확인해 주세요.', 'The original status could not be checked. Try again.'),
  }
  return <section className="stack stack--tight" aria-label={text('고정된 항목', 'Pinned items')}>
    <h2 className="section-title">{text('고정된 항목', 'Pinned items')}</h2>
    <p className="field-hint">{text('고정은 바로가기입니다. 원본 상태는 확인 시점의 상태이며 원본이 복사되거나 삭제되지 않도록 보호하지 않습니다. 모바일에서는 아직 보관함을 사용할 수 없습니다.', 'Pins are shortcuts. Availability reflects the time checked; pins do not copy or protect originals from deletion. The library is not yet available on mobile.')}</p>
    {!error && <button type="button" className="text-link" style={{justifySelf:'start'}} disabled={loading} onClick={onRefresh}>{text('고정 항목 다시 확인', 'Refresh pinned items')}</button>}
    {loading && <p role="status">{text('고정 항목 확인 중…', 'Loading pinned items…')}</p>}
    {error && <p role="alert">{text('고정 항목을 확인하지 못했습니다.', 'Could not load pinned items.')} <button type="button" className="text-link" disabled={busy} onClick={onRefresh}>{text('다시 확인', 'Try again')}</button></p>}
    {!loading && !error && items.length === 0 && <p>{text('고정된 항목이 없습니다.', 'No pinned items.')}</p>}
    {items.length > 0 && <ul className="stack stack--tight" style={{ listStyle: 'none', padding: 0 }}>{items.map(item => <li className="skill-row stack stack--tight" key={item.key}>
      <strong>{item.title}</strong><p className="metadata">{item.detail}</p>
      {item.availability !== 'available' && <p role="status">{item.availability==='unknown'&&item.unknownMessage?item.unknownMessage:availability[item.availability]}</p>}
      <div className="page-actions">{item.open}<button type="button" className="text-link" disabled={busy} onClick={() => onRemove(item.key)}>{text('고정 해제', 'Unpin')}</button></div>
    </li>)}</ul>}
  </section>
}
