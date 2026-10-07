import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type WheelEvent as ReactWheelEvent,
} from "react"
import { GlobalWorkerOptions, getDocument, type PDFDocumentProxy } from "pdfjs-dist/legacy/build/pdf.mjs"
import pdfWorkerUrl from "pdfjs-dist/legacy/build/pdf.worker.min.mjs?url"

GlobalWorkerOptions.workerSrc = pdfWorkerUrl

const MIN_ZOOM = 1
const MAX_ZOOM = 4

type Point = { x: number; y: number }
type Pan = Point

type Props = {
  pdfUrl: string
  pageNumber: number
  onDocumentLoad: (pageCount: number) => void
}

type Gesture =
  | { kind: "drag"; pointer: Point; pan: Pan }
  | { kind: "pinch"; distance: number; center: Point; zoom: number; pan: Pan }

function clampZoom(value: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value))
}

function distance(left: Point, right: Point): number {
  return Math.hypot(left.x - right.x, left.y - right.y)
}

function center(left: Point, right: Point): Point {
  return { x: (left.x + right.x) / 2, y: (left.y + right.y) / 2 }
}

export function PdfCanvasViewer({ pdfUrl, pageNumber, onDocumentLoad }: Props) {
  const stageRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const pointers = useRef(new Map<number, Point>())
  const gesture = useRef<Gesture | undefined>(undefined)
  const zoomRef = useRef(1)
  const panRef = useRef<Pan>({ x: 0, y: 0 })
  const [document, setDocument] = useState<PDFDocumentProxy>()
  const [stageSize, setStageSize] = useState({ width: 0, height: 0 })
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState<Pan>({ x: 0, y: 0 })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")

  const updatePan = (next: Pan) => {
    panRef.current = next
    setPan(next)
  }

  const updateZoom = (value: number) => {
    const next = clampZoom(value)
    zoomRef.current = next
    setZoom(next)
    if (next === MIN_ZOOM) updatePan({ x: 0, y: 0 })
  }

  const resetView = () => {
    updateZoom(1)
    updatePan({ x: 0, y: 0 })
  }

  useEffect(() => {
    const stage = stageRef.current
    if (!stage) return
    const update = () => setStageSize({ width: stage.clientWidth, height: stage.clientHeight })
    update()
    const observer = new ResizeObserver(update)
    observer.observe(stage)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    let active = true
    const loadingTask = getDocument({ url: pdfUrl })
    setLoading(true)
    setError("")
    setDocument(undefined)
    resetView()

    void loadingTask.promise
      .then((nextDocument) => {
        if (!active) return
        setDocument(nextDocument)
        onDocumentLoad(nextDocument.numPages)
      })
      .catch((caught: unknown) => {
        if (active) setError(caught instanceof Error ? caught.message : "The PDF could not be opened.")
      })

    return () => {
      active = false
      void loadingTask.destroy()
    }
  }, [onDocumentLoad, pdfUrl])

  useEffect(() => {
    if (!document || !canvasRef.current || stageSize.width === 0 || stageSize.height === 0) return
    let active = true
    let renderTask: ReturnType<Awaited<ReturnType<PDFDocumentProxy["getPage"]>>["render"]> | undefined

    void document.getPage(Math.min(document.numPages, Math.max(1, pageNumber)))
      .then((page) => {
        if (!active || !canvasRef.current) return
        const unscaled = page.getViewport({ scale: 1 })
        const availableWidth = Math.max(1, stageSize.width - 8)
        const availableHeight = Math.max(1, stageSize.height - 8)
        const fitScale = Math.min(availableWidth / unscaled.width, availableHeight / unscaled.height)
        const outputScale = Math.min(window.devicePixelRatio || 1, 2)
        const viewport = page.getViewport({ scale: fitScale * outputScale })
        const canvas = canvasRef.current
        const context = canvas.getContext("2d", { alpha: false })
        if (!context) throw new Error("Canvas rendering is unavailable.")
        canvas.width = Math.ceil(viewport.width)
        canvas.height = Math.ceil(viewport.height)
        canvas.style.width = `${viewport.width / outputScale}px`
        canvas.style.height = `${viewport.height / outputScale}px`
        setLoading(true)
        renderTask = page.render({ canvas, canvasContext: context, viewport })
        return renderTask.promise
      })
      .then(() => {
        if (active) setLoading(false)
      })
      .catch((caught: unknown) => {
        if (active && !(caught instanceof Error && caught.name === "RenderingCancelledException")) {
          setError(caught instanceof Error ? caught.message : "The PDF page could not be rendered.")
        }
      })

    return () => {
      active = false
      renderTask?.cancel()
    }
  }, [document, pageNumber, stageSize])

  const beginGesture = (event: ReactPointerEvent<HTMLDivElement>) => {
    if ((event.target as Element).closest(".pdf-zoom-controls")) return
    event.currentTarget.setPointerCapture(event.pointerId)
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY })
    const active = [...pointers.current.values()]
    if (active.length >= 2) {
      gesture.current = {
        kind: "pinch",
        distance: Math.max(1, distance(active[0], active[1])),
        center: center(active[0], active[1]),
        zoom: zoomRef.current,
        pan: panRef.current,
      }
    } else {
      gesture.current = { kind: "drag", pointer: active[0], pan: panRef.current }
    }
  }

  const moveGesture = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!pointers.current.has(event.pointerId)) return
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY })
    const active = [...pointers.current.values()]
    const currentGesture = gesture.current

    if (active.length >= 2 && currentGesture?.kind === "pinch") {
      const currentCenter = center(active[0], active[1])
      const ratio = distance(active[0], active[1]) / currentGesture.distance
      const nextZoom = clampZoom(currentGesture.zoom * ratio)
      zoomRef.current = nextZoom
      setZoom(nextZoom)
      updatePan({
        x: currentGesture.pan.x + currentCenter.x - currentGesture.center.x,
        y: currentGesture.pan.y + currentCenter.y - currentGesture.center.y,
      })
    } else if (active.length === 1 && currentGesture?.kind === "drag" && zoomRef.current > 1) {
      updatePan({
        x: currentGesture.pan.x + active[0].x - currentGesture.pointer.x,
        y: currentGesture.pan.y + active[0].y - currentGesture.pointer.y,
      })
    }
  }

  const endGesture = (event: ReactPointerEvent<HTMLDivElement>) => {
    pointers.current.delete(event.pointerId)
    const active = [...pointers.current.values()]
    gesture.current = active.length === 1
      ? { kind: "drag", pointer: active[0], pan: panRef.current }
      : undefined
  }

  const zoomWithWheel = (event: ReactWheelEvent<HTMLDivElement>) => {
    if (!event.ctrlKey && !event.metaKey) return
    event.preventDefault()
    updateZoom(zoomRef.current * Math.exp(-event.deltaY * 0.008))
  }

  return (
    <div
      className={`pdf-canvas-stage ${zoom > 1 ? "is-zoomed" : ""}`}
      ref={stageRef}
      onPointerDown={beginGesture}
      onPointerMove={moveGesture}
      onPointerUp={endGesture}
      onPointerCancel={endGesture}
      onWheel={zoomWithWheel}
      aria-label="Touch-enabled PDF viewer"
    >
      <div
        className="pdf-canvas-transform"
        style={{ transform: `translate3d(${pan.x}px, ${pan.y}px, 0) scale(${zoom})` }}
      >
        <canvas ref={canvasRef} />
      </div>

      {(loading || error) && (
        <div className={`pdf-canvas-state ${error ? "has-error" : ""}`} role={error ? "alert" : "status"}>
          {error || "Rendering slide…"}
        </div>
      )}

      <div className="pdf-zoom-controls" aria-label="PDF zoom controls">
        <button type="button" aria-label="Zoom PDF out" onClick={() => updateZoom(zoomRef.current - .25)} disabled={zoom <= MIN_ZOOM}>−</button>
        <output aria-label="PDF zoom level">{Math.round(zoom * 100)}%</output>
        <button type="button" aria-label="Zoom PDF in" onClick={() => updateZoom(zoomRef.current + .25)} disabled={zoom >= MAX_ZOOM}>+</button>
        <button type="button" className="pdf-zoom-fit" onClick={resetView} disabled={zoom === 1 && pan.x === 0 && pan.y === 0}>Fit</button>
      </div>
    </div>
  )
}
