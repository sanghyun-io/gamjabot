import { clearPagePendingPrivateData } from '../lib/pagePendingPrivacy'
import { clearRoomStops } from '../lib/roomStopWorkspace'
import { clearContinuations } from '../lib/roomContinuationWorkspace'
import {clearConnectionDrafts} from '../lib/connectionEditorDraft'
import {clearConnectionSaves} from '../lib/connectionSaveWorkspace'
import {clearConnectionRemovals} from '../lib/connectionRemovalWorkspace'
import {clearInactiveReferences} from '../lib/inactiveReferenceWorkspace'
import { create } from 'zustand'
import { clearLegacyAuthStorage } from '../lib/legacyAuthStorage'
import { queryClient } from '../lib/queryClient'
import { roomEventCursor } from '../lib/eventCursor'
import { clearDirectoryViewMemory } from '../lib/directoryViewMemory'
import { clearUnsavedComposerDrafts } from '../lib/unsavedComposerDrafts'
import { clearDraftIndex } from '../lib/composerDraftIndex'
import { clearRecentResults } from '../lib/recentResultViews'
import { clearResultWorkspaces } from '../lib/resultSaveWorkspace'
import { clearReportEditor } from '../lib/reportEditorWorkspace'
import { clearTranscriptSaves } from '../lib/transcriptFileSave'
import { clearWorkspaceSaves } from '../lib/workspaceSaveWorkspace'
import { clearFinalizers } from '../lib/finalizeWorkspace'
import { clearRequestRetries } from '../lib/requestRetryWorkspace'
import { clearTransfers } from '../lib/transferWorkspace'
import { clearRoomActions } from '../lib/roomActionWorkspace'
import { clearRoomSetups } from '../lib/roomSetupWorkspace'
import { clearProjectLifecycle } from '../lib/projectLifecycleWorkspace'
import { clearProjectWorkspaces } from '../lib/projectEditorWorkspace'
import { clearAgentWorkspaces } from '../lib/agentEditorWorkspace'
import { clearConnectionTests } from '../lib/connectionTestWorkspace'
import { clearAuthSessions } from '../lib/authSessionWorkspace'
import { clearFeatureChecks } from '../lib/localFeatureChecks'
import { clearLocalSetups } from '../lib/localSetupWorkspace'
import { clearMemoryWorkspaces } from '../lib/memoryEditorWorkspace'
import { clearProjectAssignments } from '../lib/projectAssignmentRequests'
import { clearStarterWorkspace } from '../lib/starterWorkspace'
import { clearHandoffEditors } from '../lib/handoffEditorState'
import { clearPlanEditors } from '../lib/roomPlanEditorState'
import { clearScheduleEditors } from '../lib/roomScheduleEditorState'
import { clearMemberChanges } from '../lib/pendingMemberChanges'
import { clearPendingMessages } from '../lib/pendingRoomMessages'

// Remove credentials left by earlier versions; current credentials live only in memory.
clearLegacyAuthStorage()

function clearOwnerData() {
  clearPagePendingPrivateData()
  clearDirectoryViewMemory()
  clearInactiveReferences()
  clearConnectionRemovals()
  clearConnectionSaves()
  clearConnectionDrafts()
  clearLocalSetups()
  clearFeatureChecks()
  clearAuthSessions()
  clearConnectionTests()
  clearAgentWorkspaces()
  clearProjectWorkspaces()
  clearProjectLifecycle()
  clearRoomSetups()
  clearRoomActions()
  clearTransfers()
  clearRequestRetries()
  clearFinalizers()
  clearWorkspaceSaves()
  clearTranscriptSaves()
  clearUnsavedComposerDrafts()
  clearPendingMessages()
  clearContinuations()
  clearRoomStops()
  clearMemberChanges()
  clearScheduleEditors()
  clearPlanEditors()
  clearHandoffEditors()
  clearResultWorkspaces()
  clearRecentResults()
  clearDraftIndex()
  clearReportEditor()
  clearStarterWorkspace()
  clearProjectAssignments()
  clearMemoryWorkspaces()
  roomEventCursor.clear()
  queryClient.clear()
}

interface AuthState {
  token: string | null
  user: {
    id: number
    email: string
    username: string
  } | null
  epoch: number
  sessionOnly: boolean
  setAuth: (token: string, user: any, options?: { sessionOnly?: boolean }) => void
  replaceTokenIfCurrent: (epoch: number, owner: number, previous: string, token: string) => boolean
  logout: () => void
}

export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  user: null,
  epoch: 0,
  sessionOnly: false,
  setAuth: (token, user, options) => { clearOwnerData(); set((state) => ({ token, user, sessionOnly: options?.sessionOnly === true, epoch: state.epoch + 1 })) },
  replaceTokenIfCurrent: (epoch, owner, previous, token) => {
    let replaced = false
    set((current) => {
      if (!current.token || !token || current.epoch !== epoch
        || current.user?.id !== owner || current.token !== previous) return current
      replaced = true
      return { token }
    })
    return replaced
  },
  logout: () => { clearOwnerData(); set((state) => ({ token: null, user: null, sessionOnly: false, epoch: state.epoch + 1 })) },
}))
