import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { LectureMapPage } from "./features/lecture-map/components/LectureMapPage"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <LectureMapPage />
  </StrictMode>,
)
