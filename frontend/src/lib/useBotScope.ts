import { useEffect, useRef, useState } from 'react'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from './directoryViewMemory'
import { useQuery } from '@tanstack/react-query'
import { botsApi } from '../api/bots'

export function useBotScope() {
  const ownerId = useAuthStore(state => state.user?.id || 0), epoch = useAuthStore(state => state.epoch)
  const [generation] = useState(directoryViewGeneration), alive = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const canUse = () => {
    const current = useAuthStore.getState()
    return alive.current && !!current.token && current.user?.id === ownerId && current.epoch === epoch
      && directoryViewGeneration() === generation
  }
  return { ownerId, epoch, identity: `${ownerId}:${epoch}`, canUse }
}

export function useBotConversationMetadata(roomId: number, enabled = true) {
  const scope = useBotScope()
  return useQuery({ queryKey: ['bot-conversation', roomId, scope.ownerId, scope.epoch], enabled: enabled && roomId > 0 && scope.ownerId > 0,
    retry: false, queryFn: async () => {
      const value = await botsApi.metadata(roomId)
      if (!scope.canUse()) throw new Error('Bot conversation identity changed')
      return value
    } })
}
