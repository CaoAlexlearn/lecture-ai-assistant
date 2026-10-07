import { describe, expect, it } from "vitest"
import { lectureDestination, parseWorkspaceRoute, workspaceLinkDestination } from "./workspaceRouting"

describe("workspace routing", () => {
  const currentUrl = "http://127.0.0.1:8765/courses/economics/lectures/lecture5/live"
  const click = { href: "/courses", button: 0, defaultPrevented: false, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false }

  it("keeps home and global navigation within the loaded application", () => {
    expect(workspaceLinkDestination(click, currentUrl)).toBe("/courses")
    expect(workspaceLinkDestination({ ...click, href: "/agent-lab" }, currentUrl)).toBe("/agent-lab")
    expect(workspaceLinkDestination({ ...click, href: "/instructions?scope=global" }, currentUrl)).toBe("/instructions?scope=global")
  })

  it("preserves browser new-tab, download, and previously handled click actions", () => {
    for (const change of [{ metaKey: true }, { ctrlKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }, { target: "_blank" }, { download: true }, { defaultPrevented: true }]) {
      expect(workspaceLinkDestination({ ...click, ...change }, currentUrl)).toBeUndefined()
    }
  })

  it("leaves external links, materials, and in-page anchors to the browser", () => {
    for (const href of ["https://example.com/courses", "/api/courses/economics/material.pdf", "/courses#course-list", "mailto:teacher@example.com"]) {
      expect(workspaceLinkDestination({ ...click, href }, currentUrl)).toBeUndefined()
    }
  })

  it("parses the complete workspace route set", () => {
    expect(parseWorkspaceRoute("/courses")).toEqual({ page: "courses" })
    expect(parseWorkspaceRoute("/agent-lab")).toEqual({ page: "agent-lab" })
    expect(parseWorkspaceRoute("/instructions")).toEqual({ page: "instructions" })
    expect(parseWorkspaceRoute("/courses/open-system")).toEqual({ page: "course", courseId: "open-system" })
    expect(parseWorkspaceRoute("/courses/open-system/lectures/networks/prepare").page).toBe("prepare")
    expect(parseWorkspaceRoute("/courses/open-system/lectures/networks/live").page).toBe("live")
    expect(parseWorkspaceRoute("/courses/open-system/lectures/networks/formulas")).toEqual({ page: "formulas", courseId: "open-system", lectureId: "networks" })
  })

  it("never routes an unprepared lecture into live", () => {
    expect(lectureDestination("course", "lecture", "created")).toContain("/prepare")
    expect(lectureDestination("course", "lecture", "material_uploaded")).toContain("/prepare")
    expect(lectureDestination("course", "lecture", "preparing")).toContain("/prepare")
    expect(lectureDestination("course", "lecture", "outdated")).toContain("/prepare")
    expect(lectureDestination("course", "lecture", "failed")).toContain("/prepare")
    expect(lectureDestination("course", "lecture", "ready")).toContain("/live")
  })
})
