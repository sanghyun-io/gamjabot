export function closeOtherRoomPanels(current: HTMLDetailsElement) {
  if (!current.open) return
  current.closest('.room-column')?.querySelectorAll<HTMLDetailsElement>('details[name="room-panel"][open]')
    .forEach((panel) => { if (panel !== current) panel.open = false })
}
