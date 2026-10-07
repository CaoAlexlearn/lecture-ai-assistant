import { describe, expect, it } from "vitest"
import { paneWidth, transcriptHeight } from "./workspaceLayout"

describe("reading workspace resizing", () => {
  it("keeps enough room for the PDF when either side pane is widened", () => {
    const available = 1100
    const tree = paneWidth(700, available, 320, 180, 360)
    const detail = paneWidth(800, available, tree, 280)
    expect(tree).toBeLessThanOrEqual(360)
    expect(available - tree - detail - 16).toBeGreaterThanOrEqual(360)
    expect(paneWidth(-100, available, tree, 280)).toBe(280)
  })
  it("bounds the transcript without hiding the slide area", () => {
    expect(transcriptHeight(1000, 700)).toBe(432)
    expect(transcriptHeight(10, 700)).toBe(180)
  })
})
