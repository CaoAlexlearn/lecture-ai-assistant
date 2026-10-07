import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { WorkspaceApp } from "./features/workspace/WorkspaceApp"

createRoot(document.getElementById("root")!).render(<StrictMode><WorkspaceApp /></StrictMode>)
