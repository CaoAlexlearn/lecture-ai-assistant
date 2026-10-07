# Lecture AI Assistant

大学课堂实时转写、翻译与 AI 笔记助手。仓库包含应用代码、通用 Agent 提示词及模拟测试，不包含真实课件、课堂录音、转写、笔记、个人指令或 API 密钥。

A university lecture assistant for real-time transcription, translation, and AI-assisted study notes. This repository contains application code, generic agent prompts, and mock-based tests. Actual course materials, recordings, transcripts, notes, personal instructions, and API credentials remain local.

## Current implementation / 当前实现

- **Realtime / 实时课堂:** browser microphone → 16 kHz PCM WebSocket → bundled WhisperLiveKit with Qwen streaming ASR → stable source text and Qwen translation.
- **Preparation / 课前准备:** MinerU/PDF evidence extraction, knowledge scaffold, and course-specific ASR hotwords. Preparation supports Qwen API or isolated Codex CLI calls.
- **Post-class / 课后处理:** Agent1 segmentation and summaries; Agent2 evidence retrieval, audited ASR corrections, and knowledge enrichment.
- **Context / 上下文:** scoped instructions, prompt versions, local course/session artifacts, and resumable caches.

The current snapshot uses Qwen and Codex integrations. Claude Code subagent orchestration and WhisperX are not implemented in this snapshot. Agent3 is a non-executable placeholder rather than a completed cross-course memory service.

当前版本接入 Qwen 与 Codex；尚未实现 Claude Code Subagent 调度或 WhisperX 链路。Agent3 目前为不可执行的占位定义，跨课程 Memory 服务尚未完成。

## Setup / 安装

Use Python 3.12 and Node.js/npm. MinerU is optional for document conversion and is installed separately.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
npm --prefix frontend ci
npm --prefix frontend run build
python main.py
```

Set your own credentials in `.env`, including a Beijing-region `DASHSCOPE_API_KEY`. Open http://127.0.0.1:8765/courses to create a course and lecture, upload your materials, and prepare a classroom session. Optional Codex stages require an installed Codex CLI with ChatGPT login. There is no automatic provider fallback.

在本地 `.env` 填写自己的密钥，再打开课程页面创建课程、讲次并上传课件。所有课堂内容仅保存在本地的 `courses/`、`data/` 等目录中；这些目录被 Git 忽略。使用 Codex 阶段时需另行安装 Codex CLI 并登录。

## Verification / 验证

Install `pytest` and `pytest-asyncio` to run backend tests:

```bash
pip install pytest pytest-asyncio
python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
```

Regression files depending on actual lecture snapshots are omitted from this upload. No production build bundles local lecture PDFs or preparation outputs. Live microphone, paid model calls, MinerU, and optional ASR backends require separate local configuration and verification.

依赖真实课堂快照的回归测试未纳入上传；前端构建不会打包本地 PDF 或课前产物。实时麦克风、真实模型请求及可选 ASR 后端需要另行配置和验证。

## Privacy / 隐私

This is a fresh source snapshot with no original Git history. Keep secrets, personal instructions, classroom content, logs, and model responses out of commits. `.gitignore` does not remove files that have already been committed. Do not copy private runtime directories into this repository.

本仓库使用全新提交，不包含原项目历史。不要将本地课堂目录或运行记录复制进仓库；已经提交过的文件不会因 `.gitignore` 而自动移除。

## Third-party code / 第三方代码

The bundled `whisperlivekit/` tree includes third-party ASR code and assets. Preserve its existing `LICENSE.whisperlivekit` notice. Optional backends may require packages beyond the minimal application requirements. No project-wide license is selected for this snapshot.
