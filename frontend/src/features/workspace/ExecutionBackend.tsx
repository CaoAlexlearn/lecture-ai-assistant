import { useState } from "react"
import "./execution-backend.css"

export type ExecutionBackend = "qwen" | "codex"

export function useExecutionBackend(stage: string) {
  const key = `classroom.execution-backend.${stage}`
  const [backend, setValue] = useState<ExecutionBackend>(() => {
    try { return localStorage.getItem(key) === "codex" ? "codex" : "qwen" } catch { return "qwen" }
  })
  const setBackend = (value: ExecutionBackend) => {
    setValue(value)
    try { localStorage.setItem(key, value) } catch { /* Selection remains usable without storage. */ }
  }
  return { backend, setBackend }
}

export function ExecutionBackendSelect({ label, value, onChange, disabled, provider, model }: {
  label: string; value: ExecutionBackend; onChange: (value: ExecutionBackend) => void
  disabled?: boolean; provider?: string; model?: string
}) {
  const source = provider === "codex" ? "Codex" : provider === "alibaba_cloud_model_studio" || model?.startsWith("qwen") ? "Qwen" : undefined
  return <div className="execution-backend">
    <label>{label}运行方式<select aria-label={`${label}运行方式`} value={value} disabled={disabled}
      title={value === "codex" ? "使用已登录的 ChatGPT 账户额度；失败时不会自动切换到 Qwen。" : "使用 Qwen API。选择后点击运行才会开始。"}
      onChange={(event) => onChange(event.target.value as ExecutionBackend)}>
      <option value="qwen">Qwen API</option><option value="codex">Codex · ChatGPT 账户额度</option>
    </select></label>
    {model && <small title={model}>实际执行：{source ? `${source} · ` : ""}{model}</small>}
  </div>
}
