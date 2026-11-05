import { useI18n } from '../i18n'

export default function MemoryImportSource({ source, content }: { source: string; content: string }) {
  const { t } = useI18n()
  return <section aria-label={t('memoryImport.messageTitle')}>
    <strong>{t('memoryImport.messageTitle')}</strong>
    <p className="metadata">{t('memoryImport.origin', { source })}</p>
    {Array.from(content).length > 240
      ? <details className="compact-details"><summary>{t('memoryImport.messageShow')}</summary><p className="bot-memory-content">{content}</p></details>
      : <p className="bot-memory-content">{content}</p>}
  </section>
}
