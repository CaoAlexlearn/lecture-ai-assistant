import { afterEach, describe, expect, it, vi } from "vitest"
import { workspaceApi } from "./workspaceApi"
import { enrichmentApi } from "../classroom-enrichment/enrichmentApi"

afterEach(() => vi.unstubAllGlobals())

describe("preparation backend selection", () => {
  it.each(["qwen", "codex"] as const)("sends %s only on explicit run", async (backend) => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ preparationStatus: "preparing" }) })
    vi.stubGlobal("fetch", fetch)
    await workspaceApi.runPreparation("course", "lecture", backend)
    expect(fetch).toHaveBeenCalledWith(`/api/courses/course/lectures/lecture/prepare?backend=${backend}`, { method: "POST" })
  })

  it("keeps Qwen as the compatibility default", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
    vi.stubGlobal("fetch", fetch)
    await workspaceApi.runPreparation("course", "lecture")
    expect(fetch).toHaveBeenCalledWith("/api/courses/course/lectures/lecture/prepare?backend=qwen", { method: "POST" })
  })
})

describe("post-class backend selection", () => {
  it.each(["qwen", "codex"] as const)("forwards %s independently for each stage", async (backend) => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
    vi.stubGlobal("fetch", fetch)
    await workspaceApi.runAgent1("c", "l", "s", backend)
    await workspaceApi.runAgent2("c", "l", "s", backend)
    await enrichmentApi("c", "l", "s").run(backend)
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      `/api/courses/c/lectures/l/sessions/s/agent1/run?backend=${backend}`,
      `/api/courses/c/lectures/l/sessions/s/agent2/run?backend=${backend}`,
      `/api/courses/c/lectures/l/sessions/s/agent2/enrichment/run?backend=${backend}`,
    ])
  })
})
