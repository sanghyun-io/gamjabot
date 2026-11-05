import { useI18n } from '../i18n'
import type { RoomStopState } from '../lib/roomStopWorkspace'
export type { RoomStopState } from '../lib/roomStopWorkspace'
export default function RoomStopNotice({ state }: { state: RoomStopState }) {
  const { t } = useI18n()
  if (!state) return null
  const warning = state === 'unknown' || state === 'notSent'
  return <p role={warning ? 'alert' : 'status'} className={`notice ${warning ? 'notice--warning' : 'notice--info'}`} style={{ flexShrink: 0 }}>{t(`chat.stopRequest.${state}`)}</p>
}
