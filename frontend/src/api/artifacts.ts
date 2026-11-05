import { invoke, isTauri } from '@tauri-apps/api/core'
import { useAuthStore } from '../stores/authStore'

export type ScreenshotArtifactState = 'available' | 'removed' | 'unavailable'

export interface ScreenshotArtifactItem {
  room_id: number
  event_seq: number
  action_id: string
  created_at: string
  width: number
  height: number
  state: ScreenshotArtifactState
}

export interface ScreenshotArtifactPage {
  version: 1
  room_id: number
  after_seq: number
  scanned_through_seq: number
  next_after_seq: number | null
  items: ScreenshotArtifactItem[]
}

function unavailable(value?: unknown): Error {
  return new Error(typeof value === 'string' ? value : 'artifact_unavailable')
}

export const artifactsApi = {
  list: async (roomId: number, afterSeq = 0, limit = 50): Promise<ScreenshotArtifactPage> => {
    if (!isTauri()) throw unavailable()
    const bearerToken = useAuthStore.getState().token
    if (!bearerToken) throw unavailable('access_denied')
    try {
      return await invoke<ScreenshotArtifactPage>('list_screenshot_artifacts', {
        roomId, afterSeq, limit, bearerToken,
      })
    } catch (error) {
      throw unavailable(error)
    }
  },
}
