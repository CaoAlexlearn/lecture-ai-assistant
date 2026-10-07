import { useState } from "react"
import { KnowledgeTree } from "../knowledge-scaffold/KnowledgeTree"
import { KnowledgeNodeInspector } from "../knowledge-scaffold/KnowledgeNodeInspector"
import { firstContentNodeId, type KnowledgeScaffold } from "../knowledge-scaffold/scaffold"

export function PreparationKnowledgePreview({ scaffold, materialUrl }: { scaffold: KnowledgeScaffold; materialUrl?: string | null }) {
  const [selectedId, setSelectedId] = useState(() => firstContentNodeId(scaffold))
  const node = scaffold.nodes[selectedId ?? ""] ?? scaffold.nodes[firstContentNodeId(scaffold) ?? ""]
  if (!node) return <p className="prepare-knowledge-empty">No knowledge nodes are available yet.</p>
  return <div className="prepare-knowledge-preview">
    <KnowledgeTree scaffold={scaffold} selectedNodeId={node.node_id} onSelect={setSelectedId} />
    <KnowledgeNodeInspector key={node.node_id} scaffold={scaffold} node={node} onSelect={setSelectedId}
      onSlideFocus={(page) => { if (materialUrl) window.open(`${materialUrl.split("#")[0]}#page=${page}`, "_blank", "noopener,noreferrer") }} />
  </div>
}
