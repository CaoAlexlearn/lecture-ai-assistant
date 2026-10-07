import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { LectureWorkspacePage } from "./features/lecture-workspace/components/LectureWorkspacePage"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <LectureWorkspacePage />
  </StrictMode>,
)
