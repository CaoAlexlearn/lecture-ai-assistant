export function paneWidth(proposed: number, availableWidth: number, otherWidth: number, minimum: number, preferredMaximum = 520): number {
  const maximum = Math.max(minimum, Math.min(preferredMaximum, availableWidth - otherWidth - 360 - 16))
  return Math.min(Math.max(proposed, minimum), maximum)
}

export function transcriptHeight(proposed: number, availableHeight: number): number {
  return Math.min(Math.max(proposed, 180), Math.max(180, availableHeight - 260 - 8))
}
