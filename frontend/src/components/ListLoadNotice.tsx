import { useI18n } from '../i18n'

export default function ListLoadNotice({ failed, fetching, hasSnapshot = false, message, onRetry }: {
  failed: boolean; fetching: boolean; hasSnapshot?: boolean; message: string; onRetry: () => void
}) {
  const { t } = useI18n()
  if (!failed) return null
  return <div className={`notice ${hasSnapshot ? 'notice--warning' : 'notice--danger'} stack stack--tight`}>
    <p role="alert">{message}{hasSnapshot && <> {t('common.listSnapshot')}</>}</p>
    <button type="button" className="button button--secondary button--compact" disabled={fetching} onClick={onRetry}>
      {t(fetching ? 'common.loading' : 'common.retry')}
    </button>
  </div>
}
