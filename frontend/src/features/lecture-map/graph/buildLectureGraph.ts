import ELK from "elkjs/lib/elk.bundled.js"
import type { Edge, Node } from "@xyflow/react"
import type {
  LectureGraphState,
  LectureNodeData,
  LectureOverview,
  LectureSection,
  LectureSelection,
  LectureSubtopic,
} from "../types/lectureMap"

export type LectureFlowNode = Node<LectureNodeData, "lectureNode">

const SECTION_COLORS = [
  "#677f86",
  "#76759a",
  "#8a705f",
  "#637e70",
  "#7c7088",
  "#64758f",
  "#87745e",
  "#6c7d70",
]

type SizedNode = Omit<LectureFlowNode, "position"> & { width: number; height: number }

function compactLectureTitle(title: string): string {
  const part = title.match(/Part\s+[A-Z]:\s*(.+)$/i)?.[1] ?? title
  return part.replace(/\bChap(?:ter)?\s*(\d+)\s*/i, "Chapter $1 · ").replace(/\s+/g, " ").trim()
}

function orderedSections(overview: LectureOverview): LectureSection[] {
  return [...overview.sections].sort((left, right) => left.orderIndex - right.orderIndex)
}

function orderedSubtopics(section: LectureSection): LectureSubtopic[] {
  return [...section.subtopics].sort((left, right) => left.orderIndex - right.orderIndex)
}

function estimatedHeight(title: string, charactersPerLine: number, base: number, lineHeight: number, maxLines: number): number {
  const lines = Math.min(maxLines, Math.max(1, Math.ceil(title.length / charactersPerLine)))
  return base + (lines - 1) * lineHeight
}

function sectionHeight(title: string): number {
  return estimatedHeight(title, 35, 78, 16, 3)
}

function subtopicHeight(title: string): number {
  return estimatedHeight(title, 43, 58, 15, 2)
}

function isVisible(section: LectureSection, state: LectureGraphState): boolean {
  return state.expandAll || state.expandedSectionId === section.id
}

function nodeData(overview: LectureOverview, state: LectureGraphState): SizedNode[] {
  const rootSelected = !state.selectedSectionId
  const nodes: SizedNode[] = [{
    id: "lecture-root",
    type: "lectureNode",
    width: 276,
    height: 108,
    style: { width: 276, height: 108 },
    data: {
      kind: "root",
      label: compactLectureTitle(overview.title),
      eyebrow: "Lecture map",
      accent: "#506a70",
      selected: rootSelected,
      activePath: true,
      dimmed: false,
      selection: { kind: "root" },
    },
  }]

  orderedSections(overview).forEach((section, sectionIndex) => {
    const accent = SECTION_COLORS[sectionIndex % SECTION_COLORS.length]
    const sectionSelected = state.selectedSectionId === section.id
    const sectionSelection: LectureSelection = { kind: "section", sectionId: section.id }
    const height = sectionHeight(section.title)
    nodes.push({
      id: section.id,
      type: "lectureNode",
      width: 292,
      height,
      style: { width: 292, height },
      data: {
        kind: "section",
        label: section.title,
        eyebrow: section.kind === "context" ? "Context" : String(section.number ?? sectionIndex + 1).padStart(2, "0"),
        slideLabel: section.slideRange?.label,
        accent,
        selected: sectionSelected && !state.selectedSubtopicId,
        activePath: sectionSelected,
        dimmed: Boolean(state.selectedSectionId && !sectionSelected),
        selection: sectionSelection,
      },
    })

    if (!isVisible(section, state)) return
    orderedSubtopics(section).forEach((subtopic) => {
      const topicSelection: LectureSelection = {
        kind: "subtopic",
        sectionId: section.id,
        subtopicId: subtopic.id,
      }
      const topicSelected = state.selectedSubtopicId === subtopic.id
      const topicHeight = subtopicHeight(subtopic.title)
      nodes.push({
        id: subtopic.id,
        type: "lectureNode",
        width: 310,
        height: topicHeight,
        style: { width: 310, height: topicHeight },
        data: {
          kind: "subtopic",
          label: subtopic.title,
          slideLabel: subtopic.slideRange.label,
          accent,
          selected: topicSelected,
          activePath: topicSelected,
          dimmed: Boolean(state.expandAll && state.selectedSectionId && !sectionSelected),
          selection: topicSelection,
        },
      })
    })
  })
  return nodes
}

function graphEdges(overview: LectureOverview, state: LectureGraphState): Edge[] {
  return orderedSections(overview).flatMap((section, sectionIndex) => {
    const accent = SECTION_COLORS[sectionIndex % SECTION_COLORS.length]
    const sectionActive = state.selectedSectionId === section.id
    const edges: Edge[] = [{
      id: `edge-root-${section.id}`,
      source: "lecture-root",
      target: section.id,
      type: "bezier",
      animated: false,
      style: {
        stroke: accent,
        strokeWidth: sectionActive ? 2.3 : 1.45,
        opacity: sectionActive ? 0.86 : state.selectedSectionId ? 0.28 : 0.46,
        transition: "opacity 180ms ease, stroke-width 180ms ease",
      },
    }]

    if (!isVisible(section, state)) return edges
    orderedSubtopics(section).forEach((subtopic) => {
      const active = state.selectedSubtopicId === subtopic.id
      edges.push({
        id: `edge-${section.id}-${subtopic.id}`,
        source: section.id,
        target: subtopic.id,
        type: "bezier",
        animated: false,
        style: {
          stroke: accent,
          strokeWidth: active ? 2 : 1.2,
          opacity: active ? 0.82 : sectionActive ? 0.46 : 0.25,
          transition: "opacity 180ms ease, stroke-width 180ms ease",
        },
      })
    })
    return edges
  })
}

export async function buildLectureGraph(
  overview: LectureOverview,
  state: LectureGraphState,
): Promise<{ nodes: LectureFlowNode[]; edges: Edge[] }> {
  const sourceNodes = nodeData(overview, state)
  const edges = graphEdges(overview, state)
  const elk = new ELK()
  const layout = await elk.layout({
    id: "lecture-map",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.alignment": "CENTER",
      "elk.spacing.nodeNode": "18",
      "elk.layered.spacing.nodeNodeBetweenLayers": "98",
      "elk.layered.spacing.edgeNodeBetweenLayers": "28",
      "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
      "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
      "org.eclipse.elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
      "org.eclipse.elk.layered.crossingMinimization.forceNodeModelOrder": "true",
    },
    children: sourceNodes.map((node) => ({
      id: node.id,
      width: node.width,
      height: node.height,
    })),
    edges: edges.map((edge) => ({
      id: edge.id,
      sources: [edge.source],
      targets: [edge.target],
    })),
  })

  const positions = new Map(layout.children?.map((node) => [node.id, node]) ?? [])
  return {
    nodes: sourceNodes.map(({ width: _width, height: _height, ...node }) => {
      const position = positions.get(node.id)
      return {
        ...node,
        position: { x: position?.x ?? 0, y: position?.y ?? 0 },
      }
    }),
    edges,
  }
}
