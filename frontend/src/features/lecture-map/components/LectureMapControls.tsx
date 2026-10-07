import { useReactFlow, useStore } from "@xyflow/react"

type Props = {
  onOpenDetails: () => void
  detailsOpen: boolean
  expandAll: boolean
  onToggleExpandAll: () => void
}

export function LectureMapControls({ onOpenDetails, detailsOpen, expandAll, onToggleExpandAll }: Props) {
  const instance = useReactFlow()
  const zoom = useStore((state) => state.transform[2])

  const changeZoom = (next: number) => {
    const clamped = Math.max(0.25, Math.min(1.6, next))
    void instance.zoomTo(clamped, { duration: 180 })
  }

  return (
    <div className="lecture-map-controls" aria-label="Mind map controls">
      {!detailsOpen && (
        <button className="lecture-map-controls__details" type="button" onClick={onOpenDetails}>
          Details
        </button>
      )}
      <button
        className="lecture-map-controls__expand"
        type="button"
        aria-pressed={expandAll}
        onClick={onToggleExpandAll}
      >
        {expandAll ? "Collapse all" : "Expand all"}
      </button>
      <span className="lecture-map-controls__divider" />
      <button
        type="button"
        className="lecture-map-controls__fit"
        onClick={() => void instance.fitView({ padding: 0.14, duration: 420 })}
      >
        Fit view
      </button>
      <span className="lecture-map-controls__divider" />
      <button type="button" aria-label="Zoom out" onClick={() => changeZoom(zoom - 0.1)}>−</button>
      <output aria-label="Current zoom">{Math.round(zoom * 100)}%</output>
      <button type="button" aria-label="Zoom in" onClick={() => changeZoom(zoom + 0.1)}>+</button>
    </div>
  )
}
