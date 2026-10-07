import type { PreparationStatus, WorkspaceRoute } from "./types"

type LinkClick = Pick<MouseEvent, "button" | "defaultPrevented" | "metaKey" | "ctrlKey" | "shiftKey" | "altKey"> & {
  href: string
  target?: string
  download?: boolean
}

export function workspaceLinkDestination(link: LinkClick, currentUrl: string): string | undefined {
  if (link.defaultPrevented || link.button !== 0 || link.metaKey || link.ctrlKey || link.shiftKey || link.altKey || link.download || (link.target && link.target !== "_self")) return
  const current = new URL(currentUrl)
  const destination = new URL(link.href, current)
  if (destination.origin !== current.origin || destination.hash || parseWorkspaceRoute(destination.pathname).page === "not-found") return
  return destination.pathname + destination.search
}

export function parseWorkspaceRoute(pathname: string): WorkspaceRoute {
  const parts = pathname.replace(/^\/+|\/+$/g, "").split("/").filter(Boolean)
  if (parts.length === 1 && parts[0] === "courses") return { page: "courses" }
  if (parts.length === 1 && parts[0] === "agent-lab") return { page: "agent-lab" }
  if (parts.length === 1 && parts[0] === "instructions") return { page: "instructions" }
  if (parts.length === 2 && parts[0] === "courses") return { page: "course", courseId: parts[1] }
  if (parts.length === 5 && parts[0] === "courses" && parts[2] === "lectures" && (parts[4] === "prepare" || parts[4] === "live" || parts[4] === "formulas")) {
    return { page: parts[4], courseId: parts[1], lectureId: parts[3] }
  }
  return { page: "not-found" }
}

export function lectureDestination(courseId: string, lectureId: string, status: PreparationStatus): string {
  const destination = status === "ready" ? "live" : "prepare"
  return `/courses/${courseId}/lectures/${lectureId}/${destination}`
}
