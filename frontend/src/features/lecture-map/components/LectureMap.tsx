import { useEffect, useMemo, useRef, useState } from "react"
import {
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type NodeMouseHandler,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import { buildLectureGraph, type LectureFlowNode } from "../graph/buildLectureGraph"
import type {
  LectureGraphState,
  LectureMapData,
  LectureSelection,
} from "../types/lectureMap"
import { DetailPanel } from "./DetailPanel"
import { LectureMapControls } from "./LectureMapControls"
import { LectureNode } from "./LectureNodes"

const NODE_TYPES = { lectureNode: LectureNode }

function Canvas({ data, onSlideFocus }: { data: LectureMapData; onSlideFocus?: (slideNumber: number) => void }) {
  const flow = useReactFlow<LectureFlowNode>()
  const [selectedSectionId, setSelectedSectionId] = useState<string>()
  const [selectedSubtopicId, setSelectedSubtopicId] = useState<string>()
  const [expandedSectionId, setExpandedSectionId] = useState<string>()
  const [expandAll, setExpandAll] = useState(false)
  const [nodes, setNodes] = useState<LectureFlowNode[]>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [detailsOpen, setDetailsOpen] = useState(true)
  const previousStructure = useRef<string | undefined>(undefined)

  const graphState = useMemo<LectureGraphState>(() => ({
    selectedSectionId,
    selectedSubtopicId,
    expandedSectionId,
    expandAll,
  }), [expandAll, expandedSectionId, selectedSectionId, selectedSubtopicId])

  const selection = useMemo<LectureSelection>(() => {
    if (selectedSectionId && selectedSubtopicId) {
      return { kind: "subtopic", sectionId: selectedSectionId, subtopicId: selectedSubtopicId }
    }
    if (selectedSectionId) return { kind: "section", sectionId: selectedSectionId }
    return { kind: "root" }
  }, [selectedSectionId, selectedSubtopicId])

  useEffect(() => {
    let current = true
    const structureKey = expandAll ? "all" : expandedSectionId ?? "collapsed"
    const structureChanged = previousStructure.current !== structureKey

    void buildLectureGraph(data.overview, graphState).then((graph) => {
      if (!current) return
      setNodes(graph.nodes)
      setEdges(graph.edges)
      previousStructure.current = structureKey

      if (structureChanged) {
        window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
          void flow.fitView({
            nodes: graph.nodes,
            padding: expandAll ? 0.12 : 0.16,
            minZoom: expandAll ? 0.25 : 0.62,
            maxZoom: 1.05,
            duration: 440,
          })
        }))
      }
    })
    return () => { current = false }
  }, [data.overview, expandAll, expandedSectionId, flow, graphState])

  const selectNode = (next: LectureSelection) => {
    const section = next.kind === "root"
      ? data.overview.sections[0]
      : data.overview.sections.find((candidate) => candidate.id === next.sectionId)
    const slideNumber = next.kind === "subtopic"
      ? section?.subtopics.find((candidate) => candidate.id === next.subtopicId)?.slideRange.start
      : section?.slideRange?.start
    if (slideNumber) onSlideFocus?.(slideNumber)

    if (next.kind === "root") {
      setSelectedSectionId(undefined)
      setSelectedSubtopicId(undefined)
      if (!expandAll) setExpandedSectionId(undefined)
    } else if (next.kind === "section") {
      setSelectedSectionId(next.sectionId)
      setSelectedSubtopicId(undefined)
      if (!expandAll) setExpandedSectionId(next.sectionId)
    } else {
      setSelectedSectionId(next.sectionId)
      setSelectedSubtopicId(next.subtopicId)
      if (!expandAll) setExpandedSectionId(next.sectionId)
    }
    setDetailsOpen(true)
  }

  const select: NodeMouseHandler<LectureFlowNode> = (_event, node) => selectNode(node.data.selection)

  const toggleExpandAll = () => {
    if (expandAll) {
      setExpandAll(false)
      setExpandedSectionId(undefined)
      setSelectedSubtopicId(undefined)
    } else {
      setExpandAll(true)
      setExpandedSectionId(undefined)
    }
  }

  return (
    <div className={`lecture-map-workspace ${detailsOpen ? "has-details" : ""}`}>
      <section className="lecture-map-canvas" aria-label="Interactive lecture mind map">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          onNodeClick={select}
          nodesDraggable={false}
          nodesConnectable={false}
          edgesReconnectable={false}
          elementsSelectable
          panOnDrag
          zoomOnDoubleClick={false}
          minZoom={0.25}
          maxZoom={1.6}
          proOptions={{ hideAttribution: true }}
        >
          <LectureMapControls
            detailsOpen={detailsOpen}
            onOpenDetails={() => setDetailsOpen(true)}
            expandAll={expandAll}
            onToggleExpandAll={toggleExpandAll}
          />
        </ReactFlow>
      </section>
      <DetailPanel
        data={data}
        selection={selection}
        open={detailsOpen}
        onClose={() => setDetailsOpen(false)}
        onSelect={selectNode}
      />
    </div>
  )
}

export function LectureMap({ data, onSlideFocus }: { data: LectureMapData; onSlideFocus?: (slideNumber: number) => void }) {
  return (
    <ReactFlowProvider>
      <Canvas data={data} onSlideFocus={onSlideFocus} />
    </ReactFlowProvider>
  )
}
