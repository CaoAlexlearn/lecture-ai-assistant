import { useEffect, useState, type ReactNode } from "react"
import { defaultExpandedIds, slideLabel, sortedChildren, type KnowledgeNode, type KnowledgeScaffold } from "./scaffold"

type Props = {
  scaffold: KnowledgeScaffold
  selectedNodeId?: string
  onSelect: (id: string) => void
  compact?: boolean
  headerAction?: ReactNode
}

export function KnowledgeTree({ scaffold, selectedNodeId, onSelect, compact = false, headerAction }: Props) {
  const [expandedIds, setExpandedIds] = useState(() => new Set(defaultExpandedIds(scaffold)))
  const branchIds = Object.values(scaffold.nodes).filter((node) => node.children.length).map((node) => node.node_id)

  useEffect(() => {
    if (!selectedNodeId) return
    setExpandedIds((current) => {
      const next = new Set(current)
      let parentId = scaffold.nodes[selectedNodeId]?.parent_id
      while (parentId) {
        next.add(parentId)
        parentId = scaffold.nodes[parentId]?.parent_id ?? null
      }
      return next
    })
  }, [scaffold, selectedNodeId])

  const toggle = (id: string) => setExpandedIds((current) => {
    const next = new Set(current)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

  const renderNode = (node: KnowledgeNode, depth: number) => {
    const expanded = expandedIds.has(node.node_id)
    const hasChildren = node.children.length > 0
    const selected = selectedNodeId === node.node_id
    return (
      <li className="knowledge-tree__item" key={node.node_id}>
        <div className={`knowledge-tree__row ${selected ? "is-selected" : ""} ${depth === 0 ? "is-root" : ""}`}>
          {hasChildren ? (
            <button className="knowledge-tree__toggle" type="button" onClick={() => toggle(node.node_id)}
              aria-label={`${expanded ? "Collapse" : "Expand"} ${node.title}`} aria-expanded={expanded}> {expanded ? "⌄" : "›"} </button>
          ) : <span className="knowledge-tree__leaf" aria-hidden="true" />}
          <button className="knowledge-tree__select" type="button" onClick={() => onSelect(node.node_id)}
            aria-current={selected ? "true" : undefined} title={node.title}>
            <strong>{node.title}</strong>
            <span className="knowledge-tree__meta">
              <em className={`knowledge-tree__type is-${node.node_type}`}>{node.node_type}</em>
              {slideLabel(node) && <small>{slideLabel(node)}</small>}
            </span>
          </button>
        </div>
        {hasChildren && expanded && (
          <ul className="knowledge-tree__children">
            {sortedChildren(scaffold, node.children).map((child) => renderNode(child, depth + 1))}
          </ul>
        )}
      </li>
    )
  }

  return (
    <section className={`knowledge-tree ${compact ? "knowledge-tree--compact" : ""}`} aria-label="Knowledge Tree">
      <header className="knowledge-tree__header">
        <div className="knowledge-tree__title"><div><span className="knowledge-panel__eyebrow">Lecture knowledge</span><h1>{compact ? "Knowledge" : "Knowledge Tree"}</h1><p>{scaffold.title}</p></div>{headerAction}</div>
        <div className="knowledge-tree__controls">
          <button type="button" onClick={() => setExpandedIds(new Set(branchIds))}>Expand all</button>
          <button type="button" onClick={() => setExpandedIds(new Set())}>Collapse all</button>
        </div>
      </header>
      <div className="knowledge-tree__scroll">
        <ul className="knowledge-tree__roots">{sortedChildren(scaffold, scaffold.root_nodes).map((node) => renderNode(node, 0))}</ul>
      </div>
      <footer className="knowledge-tree__footer">{Object.keys(scaffold.nodes).length} knowledge nodes{!compact && " · Select a node to inspect it"}</footer>
    </section>
  )
}
