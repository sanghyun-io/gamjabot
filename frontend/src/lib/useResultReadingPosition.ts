import { useEffect, useRef, type RefObject, type UIEvent } from 'react'
import { readDirectoryView, writeDirectoryView } from './directoryViewMemory'

type Position = { selection: string; top: number }
type Props = { root: RefObject<HTMLElement | null>; identity: string; generation: number; roomId: number; selection: string;
  visible: boolean; ready: boolean; explicitRoute: boolean; canUse: () => boolean }
export function useResultReadingPosition(props: Props) {
  const key = `result-reading:${props.roomId}`
  const saved = useRef(readDirectoryView<Position | null>(props.identity, key, null))
  const pending = useRef(true)
  const interrupt = () => { pending.current = false }
  useEffect(() => {
    if (!pending.current) return
    const position = saved.current
    if (props.explicitRoute || !position || position.selection !== props.selection || !Number.isFinite(position.top) || position.top < 0) { pending.current = false; return }
    if (!props.visible || !props.ready || !props.canUse()) return
    const node = props.root.current
    if (!node?.isConnected || !node.getClientRects().length) return
    pending.current = false
    node.scrollTop = Math.min(position.top, Math.max(0, node.scrollHeight - node.clientHeight))
  }, [props.visible, props.ready, props.explicitRoute, props.selection, props.canUse, props.root])
  const onScroll = (event: UIEvent<HTMLElement>) => {
    if (event.target !== event.currentTarget || pending.current || !props.visible || !props.ready || !props.canUse()) return
    const node = event.currentTarget
    if (!node.isConnected || !node.getClientRects().length || !Number.isFinite(node.scrollTop)) return
    writeDirectoryView(props.identity, key, { selection: props.selection, top: Math.max(0, node.scrollTop) }, props.generation)
  }
  return { interrupt, onScroll }
}
