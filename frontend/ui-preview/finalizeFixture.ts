export function finalizeFixtureAuthority(blocked: boolean) {
  return {
    hasWaitingAction: blocked,
    eligible: !blocked,
    blockedReason: blocked ? 'action_unsettled' as const : null,
    planHash: blocked ? null : 'd'.repeat(64),
  }
}
