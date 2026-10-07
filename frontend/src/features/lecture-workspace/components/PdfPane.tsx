import { useEffect, useId, useState, type ChangeEvent, type DragEvent } from "react"
import { PdfCanvasViewer } from "./PdfCanvasViewer"

type Props = {
  id?: string
  fileName: string
  pdfUrl?: string
  pageNumber: number
  notice?: string
  expanded: boolean
  onFile: (file: File) => void
  onPageChange: (page: number) => void
  onToggleExpanded: () => void
}

function isPdf(file: File): boolean {
  return file.type === "application/pdf" || file.name.toLocaleLowerCase().endsWith(".pdf")
}

export function PdfPane({
  id,
  fileName,
  pdfUrl,
  pageNumber,
  notice,
  expanded,
  onFile,
  onPageChange,
  onToggleExpanded,
}: Props) {
  const inputId = useId()
  const [pageCount, setPageCount] = useState<number>()

  useEffect(() => setPageCount(undefined), [pdfUrl])

  const acceptFile = (file?: File) => {
    if (file && isPdf(file)) onFile(file)
  }

  const handleInput = (event: ChangeEvent<HTMLInputElement>) => {
    acceptFile(event.target.files?.[0])
    event.target.value = ""
  }

  const handleDrop = (event: DragEvent<HTMLElement>) => {
    event.preventDefault()
    acceptFile(event.dataTransfer.files[0])
  }

  return (
    <section
      id={id}
      className={`pdf-pane ${notice ? "has-notice" : ""}`}
      aria-label="Lecture PDF"
      onDragOver={(event) => event.preventDefault()}
      onDrop={handleDrop}
    >
      <header className="pdf-pane__header">
        <div>
          <strong title={fileName}>{fileName}</strong>
        </div>
        <div className="pdf-pane__actions">
          <button
            className="pdf-expand"
            type="button"
            aria-label={expanded ? "Restore workspace" : "Maximize PDF"}
            aria-pressed={expanded}
            onClick={onToggleExpanded}
          >
            <svg viewBox="0 0 20 20" aria-hidden="true">
              {expanded ? (
                <path d="M8 3v5H3m9 9v-5h5M3 8l5-5m4 14 5-5" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" />
              ) : (
                <path d="M7.5 3H3v4.5M12.5 3H17v4.5M7.5 17H3v-4.5m9.5 4.5H17v-4.5" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" />
              )}
            </svg>
            {expanded ? "Restore" : "Maximize"}
          </button>
          <label className="pdf-upload" htmlFor={inputId}>
            <input id={inputId} type="file" accept=".pdf,application/pdf" onChange={handleInput} />
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <path d="M10 13V3m0 0L6.5 6.5M10 3l3.5 3.5M4 11v4.5A1.5 1.5 0 0 0 5.5 17h9a1.5 1.5 0 0 0 1.5-1.5V11" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" />
            </svg>
            Upload PDF
          </label>
        </div>
      </header>

      {notice && <p className="pdf-pane__notice">{notice}</p>}

      <div className="pdf-pane__viewer" title="Pinch to zoom · drag to pan · knowledge nodes jump to slides.">
        {pdfUrl ? (
          <PdfCanvasViewer pdfUrl={pdfUrl} pageNumber={pageNumber} onDocumentLoad={setPageCount} />
        ) : (
          <label className="pdf-empty" htmlFor={inputId}>
            <span aria-hidden="true">PDF</span>
            <strong>Drop a lecture PDF here</strong>
            <small>or choose a file from your computer</small>
          </label>
        )}
      </div>

      <footer className="pdf-pane__footer">
        <button type="button" aria-label="Previous slide" onClick={() => onPageChange(Math.max(1, pageNumber - 1))} disabled={pageNumber <= 1}>←</button>
        <span>Slide <strong>{pageNumber}</strong></span>
        <button
          type="button"
          aria-label="Next slide"
          onClick={() => onPageChange(pageCount ? Math.min(pageCount, pageNumber + 1) : pageNumber + 1)}
          disabled={pageCount !== undefined && pageNumber >= pageCount}
        >→</button>
      </footer>
    </section>
  )
}
