import { readFile } from "node:fs/promises"
import { resolve } from "node:path"
import { defineConfig, loadEnv, type Plugin } from "vite"
import react from "@vitejs/plugin-react"

const LECTURE_FILES = ["lecture_overview.md", "slides_preparation.md"] as const

type LectureDataSource = {
  key: string
  preparedDir: string
  pdfPath: string
}

function lectureDataPlugin(sources: LectureDataSource[]): Plugin {
  const sourceFiles = new Map<string, { path: string; contentType: string }>(
    sources.flatMap((source) => [
      ...LECTURE_FILES.map((name) => [
        `/${source.key}/${name}`,
        { path: resolve(source.preparedDir, name), contentType: "text/markdown; charset=utf-8" },
      ] as const),
      [
        `/${source.key}/original.pdf`,
        { path: source.pdfPath, contentType: "application/pdf" },
      ] as const,
    ]),
  )

  return {
    name: "lecture-map-data",
    configureServer(server) {
      server.middlewares.use("/lecture-data", (request, response, next) => {
        if (!request.url || !["GET", "HEAD"].includes(request.method ?? "GET")) return next()
        const requestPath = new URL(request.url, "http://localhost").pathname
        const sourceFile = sourceFiles.get(requestPath)
        if (!sourceFile) return next()

        void readFile(sourceFile.path)
          .then((source) => {
            response.statusCode = 200
            response.setHeader("Content-Type", sourceFile.contentType)
            response.setHeader("Cache-Control", "no-store")
            if (sourceFile.contentType === "application/pdf") {
              response.setHeader("Content-Disposition", "inline")
            }
            response.end(request.method === "HEAD" ? undefined : source)
          })
          .catch(() => {
            response.statusCode = 404
            response.setHeader("Content-Type", "text/plain; charset=utf-8")
            response.end(`Lecture source not found: ${requestPath}`)
          })
      })
    },

  }
}

function workspaceRoutesPlugin(): Plugin {
  return {
    name: "workspace-routes",
    configureServer(server) {
      server.middlewares.use((request, _response, next) => {
        if (request.url) {
          const pathname = new URL(request.url, "http://localhost").pathname
          if (pathname === "/agent-lab" || pathname === "/instructions" || /^\/courses(?:\/|$)/.test(pathname)) {
            request.url = "/workspace.html"
          }
        }
        next()
      })
    },
  }
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "")
  // Local lecture assets are opt-in and are never included in a production build.
  const sources: LectureDataSource[] = env.LECTURE_MAP_PREPARED_DIR && env.LECTURE_MAP_PDF_PATH
    ? [{ key: "open-systems/networks-introduction",
         preparedDir: resolve(process.cwd(), env.LECTURE_MAP_PREPARED_DIR),
         pdfPath: resolve(process.cwd(), env.LECTURE_MAP_PDF_PATH) }]
    : []

  return {
    plugins: [workspaceRoutesPlugin(), react(), lectureDataPlugin(sources)],
    build: {
      rollupOptions: {
        input: {
          classroom: "index.html",
          lectureMap: "lecture-map.html",
          lectureWorkspace: "lecture-workspace.html",
          workspace: "workspace.html",
        },
      },
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        "/api": { target: "http://127.0.0.1:8765" },
        "/ws": { target: "ws://127.0.0.1:8765", ws: true },
      },
    },
  }
})
