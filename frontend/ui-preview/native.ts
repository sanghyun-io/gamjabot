// Workbench-only synthetic transport. This module never imports the live Tauri bridge.
type NativeFixture = (command: string, args?: Record<string, unknown>) => Promise<unknown>
let fixture: NativeFixture | null = null
const allowed = new Set(['workspace_export_transport', 'choose_generated_file_export', 'save_generated_file_export', 'choose_binary_file_export', 'save_binary_file_export', 'discard_workspace_export'])
export function installNativeFixture(value: NativeFixture) {
  const previous = fixture; fixture = value
  return () => { if (fixture === value) fixture = previous }
}
export const isTauri = () => fixture !== null
export async function invoke<T = unknown>(command: string, args?: Record<string, unknown>): Promise<T> {
  if (!fixture || !allowed.has(command)) throw new Error('[UI workbench] Native commands are disabled')
  return await fixture(command, args) as T
}
export function convertFileSrc(): string {
  throw new Error('[UI workbench] Native files are not available')
}
