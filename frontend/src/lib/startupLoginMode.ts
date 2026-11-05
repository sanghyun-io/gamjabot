import { invoke, isTauri } from '@tauri-apps/api/core'

// Unknown Native startup mode must not accidentally restore a saved login.
export async function skipAutoRestoreOnStartup(): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    if (!isTauri()) return false
    const timeout = new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('startup mode timeout')), 2000) })
    const mode = await Promise.race([invoke<unknown>('get_startup_login_mode'), timeout])
    if (!mode || typeof mode !== 'object' || Array.isArray(mode)
      || typeof (mode as { skip_auto_restore?: unknown }).skip_auto_restore !== 'boolean') return true
    return (mode as { skip_auto_restore: boolean }).skip_auto_restore
  } catch { return true }
  finally { if (timer !== undefined) clearTimeout(timer) }
}
