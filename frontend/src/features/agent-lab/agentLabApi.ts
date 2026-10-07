import type { AgentDefinition, AgentTestRun, PromptVersion, ResolvedContext } from "./types"

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options)
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: string }
    throw new Error(body.detail || `Request failed (${response.status}).`)
  }
  return response.json() as Promise<T>
}

const json = (value: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(value),
})

export const agentLabApi = {
  listAgents: async () => (await request<{ agents: AgentDefinition[] }>("/api/agent-lab/agents")).agents,
  getAgent: (agentId: string) => request<AgentDefinition>(`/api/agent-lab/agents/${agentId}`),
  updateModel: (agentId: string, model: string) => request<AgentDefinition>(`/api/agent-lab/agents/${agentId}/model-config`, {
    ...json({ model }),
    method: "PUT",
  }),
  listVersions: async (agentId: string) => (await request<{ versions: PromptVersion[] }>(`/api/agent-lab/agents/${agentId}/versions`)).versions,
  createDraft: (agentId: string, sourceVersionId?: string) => request<PromptVersion>(`/api/agent-lab/agents/${agentId}/drafts`, json({ sourceVersionId })),
  saveDraft: (agentId: string, versionId: string, content: string, expectedUpdatedAt: string, note: string) => request<PromptVersion>(`/api/agent-lab/agents/${agentId}/versions/${versionId}`, {
    ...json({ content, expectedUpdatedAt, note }),
    method: "PUT",
  }),
  activate: (agentId: string, versionId: string) => request<AgentDefinition>(`/api/agent-lab/agents/${agentId}/versions/${versionId}/activate`, { method: "POST" }),
  restore: (agentId: string, versionId: string) => request<PromptVersion>(`/api/agent-lab/agents/${agentId}/versions/${versionId}/restore`, { method: "POST" }),
  compare: (agentId: string, base: string, target: string) => request<{ diff: string; added: number; removed: number }>(`/api/agent-lab/agents/${agentId}/compare?base=${encodeURIComponent(base)}&target=${encodeURIComponent(target)}`),
  resolveContext: (value: Record<string, unknown>) => request<ResolvedContext>("/api/agent-lab/context/resolve", json(value)),
  runTest: (value: Record<string, unknown>) => request<AgentTestRun>("/api/agent-lab/tests", json(value)),
  listTests: async (agentId: string) => (await request<{ tests: AgentTestRun[] }>(`/api/agent-lab/tests?agent_id=${encodeURIComponent(agentId)}`)).tests,
}
