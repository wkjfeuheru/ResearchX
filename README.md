# ResearchX

> **PostgreSQL cutover / 数据库切换**：启动前必须配置 `RESEARCHX_DATABASE_URL` 并执行 `uv run python -m researchx.storage.migrate`。旧版用户先备份和显式导入；不再读取 JSON/SQLite 业务状态。见 [部署、Schema、历史导入与恢复](docs/POSTGRESQL.md)。测试使用独立 `RESEARCHX_TEST_DATABASE_URL`。

ResearchX is a local investment research Web workspace. Each conversation owns its task context, research state, evidence pool and auditable reasoning summaries. Sources carry immutable snapshots, timestamps and stable IDs; answers freeze the versions they cite.

[中文说明](README.zh-CN.md) · [Web workspace](docs/DIRECTORY_MIGRATION.md#工作区与-web-边界) · [Contributing](CONTRIBUTING.md)

`oh eval` runs a versioned 200-case research benchmark with real runtime traces, independent judging and four-dimensional reports. Langfuse is an optional evaluation dependency; regular conversations are not uploaded. See [evaluation setup and workflow](docs/agent-evaluation.md).

## Install and start

Python 3.10 or newer is required.

```bash
pip install 'researchx-ai[web]'
rx setup
rx web
```

Open the local address printed by `rx web`. The server accepts local hosts and checks browser origins. Credentials stay on the backend and are redacted from browser output.

The Web workspace streams events with FastAPI SSE and sends commands through HTTP POST. Disconnecting stops the active run and saves partial output; reopening restores its snapshot without replaying messages. See [SSE and command contracts](docs/DIRECTORY_MIGRATION.md#工作区与-web-边界).

Internal modules now separate storage, security, research documents, workspace files and Web session coordination. Tools are flat modules, and document ingestion uses `python -m researchx.workspace.documents`. See the [directory migration and import mapping](docs/DIRECTORY_MIGRATION.md).

For a source checkout, build the frontend with Node.js 20 or newer:

```bash
uv sync --extra dev --extra web
cd frontend/web
npm ci
npm run build
cd ../..
uv run rx web
```

Install from PyPI or source. `rx`, `oh` and `openh` share the same CLI; on PowerShell use `openh web` or `oh.exe web` to avoid the built-in `oh` alias. If console launchers are not on PATH, use `python -m researchx web`.

## Research workflow

Complex questions produce a concise task outline and committed progress. Simple questions need no forced plan. Research tools read and search documents, fetch webpages, query configured MCP services, execute calculations and create files. Financial analysis, valuation, charts and report export belong in Skills using these shared tools. File edits and Shell operations retain permission and sandbox checks.

The research memory tool maintains evidence, provenance, conclusions, conflicts and auditable method summaries. Report objectives, plans and task state are controlled by ResearchAgentRuntime, TaskManager and CompletionPolicy; legacy memory planning operations remain available only outside report projects. A successful tool call does not verify a claim. Search summaries and user accounts remain pending verification until supported by explicit records. Corrections preserve history and mark dependent conclusions for review.

Use **Stop** to cancel generation, or **Interrupt and modify** to save a change request, finish cancellation and replan. Refreshing or restarting restores committed state and historical citations; it does not automatically resume a model run. Conversations do not inherit other conversations' research.

Report production can start an explicit `research_project` with a versioned objective. The main agent calls the registered `planner` and `replanner` tools; the existing loop's ResearchAgentRuntime validates their proposals, leases DAG tasks, isolates late results and checks evidence-backed artifacts before freezing delivery. Report drafts are downloadable Markdown artifacts. Ordinary questions and historical plans keep their existing path. See [core tool contracts and compatibility](docs/AGENT_CORE_TOOLS.md). The offline earnings commentary fixture can be run with `uv run pytest -q tests/test_research/test_earnings_workflow.py`.

Each report project binds an isolated directory with `MEMORY.md`, `artifacts/` and `reports/`. File tools maintain the Markdown notes. Model requests receive a bounded research index and can read `MEMORY.md` on demand. See [workspace configuration, file boundaries and recovery](docs/WORKSPACE_MEMORY.md).

`dispatch_subagents` delegates 1–16 independent assignments with bounded parallelism, separate histories and private output directories under `subagents/`. Children return candidates for main-agent review and cannot edit main `MEMORY.md` or authoritative task state. The research registry excludes `notebook_edit`, `config`, `mcp_auth`, `image_generation` and `sleep`; host configuration services remain available. The general registry compatibility argument uses the same retained research tools.

Unresolved effects block overlapping writes while read-only diagnostics remain available. Use `rx operations list --cwd ORIGINAL_WORKSPACE --session SESSION_ID` and, after independently verifying or compensating the effect, `rx operations resolve OPERATION_ID --cwd ORIGINAL_WORKSPACE --session SESSION_ID --outcome no_effect --evidence "verification record"`. This records the operator's decision without replaying a tool. Confirmed success requires a matching verified result receipt. See the [Chinese recovery instructions](README.zh-CN.md) for receipt fields and safety rules. Children stop after repeated attempts to modify authoritative state or a revoked parent scope. Evaluation reports task completion separately from citation coverage, retaining checks for required delivery, critical errors and invalid citations.

## Models, credentials and extensions

Existing Anthropic/OpenAI compatible APIs and Codex, Claude and Copilot subscription authentication are retained. Add API configurations in the Web model page or use the CLI. Subscription profiles can be selected and tested in Web; their authentication is managed by CLI:

```bash
oh auth login
oh auth codex-login
oh auth claude-login
oh auth copilot-login
oh auth status
oh provider list
oh provider use PROFILE
```

Financial data comes from your configured services and retrieved sources; ResearchX does not add a built-in live quote feed or trading service.

The official DeepSeek API IDs `deepseek-flash`, `deepseek-v4-pro`, and the Flash aliases `deepseek-v4-flash` and `deepseek-v4-flash-vision-exp` have a built-in 1,000,000-token context window ([official specifications](https://api-docs.deepseek.com/quick_start/pricing/)). Set the actual context window in the Web model form if your gateway has a lower limit; explicit `context_window_tokens` always takes precedence. Unknown models require an explicit window, and budget errors preserve the original conversation.

```bash
oh mcp list
oh mcp add --help
oh config show
oh config set research_memory.enabled false
```

Business extensions use individual Skills (`SKILL.md` with scripts, references and templates). Builtin, user and project Skills can be enabled independently. Compatibility directories include `.agents/skills` and `.claude/skills`; MCP services and hooks use independent settings.

Web search defaults to configured investment research sources, ordered by official disclosures/statistics/policy, industry professionals, then financial media. Broader web discovery requires an explicit scope. Eight builtin business Skills ship in `skills/bundled`, with individual switches, metadata-only discovery and on-demand loading. See [Skill modules and progressive loading](docs/skill-modules.md).

Research recall defaults to a 1,500-token summary and at most 12 indexed records. `MEMORY.md` is read on demand through file tools. Disabling recall does not disable research persistence or execution checks. Legacy memory context budgets migrate only when no top-level or active-model budget is set. Existing user data is not automatically removed.

The personal assistant product, messaging integrations, coding orchestration, terminal UI, scheduling and old long-term/vector memory are retired. Only the research Web runtime and administrative CLI remain.

## Validation

The deterministic Agent core integration suite covers parallel delegation, memory updates, replanning, cancellation/recovery and report delivery through the existing loop. See the [E2E-01–16 test report and coverage](docs/testing/agent-core-e2e-report.md) and [remaining limits](docs/testing/agent-core-risks.md).

Agent Shell and Skill calculations require SRT 0.0.79 or the configured Docker backend; unavailable backends reject execution. Explicit trusted host mode is available only to the main agent outside report projects. See [shell policy, exports and recovery](docs/BACKEND_STABILIZATION.md). Run `uv run python tools/check_types.py` for strict checks of all production Python, including bundled scripts.

```bash
uv run pytest -q
uv run ruff check src tests tools evals hatch_build.py
cd frontend/web
npm run build
npx playwright install chromium
npm run test:e2e
```

Real model verification is separate from deterministic tests. See `tests/test_web/real_research_eval.py --help` for the two-turn Web evaluation with source collection, calculations, citations, browser checks and restart recovery. Run it with an existing configured profile in a disposable workspace.

[Cleanup scope and validation report](docs/research-product-cleanup.md)

Developer reference: [Harness contracts, permissions, retry and recovery](docs/HARNESS_EXECUTION.md), [backend stabilization](docs/BACKEND_STABILIZATION.md), and [validation results](docs/testing/backend-stability-validation.md).
