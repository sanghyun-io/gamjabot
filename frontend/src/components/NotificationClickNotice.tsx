type Props = { reason: 'modal' | 'unresolved'; locale: string; onOpen: () => void; onDismiss: () => void }

/** The original click is kept only in the current signed-in app session. */
export function NotificationClickNotice({reason, locale, onOpen, onDismiss}: Props) {
  const ko = locale === 'ko'
  return <div role="status" className="notice notice--info page-frame page-frame--wide">
    <p>{reason === 'modal'
      ? (ko ? '열려 있는 창을 닫은 뒤 알림을 확인하세요.' : 'Close the open dialog to review the notification.')
      : (ko ? '알림을 열지 못했어요. 연결을 확인한 뒤 다시 열 수 있어요.' : 'Could not open the notification. Check the connection and try again.')}</p>
    <div className="page-actions">
      <button type="button" className="button button--secondary button--compact" onClick={onOpen}>{ko ? '알림 열기' : 'Open notification'}</button>
      <button type="button" className="button button--ghost button--compact" onClick={onDismiss}>{ko ? '닫기' : 'Dismiss'}</button>
    </div>
  </div>
}
