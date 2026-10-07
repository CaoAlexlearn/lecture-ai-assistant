import { useEffect } from "react"

/** Redirect the retired standalone classroom entry to the session-owned UI. */
export default function App() {
  useEffect(() => {
    window.location.replace("/courses")
  }, [])

  return <main className="app-shell">Opening the course workspace…</main>
}
