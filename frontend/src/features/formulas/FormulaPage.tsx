import { AppHeader } from "../ui/AppHeader"
import { useEffect, useMemo, useState } from "react"
import type { WorkspaceCourse, WorkspaceLecture } from "../workspace/types"
import { request } from "../workspace/workspaceApi"
import { MathFormula } from "./MathFormula"
import "./formulas.css"

type Original = { id: string; expression: string; pages: number[]; block_refs: { page: number; block_id: string }[] }
type Formula = { id: string; title: string; group: string; expression: string; display_expressions: string[]; display_source: string; pages: number[]; variables: { symbol: string; meaning: string }[]; interpretation: string | null; assumptions: string[]; source_formulas: Original[] }
type Collection = { formulas: Formula[]; originals: Original[]; source_block_count: number; has_normalized_source: boolean }

export function FormulaPage({ course, lecture }: { course: WorkspaceCourse; lecture: WorkspaceLecture }) {
  const [collection, setCollection] = useState<Collection>()
  const [error, setError] = useState("")
  const [search, setSearch] = useState("")
  const [tab, setTab] = useState<"knowledge" | "source">("knowledge")
  useEffect(() => {
    let active = true
    setCollection(undefined); setError(""); setSearch("")
    request<Collection>(`/api/courses/${encodeURIComponent(course.id)}/lectures/${encodeURIComponent(lecture.id)}/formulas`)
      .then((value) => { if (active) setCollection(value) }).catch((caught: unknown) => { if (active) setError(caught instanceof Error ? caught.message : "公式暂时无法打开") })
    return () => { active = false }
  }, [course.id, lecture.id])
  const matches = (value: unknown) => JSON.stringify(value).toLocaleLowerCase().includes(search.trim().toLocaleLowerCase())
  const formulas = collection?.formulas.filter(matches) ?? []
  const originals = collection?.originals.filter(matches) ?? []
  const groups = useMemo(() => [...new Set(formulas.map((row) => row.group))], [formulas])
  const base = `/courses/${encodeURIComponent(course.id)}/lectures/${encodeURIComponent(lecture.id)}`
  const pages = (values: number[]) => <div className="formula-sources">{values.map((page) => <a key={page} href={lecture.urls.material ? `${lecture.urls.material}#page=${page}` : undefined} target="_blank" rel="noreferrer">PPT 第 {page} 页 ↗</a>)}</div>
  return <main className="formula-page">
    <AppHeader subtitle="Formula collection"><a className="workspace-header-link" href={`${base}/live`}>返回课堂工作区</a></AppHeader>
    <div className="formula-content"><a className="formula-back" href={`/courses/${course.id}`}>← {course.title}</a><div className="formula-heading"><div><span>LECTURE {lecture.number} · FORMULA COLLECTION</span><h1>本讲公式</h1><p>{lecture.title}</p></div><button onClick={() => window.print()}>打印 / 保存 PDF</button></div>
      <p className="formula-note">汇总已有课件解析和公式知识点，不重新生成公式。重复原式合并来源；解析可能有 OCR 误差，图片中的公式也可能遗漏，请以来源 PPT 为准。</p>
      {error && <p role="alert" className="prepare-error">{error}</p>}
      {!collection && !error && <p role="status">正在汇总本讲公式…</p>}
      {collection && <><div className="formula-toolbar"><div role="tablist" aria-label="公式视图"><button role="tab" aria-selected={tab === "knowledge"} onClick={() => setTab("knowledge")}>知识点公式 <span>{collection.formulas.length}</span></button><button role="tab" aria-selected={tab === "source"} onClick={() => setTab("source")}>课件原式 <span>{collection.originals.length}</span></button></div><input aria-label="搜索公式" placeholder="搜索名称、公式、变量或页码…" value={search} onChange={(event) => setSearch(event.target.value)} /></div>
      {!collection.has_normalized_source && <p className="formula-note">当前讲次没有保留原始公式证据，仅展示现有知识点公式。</p>}
      {tab === "knowledge" ? groups.map((group) => <section className="formula-group" key={group}><h2>{group}</h2><div className="formula-grid">{formulas.filter((row) => row.group === group).map((row) => <article className="formula-card" key={row.id}><header><span>{row.display_source === "slide" ? "PPT 原式" : "知识点公式"}</span><h3>{row.title}</h3>{pages(row.pages)}</header>{row.display_expressions.map((expression, index) => <MathFormula key={index} expression={expression} originalLatex={row.display_source === "slide"} />)}{row.interpretation && <p>{row.interpretation}</p>}{row.variables.length > 0 && <details><summary>变量说明 · {row.variables.length}</summary><dl>{row.variables.map((variable) => <div key={variable.symbol}><dt>{variable.symbol}</dt><dd>{variable.meaning}</dd></div>)}</dl></details>}{row.assumptions.length > 0 && <details><summary>使用条件</summary><ul>{row.assumptions.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}{row.source_formulas.length > 0 && <details><summary>查看来源页全部原式 · {row.source_formulas.length}</summary>{row.source_formulas.map((source) => <div key={source.id}><MathFormula expression={source.expression} originalLatex />{pages(source.pages)}</div>)}</details>}</article>)}</div></section>) : <div className="formula-grid">{originals.map((row) => <article className="formula-card" key={row.id}><header><span>PPT 原式 · {row.block_refs.length} 处来源</span>{pages(row.pages)}</header><MathFormula expression={row.expression} originalLatex /><details><summary>原始表达式与来源编号</summary><pre>{row.expression}</pre><small>{row.block_refs.map((ref) => ref.block_id).join(" · ")}</small></details></article>)}</div>}
      {(tab === "knowledge" ? formulas : originals).length === 0 && <div className="formula-empty"><h2>{search ? "没有匹配的公式" : "暂无公式"}</h2><p>{search ? "换一个关键词，或切换到课件原式查找。" : "本讲尚未识别到该类公式，可切换另一视图查看。"}</p></div>}</>}
    </div>
  </main>
}
