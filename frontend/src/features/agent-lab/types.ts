export type PromptStatus = "draft" | "active" | "archived"

export type PromptVersion = {
  id: string
  agentId: string
  version: number
  content?: string
  status: PromptStatus
  createdAt: string
  updatedAt: string
  note?: string
}

export type AgentDefinition = {
  agentId: string
  name: string
  description: string
  executable: boolean
  promptEditable: boolean
  activePromptVersionId?: string | null
  modelConfig: {
    provider?: string
    model?: string
    temperature?: number | null
    maxOutputTokens?: number | null
    readOnly?: boolean
  }
  contextPolicy: string[]
  versions: PromptVersion[]
}

export type ContextBlock = {
  key: string
  label: string
  role: string
  content: string
  characterCount: number
  tokenCount: number
  sentToModel: boolean
  scope?: "faculty" | "user" | "course" | "lecture"
  versionId?: string
  source?: string
}

export type ResolvedContext = {
  agentId: string
  promptVersionId: string
  courseId: string
  lectureId: string
  blocks: ContextBlock[]
  input: Record<string, unknown>
  modelConfig: AgentDefinition["modelConfig"]
  instructionSnapshot?: {
    loadOrder: string[]
    precedence: string[]
    instructions: Array<Record<string, unknown>>
  }
}

export type AgentTestRun = {
  id: string
  agentId: string
  promptVersionId: string
  courseId: string
  lectureId: string
  output: string
  validation: { status: string }
  model: string
  latencyMs: number
  usage?: Record<string, number> | null
  createdAt: string
  resolvedContext: ResolvedContext
}
