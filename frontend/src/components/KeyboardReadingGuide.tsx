import { useI18n } from '../i18n'

// Shared read-only content; no activation key listener or request.
export function KeyboardReadingContent() {
  const { t } = useI18n()
  return <div className="stack stack--tight">
    <ul className="stack stack--tight">
      <li><kbd>Tab → Enter</kbd> · {t('keyboardReading.skip')}</li>
      <li><kbd>⌘/Ctrl+K</kbd> · {t('switcher.open')}</li>
      <li><kbd>⌘/Ctrl+F</kbd> · {t('keyboardReading.find')}</li>
      <li><kbd>Tab / Shift+Tab</kbd> · {t('keyboardReading.tab')}</li>
      <li><kbd>← / → / Home / End</kbd> · {t('keyboardReading.views')}</li>
      <li><kbd>Enter / Shift+Enter</kbd> · {t('keyboardReading.matches')}</li>
      <li><kbd>id:12</kbd> · {t('keyboardReading.exactId')}</li>
      <li>{t('keyboardReading.numberJump')}</li>
    </ul>
    <p className="field-hint">{t('keyboardReading.limits')}</p>
  </div>
}

export default function KeyboardReadingGuide() {
  const { t } = useI18n()
  return <details className="compact-details">
    <summary>{t('keyboardReading.title')}</summary>
    <KeyboardReadingContent/>
  </details>
}
