import type { CSSProperties } from "react"
import { Handle, Position, type NodeProps } from "@xyflow/react"
import type { LectureFlowNode } from "../graph/buildLectureGraph"

export function LectureNode({ data }: NodeProps<LectureFlowNode>) {
  const classes = [
    "lecture-node",
    `lecture-node--${data.kind}`,
    data.selected ? "is-selected" : "",
    data.activePath ? "is-active-path" : "",
    data.dimmed ? "is-dimmed" : "",
  ].filter(Boolean).join(" ")

  return (
    <div className={classes} style={{ "--branch-accent": data.accent } as CSSProperties}>
      {data.kind !== "root" && <Handle type="target" position={Position.Left} className="lecture-node__handle" />}

      {data.kind === "root" && (
        <>
          <span className="lecture-node__eyebrow">{data.eyebrow}</span>
          <strong>{data.label}</strong>
          <span className="lecture-node__hint">Explore the lecture</span>
        </>
      )}

      {data.kind === "section" && (
        <>
          <div className="lecture-node__section-meta">
            <span className="lecture-node__section-number">{data.eyebrow}</span>
            {data.slideLabel && <span>{data.slideLabel}</span>}
          </div>
          <strong>{data.label}</strong>
        </>
      )}

      {data.kind === "subtopic" && (
        <>
          <span className="lecture-node__topic-line" aria-hidden="true" />
          <div>
            <strong>{data.label}</strong>
            <span>{data.slideLabel}</span>
          </div>
        </>
      )}

      {data.kind !== "subtopic" && <Handle type="source" position={Position.Right} className="lecture-node__handle" />}
    </div>
  )
}
