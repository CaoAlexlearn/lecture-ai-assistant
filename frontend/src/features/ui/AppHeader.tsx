import type { ReactNode } from "react"
import { Icon } from "./Icon"
import { DisclosureMenu } from "./DisclosureMenu"
import "./app-header.css"

type Props = { active?: "courses" | "agent-lab" | "instructions"; subtitle?: string; children?: ReactNode; className?: string }

export function AppHeader({ active = "courses", subtitle = "Your learning workspace", children, className = "" }: Props) {
  return <header className={`app-header ${className}`}>
    <a className="app-brand" href="/courses" aria-label="Classroom Assistant home">
      <span className="app-brand__mark"><Icon name="book" /></span>
      <span className="app-brand__text"><strong>Classroom Assistant</strong><small>{subtitle}</small></span>
    </a>
    <nav className="app-nav" aria-label="Global navigation">
      <a href="/courses" aria-current={active === "courses" ? "page" : undefined}><Icon name="courses" />Courses</a>
      <DisclosureMenu label="Settings" className={active !== "courses" ? "app-settings is-current" : "app-settings"} trigger={<><Icon name="settings" />Settings</>}>
        <a href="/agent-lab" aria-current={active === "agent-lab" ? "page" : undefined}><Icon name="settings" />Agent Lab</a>
        <a href="/instructions" aria-current={active === "instructions" ? "page" : undefined}><Icon name="instructions" />Instructions</a>
      </DisclosureMenu>
    </nav>
    {children && <div className="app-header__context">{children}</div>}
  </header>
}
