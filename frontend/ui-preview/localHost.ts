let previewSocket: WebSocket | null = null

export function getLocalEndpoint() {
  return Promise.resolve({ base_url: 'http://127.0.0.1:4174', session_token: 'synthetic-preview-session' })
}

export async function openRoomSocket(): Promise<WebSocket> {
  const socket = { close() {}, onopen: null, onmessage: null, onerror: null, onclose: null } as unknown as WebSocket
  previewSocket = socket
  if (new URLSearchParams(window.location.search).get('state') !== 'unknown') {
    window.setTimeout(() => socket.onopen?.(new Event('open')), 0)
  }
  return socket
}

export function previewEmitRoomEvent(event: Record<string, unknown>) {
  previewSocket?.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ type: 'event', ...event }) }))
}
