import { useI18n } from '../i18n'

export type SavedEditorResult = { id: number; name: string; kind: 'created' | 'updated'; snapshotVersion: number; identity: string }
export function focusSavedEditorCard(prefix: 'agent' | 'project' | 'connection', id: number) {
  const card = document.getElementById(`${prefix}-card-${id}`)
  if (!card) return
  card.focus({ preventScroll: true })
  card.scrollIntoView({ block: 'center', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
}

export default function EditorSavedNotice({ result, ready, fetching, onShow, onReload, onDismiss }: {
  result: SavedEditorResult | null; ready: boolean; fetching: boolean; onShow: () => void; onReload: () => void; onDismiss: () => void
}) {
  const { t } = useI18n()
  if (!result) return null
  return <div className="notice notice--success stack stack--tight">
    <p role="status">{t(result.kind === 'created' ? 'editor.createdNotice' : 'editor.updatedNotice', { name: result.name })}</p>
    {!ready && <p className="metadata">{t(fetching ? 'common.loading' : 'editor.savedListPending')}</p>}
    <div className="page-actions">
      {ready ? <button type="button" className="button button--secondary button--compact" onClick={onShow}>{t('editor.showSaved')}</button>
        : <button type="button" className="button button--secondary button--compact" disabled={fetching} onClick={onReload}>{t('editor.reloadSaved')}</button>}
      <button type="button" className="button button--ghost button--compact" onClick={onDismiss}>{t('common.close')}</button>
    </div>
  </div>
}
