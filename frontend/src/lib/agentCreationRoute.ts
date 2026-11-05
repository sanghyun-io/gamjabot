import { validEditorModelId } from './editorValidation'

// Public selection only, bound to the current Owner view. Never a credential,
// automatic Agent save, model check, or permission to overwrite an existing draft.
export function agentCreationState(identity: string, generation: number, connectionId: number, modelId: string) {
  return { agentCreation: { identity, generation, connectionId, modelId: modelId.trim() } }
}

export function readAgentCreation(state: unknown, identity: string, generation: number) {
  if (!state || typeof state !== 'object') return null
  const value = (state as Record<string, unknown>).agentCreation
  if (!value || typeof value !== 'object') return null
  const target = value as Record<string, unknown>
  if (target.identity !== identity || target.generation !== generation || typeof target.connectionId !== 'number'
    || !Number.isSafeInteger(target.connectionId) || target.connectionId < 1 || typeof target.modelId !== 'string'
    || !validEditorModelId(target.modelId.trim())) return null
  return { connectionId: target.connectionId, modelId: target.modelId.trim() }
}
