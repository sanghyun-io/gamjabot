const drafts = new Map<string,string>()
export const keepPageFeedbackDraft = (key:string, text:string) => drafts.set(key,text)
export const readPageFeedbackDraft = (key:string) => drafts.get(key)||''
export const clearPageFeedbackDraft = (key:string) => { drafts.delete(key) }
// Keep the receipt identity, but discard the optional plaintext retry data on logout/account change.
export function clearPagePendingPrivateData() {
 drafts.clear()
 try {
  for (let i=0;i<sessionStorage.length;i++) {
   const key=sessionStorage.key(i)
   if (!key?.startsWith('page-message:')) continue
   try {
    const pending=JSON.parse(sessionStorage.getItem(key)||'null')
    if (!pending || typeof pending!=='object' || Array.isArray(pending)) continue
    delete pending.content; delete pending.selection
    sessionStorage.setItem(key,JSON.stringify(pending))
   } catch { /* Preserve unreadable receipt data; do not invent an outcome. */ }
  }
 } catch { /* Storage unavailable; the UI remains fail-closed. */ }
}
