import { useI18n } from '../i18n'

// Display-only preview. The complete literal value remains in the same reader.
export default function ReadOnlyComparisonValue({ text }: { text: string }) {
  const { t } = useI18n()
  const points = Array.from(text)
  const body = <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', margin: 0 }}>{text}</p>
  if (points.length <= 240 && text.split(/\r\n|\r|\n/).length <= 4) return body
  const preview = points.slice(0, 160).join('').replace(/\s+/g, ' ').trim() + (points.length > 160 ? '…' : '')
  return <details className="compact-details" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
    <summary><strong>{t('editor.compare.fullValue')}</strong><span style={{ display: 'block', fontWeight: 400 }}>{preview}</span></summary>
    {body}
  </details>
}
