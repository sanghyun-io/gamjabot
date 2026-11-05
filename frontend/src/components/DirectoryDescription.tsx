import { useI18n } from '../i18n'

export default function DirectoryDescription({ text, fallback }: { text?: string | null; fallback: string }) {
  const { t } = useI18n()
  if (!text) return <p className="section-copy">{fallback}</p>
  const points = Array.from(text)
  const body = <p className="section-copy" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', margin: 0 }}>{text}</p>
  if (points.length <= 240 && text.split(/\r\n|\r|\n/).length <= 4) return body
  const preview = points.slice(0, 160).join('').replace(/\s+/g, ' ').trim() + (points.length > 160 ? '…' : '')
  return <details className="compact-details" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
    <summary><strong>{t('directory.description.full')}</strong><span className="section-copy" style={{ display: 'block', fontWeight: 400 }}>{preview}</span></summary>
    {body}
  </details>
}
