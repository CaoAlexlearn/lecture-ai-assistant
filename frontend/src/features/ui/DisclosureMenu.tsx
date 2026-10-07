import { useEffect, useRef, type ReactNode } from "react"

export function DisclosureMenu({ label, trigger, children, className = "" }: { label: string; trigger: ReactNode; children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDetailsElement>(null)
  useEffect(() => {
    const closeOutside = (event: PointerEvent) => {
      if (ref.current?.open && !ref.current.contains(event.target as Node)) ref.current.open = false
    }
    document.addEventListener("pointerdown", closeOutside)
    return () => document.removeEventListener("pointerdown", closeOutside)
  }, [])
  return <details ref={ref} className={`ui-disclosure-menu ${className}`} onKeyDown={(event) => {
    if (event.key === "Escape" && ref.current?.open) { event.preventDefault(); ref.current.open = false; ref.current.querySelector("summary")?.focus() }
  }}>
    <summary aria-label={label}>{trigger}</summary>
    <div className="ui-disclosure-menu__content" onClick={(event) => {
      if (event.target instanceof Element && event.target.closest("a, button") && ref.current) {
        ref.current.open = false
        if (event.target.closest("button")) ref.current.querySelector("summary")?.focus()
      }
    }}>{children}</div>
  </details>
}
