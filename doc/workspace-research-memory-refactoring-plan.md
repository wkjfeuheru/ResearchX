# 工作区研究记忆与执行状态拆分方案

日期：2026-10-14

## 1. 目标与明确约定

将当前 `ResearchMemory` 拆成**工作上下文、执行状态、情节记忆**，研究文件继续由产物存储管理。

已确认：

- 一个工作区对应一个研究项目，可以有多个对话；对话共享工作区内的研究记录。
- 继续使用 PostgreSQL，本次不引入 MongoDB。
- 工作上下文只存在于内存；聊天记录、压缩摘要和审计记录可独立持久化，用于恢复。
- 每轮自动注入少量摘要与索引，详细研究记录通过工具按需读取。
- 保留并迁移已有研究数据。
- 本次建设通用基础，不实现因子研究、策略生成或回测功能。

## 2. 模块职责

| 模块 | 职责 | 持久化方式 |
|---|---|---|
| `WorkingContext` | 表示当前一次模型调用实际可见的全部信息 | 仅内存 |
| `ExecutionState` | 管理目标、计划、任务、运行、租约、版本和完成条件 | PostgreSQL |
| `EpisodicMemory` | 保存来源、证据、分析记录、结论、争议、决策和任务成果 | PostgreSQL |
| `ArtifactStore` | 保存文档、代码、数据与结果文件 | 复用现有文件／内容存储 |

**工作上下文**包括系统提示词、工具定义、对话摘要、近期消息、工具调用及结果、当前任务摘要和已读取的情节记录。工具结果被压缩或移出上下文后，不再属于工作记忆，其原始记录可以继续留存。

**执行状态**是 runtime 的权威依据。任务是否完成、是否允许提交结果，不能由对话摘要或研究笔记决定。

**情节记忆**按工作区组织，保留所属任务、来源对话、时间、版本与引用关系。“推理链”保存可审计的分析方法、假设、输入、结果及验证记录。

任务状态与任务成果分离：状态归执行模块，成果归情节记忆，成果正文或大文件归产物存储。

## 3. 主要修改

### 工作区与对话关系

- 将现有按会话组织的研究聚合改为按工作区组织；对话通过 `workspace_id` 访问同一项目。
- 各对话保持独立聊天历史和工作上下文；共享持久化研究成果，不自动互相注入聊天内容。
- 并行对话通过现有版本检查、任务租约和幂等操作协调写入；冲突时重新读取状态，不覆盖他人的更新。
- 项目级改目标、重规划仍由 runtime 执行，并使受影响的旧任务租约失效。

### 持久化与读取接口

- 拆解 `ResearchMemory` 的混合职责，不再通过全量聚合读取来构建提示词。
- 执行模块提供当前执行摘要及受校验的状态转换接口。
- 情节记忆模块提供查询、按 ID 批量读取、追加记录和版本化修订接口；支持按任务、记录类型、状态与关键词筛选，并分页返回。
- 保留现有来源核验、证据替代、争议处理和审核失效规则。执行状态与成果提交需要共同成功时，使用同一 PostgreSQL 事务。
- 为成果保留类型、输入引用、产物引用和验证结果；后续因子、策略和回测增加各自结构与验收规则，不复用报告章节作为通用验收标准。

### 上下文组装与恢复

每次调用遵循以下流程：

`加载当前对话 → 读取当前任务摘要与成果索引 → 按预算组装 WorkingContext → 调用模型 → 执行工具并保存结果 → 组装下一次上下文`

- 自动摘要只包含项目目标、当前任务、关键约束、待解决事项及相关成果索引，不自动加载整个证据池。
- 模型通过查询工具读取详细记录；返回值进入当前对话的工具结果。
- 复用现有 token 预算和压缩机制，保证工具调用与结果配对；摘要保留必要记录 ID，便于再次读取。
- 恢复时从聊天存档、压缩摘要和最新执行状态重建上下文，重新检查租约与版本。
- `MEMORY.md` 保留为研究笔记，按需读取；其内容不能修改任务状态或将证据标记为已核验。
- 调整上下文统计：区分“本次可见的记忆片段”和“数据库中的情节记录总量”。

## 4. 迁移与实施顺序

1. **建立工作区归属。** 将现有目录标识与业务工作区区分开；每个已有项目迁入独立工作区，关联原对话，避免因为原来使用相同目录而误合并项目。没有项目的历史研究会话各自建立工作区。
2. **迁移研究记录。** 保留记录 ID、来源快照、引用关系、版本、操作账本和成果；为记录补齐工作区及来源对话归属。
3. **拆分读写路径。** 先使执行状态与情节记录通过独立接口访问，再替换 runtime 和研究工具的调用。
4. **切换上下文构建。** 使用少量摘要与索引注入，移除旧聚合的自动注入路径。
5. **校验后切换。** 迁移期间暂停研究写入，备份数据库与文件，校验记录数量、引用完整性和内容哈希；失败则保留旧系统继续使用。生产切换后不维持双写。

服务重启后沿用手动恢复研究的行为；能够重建工作上下文，不意味着自动重新执行任务。

## 5. 验收标准

- 同一工作区两个对话可检索并引用彼此产生的成果，聊天上下文保持独立；不同工作区相互隔离。
- 压缩或清空工作上下文不会删除研究记录，也不会改变任务状态。
- 重启后能重建上下文，且无法使用已失效租约提交结果。
- 并发写入、重复提交和版本冲突不会造成覆盖、重复成果或部分提交。
- 证据修订后，依赖它的结论与审核状态按原规则失效。
- 大量历史记录不会被每轮全量加载；详细记录可通过分页和 ID 定位读取。
- 迁移前后的来源、证据、成果和引用可核对，已有报告仍能追溯。
- 深度报告生成、研报解析摘要及现有引用、核验、交付流程通过回归验证。

---

## 6. 代码核查结论与问题定位

以下结论基于 2026-10-14 工作区当前代码，包括已有未提交修改。第 1—5 节为总体方案，本节及后续章节为实施细化。下面标注“拟新增”的类型、接口、配置和命令均为设计，不表示已经实现。

### 6.1 现状不是“研究数据只放在内存”

目前研究实体已经分表保存在 PostgreSQL，部分正文由 ContentStore 保存；`ResearchMemory` 是从数据库重建的领域聚合。真正的问题是聚合范围、会话归属和读取路径，而非缺少持久化。

| 问题 | 涉及模块／文件／函数 | 当前行为与修改方向 |
|---|---|---|
| 研究聚合混合多类职责 | `src/researchx/state/models.py::ResearchMemory` | 同时包含计划、执行、证据、结论、历史、操作回执和对话回答；拆成执行、情节、会话记录 |
| 上下文准备依赖全量读取 | `state/store.py::load/_load/_validate/_validate_content/prompt_snapshot`；`storage/research_records.py::_snapshot_query/load` | 全量加载并校验聚合，再生成有长度限制的提示词；改成查询阶段就限制范围 |
| 自动注入证据与笔记 | `services/context/sources.py::compose_research_context`；`state/runtime.py::build_research_context` | 读取研究聚合、生成 prompt，并读取 `MEMORY.md`；改为执行摘要与成果索引 |
| 工作区身份混同目录 | `storage/database.py::workspace_id`；`state/store.py::ResearchStore.__init__` | 用 cwd 哈希确定 workspace；无法表达同一启动目录下的多个独立业务工作区 |
| 项目目录依赖会话存储路径 | `state/runtime.py::_workspace_key/_workspace_path/initialize_workspace` | 工作目录身份包含 `store.directory`，间接依赖 session；迁为业务工作区稳定绑定 |
| 情节实体实际按会话隔离 | `storage/schema.py::scoped_table/entities/research_sessions`；`storage/research_records.py::scope/transaction/save` | 实体主键及外键包含 session，锁与版本也属于 session；拆出项目级主键和事务锁 |
| 当前任务是单一指针 | `state/models.py::ResearchState`；`state/repository.py::begin_execution`；`state/runtime.py::dispatch_scope` | 执行使用 `current_task_id`；共享聚合后会使不同对话争用同一指针，须绑定到 run |
| 已有资产共享基础但未贯通 | `storage/schema.py` 的资产与绑定表；`storage/run_records.py::bind_conversation/conversation_project/record_accepted_run` | 已存在项目—对话、项目—资产关系，但 run 可退回以 session 充当 project；统一为真实项目绑定 |
| 删除对话可能级联删除研究实体 | `storage/conversations.py::ConversationRecords.delete`；`schema.py::scoped_table` | 删除 `research_sessions` 会级联删除依附它的研究实体；已有工作区观察值保留测试不能覆盖证据、计划与产物 |
| 会话页面读取会触发研究恢复 | `web/workspace.py::WebWorkspace.record`；`state/repository.py::recover` | 基于当前会话是否 idle 决定恢复；共享后可能错误中断另一个对话的运行 |
| 引擎与应用接口仍依赖具体聚合 | `engine/query_engine.py::_current_runtime_snapshot`；`research_application.py::prepare_user_turn/admit_tool/get_control_view` | 部分接口已有窄查询，但其他路径继续读整份状态；统一通过应用模块提供有界视图 |
| 交付、引用和文件访问隐含 session 归属 | `state/deliveries.py::DeliveryService`；`state/store.py::render_answer/invalid_citations`；`workspace/session_files.py` | 改为工作区资产权限与来源对话信息分离，保持引用和审核的原有语义 |
| 恢复快照重新推导目录哈希 | `services/context/snapshots.py::_scope/save_context_snapshot`；`services/sessions/storage.py`；`storage/conversations.py` | 显式传入业务工作区身份，避免存档落回旧目录作用域 |

上表除第一行外，路径均相对于 `src/researchx/`。主要入口是 `runtime.py` 中构造 `ResearchStore(cwd, session_id)` 的位置，以及 Web 中重复构造 ResearchStore 的调用点。

### 6.2 根因分析

1. **会话是最初的生命周期单位。** 项目、文件、任务与证据都随 session 聚合；后续新增工作区级资产没有消除旧的归属模型。
2. **一个模型同时承担持久化视图、执行校验输入和提示词来源。** 对外输出虽然受 token 限制，但数据库读取、内容存在性校验和领域校验仍可能随历史数据增长。
3. **任务控制依赖隐式“当前值”。** `current_task_id` 在一个会话串行推进时可行，无法直接提升为多个对话共享的项目字段。
4. **会话级原子性掩盖了跨会话需求。** 原有 CAS、操作账本和租约机制可保留，但锁定对象、幂等键及失效范围需要调整。
5. **执行事实与知识摘要缺少明确区分。** 摘要用于提示词，任务状态用于控制，证据与成果用于追溯；它们不能共享“memory”这一含混的接口语义。

## 7. 目标模型与数据归属

### 7.1 身份与生命周期

- `workspace_id`：业务工作区稳定 ID，由程序生成，不再由路径计算。一个工作区只对应一个项目；保留独立 `project_id` 以沿用历史引用，数据库强制一对一。
- `host_root`：启动目录／工作区集合所在目录，只用于定位本机工作区集合，不作为研究数据隔离键。
- `workspace_path`：该业务工作区的文件目录，由宿主管理并持久化；迁移时允许保留既有安全目录，不要求按新 ID 重命名。
- `session_id`：一个工作区内的对话 ID；绑定建立后不支持直接跨工作区搬移。
- `run_id`：一次可恢复的运行。归属于工作区、项目和发起对话，并持有自身当前任务绑定。
- `execution_id`：一次工具执行；归属于 run 和 task，携带提交时必须核对的版本与租约。

新建工作区时原子创建项目壳记录，尚未确认目标时保持 `created`。`research_project.start` 用于初始化该项目目标；在已有项目中更改目标走 feedback／replan，另一个独立项目必须创建另一个工作区。普通问答不要求创建计划或领取研究任务。

### 7.2 旧字段拆分规则

| 原字段 | 新归属 | 处理规则 |
|---|---|---|
| `project/objectives/plans/executions` | ExecutionState | 数据库持久化，应用模块负责状态转换 |
| `research_state.current_task_id` | RunBinding | 每个运行独立；任务租约标记 owner run |
| `current_plan_id/replan_required` | 项目控制状态 | 仍在项目级；运行保留其计划版本快照 |
| `task_context/current_context_id` | 分类迁移 | 原始上下文记录保留为历史研究记录；实际当前选择归 run，项目约束归目标版本 |
| `sources/evidence_pool/reasoning_chain/conclusions/conflicts/arbitrations/artifacts` | EpisodicMemory | 按工作区保存，保留任务和来源对话；正文使用内容引用 |
| `history/operations` | 操作审计与幂等账本 | 持久化但不参与默认记忆召回；不可替代领域实体 |
| `answers/pending_steers` | 会话／运行记录 | 回答展示属于对话；已冻结交付物属于工作区；控制消息绑定目标 run |
| 聊天消息、压缩摘要、调用存档 | ConversationArchive | 可恢复工作上下文，不作为执行状态权威 |

情节记忆不是只存自然语言事件的单表。继续使用现有来源、证据、分析、结论等类型化实体表及 JSONB payload；统一查询视图只暴露摘要与引用，不复制完整正文。

已有 `research_observations/research_model_runs/research_quality_certificates/asset_dependencies` 继续作为专用资产表使用，不再建立第二套内容相同的成果库。

### 7.3 拟新增类型示例

以下示例表达接口约束；字段沿用现有模型命名，实际实现时复用既有消息和请求类型。

```python
@dataclass(frozen=True)
class ResearchScope:
    workspace_id: str
    project_id: str
    session_id: str
    run_id: str | None

@dataclass(frozen=True)
class WorkingContext:
    # 保存最终选定、经过预算校验的请求；不再复制另一份 messages。
    request: ApiMessageRequest
    provenance: tuple[ContextSpan, ...]
    workspace_id: str
    session_id: str
    run_id: str | None
    project_revision: int

class RunTaskBinding(BaseModel):
    run_id: str
    plan_id: str
    task_id: str
    task_revision: int
    objective_revision: int
    plan_revision: int
    execution_epoch: int
    lease_id: str

class EpisodeRef(BaseModel):
    record_type: str
    record_id: str
    version: int

class EpisodeSummary(BaseModel):
    ref: EpisodeRef
    task_id: str | None
    origin_session_id: str | None
    summary: str
    status: str
    needs_review: bool
    content_ref: str | None
```

`WorkingContext` 的 frozen 只保护外层引用；请求对象在交给 provider 前必须成为该次调用独占的快照，不允许后续刷新修改其嵌套消息。调用审计可保存序列化投影，但不建立“持久化 WorkingContext”表。

证据原有 `supersedes` 形成版本关系，不能为了统一 version 字段而重写既有证据 ID。统一引用中的版本取自实体版本；不可变旧记录无版本字段时使用版本 1。

## 8. 具体实施步骤与关键变更

### 步骤一：先改身份解析，建立独立工作区入口

**涉及：** `storage/database.py`、`storage/schema.py`、`runtime.py`、`web/app.py`、`web/workspace.py`、`web/models.py`、CLI 会话恢复入口。

1. 新增工作区目录注册／查找接口，存储稳定 ID、project ID、宿主根目录、已验证的 workspace path。
2. `ResearchStore(cwd, session_id)` 替换为显式 scope 注入。允许旧会话入口通过数据库绑定解析 scope，但禁止静默调用 `workspace_id(cwd)` 推导业务身份。
3. 现有 `WebWorkspace` 实际承担进程级会话调度，改名为宿主协调器或保留内部名字并明确职责；运行数上限仍为进程级，不能因新增工作区而被绕开。
4. 新增 `POST /api/workspaces`、`GET /api/workspaces`、`GET /api/workspaces/{workspace_id}`；`POST /api/sessions` 必须带 workspace ID，响应增加 workspace/project ID；会话列表支持 workspace 过滤。
5. 现有按 session 的命令、SSE 和下载地址保留，由服务端解析所属工作区。旧会话 ID 若存在多个候选绑定，返回明确冲突并要求带 workspace 参数，不能随意选第一条。
6. CLI 增加 `--workspace <id>`。恢复旧 session 时从绑定解析；新会话只有一个候选工作区时可直接使用，多个候选时列出并要求选择。

前端 `frontend/web/src/App.tsx`、`ChatPage.tsx`、`api.ts`、`useConversation.ts` 增加工作区选择与“在当前工作区新建对话”。项目进度共享，对话消息仍独立。

### 步骤二：拆分数据库所有权和级联关系

**涉及：** `storage/schema.py`、`storage/research_records.py`、`storage/conversations.py`、`storage/run_records.py`、新增 Alembic revision。

- `workspaces.workspace_id` 改为稳定业务 ID；添加 host root 信息；现有 canonical path 保存业务工作区文件根。
- 项目、计划、任务、来源、证据、结论、成果、研究执行与审核记录改为工作区所有。去掉以 session 为生命周期父节点的级联关系。
- 对话消息、压缩快照索引、展示回答和待处理对话控制消息仍归会话。仅这些记录可随删除对话级联删除。
- 共享记录的 `origin_session_id` 作为审计来源保留，不使用 `ON DELETE CASCADE`；删除对话后仍能展示来源 ID。
- 对话绑定表添加工作区—项目一致性约束；`bind_conversation` 遇到不同 project 的重复绑定必须报冲突，不能用 `on_conflict_do_nothing` 隐藏错误。
- `record_accepted_run` 取消 `project_id or session_id` 回退，所有研究运行必须解析真实项目。
- 执行状态 revision 与情节记录目录 revision 分离。领取任务核对任务版本和项目 epoch；检索结果携带目录 revision，避免每追加一条证据就使所有并行工具租约失效。

目标约束示例（示意 SQL，不直接对旧表执行）：

```sql
-- 每个 workspace 恰好一个项目，由创建事务及唯一约束共同保证。
UNIQUE (workspace_id)                         -- research_projects
PRIMARY KEY (workspace_id, record_id)         -- 证据等共享实体
PRIMARY KEY (workspace_id, session_id)        -- conversations
UNIQUE (workspace_id, project_id)             -- 用于复合外键
FOREIGN KEY (workspace_id, project_id)
  REFERENCES research_projects(workspace_id, project_id)
```

增加 `(workspace_id, task_id, created_at, record_id)` 和 `(workspace_id, status, record_id)` 查询索引。来源、结论、成果各表保留已有专用索引；不以单个无限增长 JSONB 存整个工作区。

新的迁移使用冻结的 DDL 定义，不导入持续变化的应用 metadata。现有 0007 的说明与 0001 实际实现存在差异：0001 已采用冻结 schema，新增迁移必须分别验证全新安装与从 0007 升级。同步更新 `Database.SCHEMA_REVISION`。

### 步骤三：拆分读写接口，保留一次提交的原子性

**涉及：** `state/store.py`、`state/repository.py`、`research_application.py`、`state/completion.py`、`state/conflicts.py`、`state/deliveries.py`。

拟新增执行查询与情节查询模块，外部只访问用途明确的接口：

```python
class ExecutionRepository:
    async def read_summary(self, scope: ResearchScope) -> ExecutionSummary: ...
    async def claim_task(self, scope, task_id, expected_task_revision) -> RunTaskBinding: ...

class EpisodicRepository:
    async def search(self, scope, query: EpisodeQuery) -> EpisodePage: ...
    async def get_many(self, scope, refs: list[EpisodeRef], *, include_content=False): ...

class ResearchApplication:
    async def context_summary(self, scope, *, max_items: int) -> ResearchContextSummary: ...
    async def commit_tool_result(self, scope, lease, result, operation_id): ...
```

查询接口不得先 `ResearchStore.load()` 再用 Python 筛选。读目标记录时批量取其必要依赖；任务验收读取该任务的输入、成果和引用闭包；项目验收可覆盖全部必需任务，但不读取无关聊天和原始大文件。

提交工具结果的事务顺序：

1. 工具运行和对象正文写入在数据库锁外完成，验证内容哈希后获得引用。
2. 开启数据库事务，检查操作回执；同 key、同请求返回原回执，不同请求报冲突。
3. 短暂锁定项目控制行，再锁定相关 task/run，核对 epoch、目标版本、任务版本、租约 owner 和 run 状态。
4. 写入来源／成果、内容引用注册与依赖关系，更新执行状态，执行必要的审核失效，写入审计和回执，一次提交。
5. 租约失效时拒绝结果进入有效研究资产；可保存拒绝审计，不把迟到结果作为已接受证据。

所有写事务使用固定的锁顺序：project → task（按 ID 排序）→ run → 相关记录。不得在这些锁中等待模型、网络或长计算。初版允许共享项目写入短暂串行，读和工具计算仍可并行。

幂等 key 使用 `(workspace_id, run_id, operation_id)`；无 run 的人工修改使用服务端分配的操作命名空间。旧回执保留原会话命名空间以支持追溯。来源 ID 生成纳入 run/execution，修复目前只依赖 `tool_use_id:index` 在跨对话共享后可能碰撞的问题。

`ResearchStore._validate` 按职责拆成记录引用校验、任务提交校验和项目验收校验。保留全量完整性检查用于迁移及诊断，不能放回每轮上下文热路径。

### 步骤四：将当前任务和取消范围绑定到 run

**涉及：** `state/repository.py::transition_task/begin_execution/execution_valid/commit_execution/recover`、`state/runtime.py::dispatch_scope/prepare_dispatch_scope/validate_dispatch_scope`、Web run 生命周期和子代理调度。

- 每个 run 同时绑定至多一个当前研究任务；一个任务同时只有一个活动 owner run，其他运行领取时得到冲突。
- 两个对话可以并行执行不同的 ready task。任务依赖和计划状态继续由现有 TaskManager 校验。
- 执行租约增加 workspace/session/run 身份，工具不能提供或覆盖这些宿主字段。
- `stop` 默认停止当前 run，释放其任务绑定；不会取消同工作区其他运行。项目 cancel、目标变化、项目级 replan 才撤销相关项目租约。
- 子代理继承主 run 的任务及版本；继续通过父级校验接纳候选成果，不赋予独立修改项目控制状态的能力。
- 进程启动时恢复所有未完成 run 为 paused，并撤销旧 lease；浏览器读历史记录只刷新显示，不执行项目级 recover。
- 保留原有并发容量与队列上限；服务重启后不自动重跑模型或回测。

### 步骤五：将 WorkingContext 收敛到最终请求

**涉及：** `engine/query.py::build_request`（循环内函数）、`engine/query_engine.py::_current_runtime_snapshot`、`services/context/sources.py::compose_research_context/refresh_runtime_messages`、`services/context/budget.py`、`services/compact/__init__.py`、`services/context/snapshots.py`。

改造后的读取链：

```text
ResearchScope
  ├─ 当前对话的摘要和近期消息
  ├─ 项目目标、版本、run 对应任务与关键约束
  └─ 当前任务／显式输入对应的成果索引
          ↓
compose_research_context（有界查询，无 MEMORY.md 正文）
          ↓
prepare_request → 预算与压缩 → 最终选定请求 → WorkingContext
          ↓
provider 调用；工具返回后生成下一次上下文
```

具体规则：

- 自动索引优先包含当前任务显式输入和成果，其次该任务最新记录，再使用剩余名额展示项目近期成果；按时间与 ID 稳定排序并去重。
- 无当前任务时注入项目目标、未解决事项摘要和近期成果索引。每条索引包含 ID、类型、简短摘要、状态、版本，不含全文。
- 当前用户消息、系统约束、工具调用配对不能为了容纳记忆索引而丢弃；预算不足时先减索引，保留读取工具和项目定位信息。
- 查询结果按显式过滤条件返回。初版不新增向量库、embedding 或自动语义召回；关键词查询支持中文原文子串，固定作用域和分页上限，使用参数化 SQL。
- `memory` token 分类保留为配置兼容键，含义明确为“可见的召回片段”；WorkingContext 总量是整个请求，不能只统计该分类。
- `refresh_runtime_messages` 只替换宿主拥有的摘要块，不修改普通聊天文本，不把历史 runtime 块重复追加。
- `save_context_snapshot` 保存的是恢复／审计投影；恢复时丢弃旧的宿主动态块并读取最新执行摘要，保留实际聊天和工具消息。
- 工作区中的 `MEMORY.md` 不再自动读全文，仅提供文件索引；继续使用已有安全路径读取、锁和权限检查。

配置示例（拟扩展现有 `ResearchMemorySettings`，新增字段而非建立另一套预算系统）：

```json
{
  "research_memory": {
    "auto_summary_max_tokens": 1500,
    "auto_index_max_items": 12,
    "query_default_limit": 20,
    "query_max_limit": 100
  }
}
```

1500 是自动注入摘要与索引的上限，实际取它与现有上下文剩余预算的较小值；超过预算时缩减可选索引。每条摘要截断到 160 个 Unicode 字符。详细工具结果仍受已有工具输出限制，可返回内容引用并分页读取。

原 `memory_auto_inject_max_chars` 配置可继续被解析并给出弃用提示，但不再控制 `MEMORY.md` 自动注入。原研究记忆 enabled 开关仅控制召回，不关闭执行校验、持久化或项目权限。

### 步骤六：工具契约与成果通用化

**涉及：** `tools/research_memory_tool.py`、`tools/research_project_tool.py`、`state/models.py`、技能说明和提示词、`contracts/research.py`。

- 保留 `research_memory` 工具名，缩小职责为工作区情节记忆；描述中的“当前会话证据”改为“当前工作区证据”。
- 保留 `read(ids, include_content)` 能力，新增 `search` 操作；workspace ID 从宿主 scope 获取，不让模型自由切换工作区。
- `search` 输入包含 query、record_types、task_id、status、cursor、limit；输出包含 items、next_cursor、目录 revision。默认排除已撤回记录，显式历史查询可包含。
- 翻页 cursor 绑定工作区、过滤条件和目录 revision；期间目录变化则返回可重试的游标过期错误，避免默默漏项。首次查询与 revision 读取使用一致快照。
- 写入工具复用现有 add_evidence、add_reasoning、verify_evidence、add_conclusion、冲突处理等领域动作及校验，不能允许任意 JSON 直接覆盖已验证记录。
- 移除模型工具中的旧 set_context/create_plan/update_task 写通路；迁移程序仍能解释旧历史。计划、任务和目标统一经 research_project 与 planner/replanner。
- 原全局 `expected_revision` 替换为动作所需的项目或记录版本；工具 schema 按 action 判别，读取回执提供下一次写入需要的版本。

读取示例：

```json
{
  "operation": {
    "action": "search",
    "record_types": ["evidence", "conclusion", "artifact"],
    "task_id": "task_example",
    "query": "盈利预测",
    "limit": 20
  }
}
```

成果模型保留通用元数据：kind、task/run 来源、input_refs、content_ref、代码／数据版本引用、verification_status。报告、解析摘要继续使用原有类型化 payload 和验收；未来 factor、strategy、backtest 再提供各自 schema 和校验器。未知 kind 可以作为未验证附件保存，不能因此通过任务完成检查。

`CompletionPolicy` 不依赖整个 ResearchMemory，而是接受执行视图和显式加载的验收输入；原有核验、财务口径、来源权限和引用检查原样保留。完成状态仍由宿主验收决定。

### 步骤七：交付、删除与文件生命周期

- `DeliveryService` 的冻结清单、审核决定和下载权限按工作区定位；保留创建它的 session/run 信息。一个对话可展示并下载同工作区成果。
- `[E:...]` 引用按工作区解析，保留原来源、版本和快照。旧聊天展示仍读取原回答回执，不把新的证据版本悄悄替换进历史回答。
- 删除对话只删除聊天与会话展示数据，并终止该对话自己的运行；证据、任务、报告、交付清单和共享文件保持可访问。
- 被有效来源／交付引用的附件不得物理删除；返回明确“正在被研究记录引用”的结果。工作区整体删除不在本次范围内。
- 工作区目录验证改为验证持久化绑定和允许的文件根，不再验证目录名必须等于基于 session 的哈希；继续拒绝越界、符号链接及硬链接绕过。
- `ConversationRecords.delete` 不再删除承载项目状态的父行。已有观察值保留测试扩展为来源、证据、任务与产物均保留。

## 9. 迁移方案与回滚

### 9.1 数据映射规则

新增可重复执行的离线迁移流程，分为预检、复制、校验、切换；不在普通请求中隐式执行迁移。

1. 输出清单：旧目录 workspace ID、session、project、项目文件路径、内容对象数量、资产绑定、运行状态。
2. 以旧作用域内明确的 project 绑定分组，为每组分配稳定新 workspace ID；同一明确项目下的多个对话进入同一工作区。没有项目的普通研究会话各自建立项目壳；压缩存档、子代理 transcript 跟随父对话，不单独创建工作区。
3. 保存不可变映射 `(old_workspace_id, old_session_id, old_project_id) → (new_workspace_id, project_id)`；重复执行复用映射，不生成第二份工作区。
4. 发现会话聚合 project 与绑定表冲突、一个对话关联多个真实项目、相同实体 ID 内容不一致时，预检失败并输出冲突清单；不得按时间或目录猜测归属。
5. 显式共享资产复制到各目标工作区，保留原始 ID、版本和来源映射；原项目内 ID 不重写。相同 ID 的冲突按上一条处理。
6. 复制内容对象到新 workspace namespace，验证 SHA-256 后注册；本地与 S3 后端均走 ContentStore，不通过替换路径字符串绕过哈希和范围验证。
7. 重写所有结构化内容引用、绑定、run、交付与引用快照的作用域字段；旧聊天自由文本保留，旧程序生成的文件／存档地址通过有作用域的迁移映射解析，禁止跨工作区回退搜索。
8. 既有项目文件根可保留其物理路径并绑定到新 ID；无项目工作区新建私有目录，导入其附件和笔记。文件内容和权限校验失败时不切换。
9. 历史 history/receipts 原内容保留并增加 legacy origin 元数据，不改写已有操作指纹。未完成 run 迁移为 paused，旧租约失效，完成成果状态不变。

旧 workspaces 行不能直接批量重命名：一个旧目录下可能有多个项目，且 content_objects、conversation archive、turn journal、run events 和审核记录都存在作用域依赖。迁移清单必须覆盖 schema 中所有 workspace/session 外键以及 JSONB 内的结构化引用。

### 9.2 切换与回滚步骤

1. 在隔离数据库和内容存储副本上完成迁移演练；生成按实体类型统计的迁移报告。
2. 正式切换时停止接收新命令，暂停活动运行，备份数据库、内容存储及项目文件。
3. 运行 schema 迁移及数据复制；每个旧研究实体都必须在迁移清单中有目标记录或明确的归档去向。
4. 校验主外键、引用闭包、文件哈希、历史回答引用和交付 manifest；执行基础冒烟测试后启动新版本。
5. 校验失败时不开放写入，恢复备份并启动旧版本。开放新版本写入后若需回滚，先导出新增数据再恢复；不承诺无损自动反向迁移。
6. 旧 schema／副本在验收完成前只读保留，不能被运行时当作 fallback。清理旧副本作为后续显式维护动作。

## 10. 影响范围与风险评估

| 范围 | 影响级别 | 具体影响与控制措施 |
|---|---|---|
| 数据库与迁移 | 高 | 改主键归属、外键和对象 namespace；先复制校验再切换，保留备份 |
| 任务与并发 | 高 | 会话单指针改为 run 绑定；测跨对话领取、取消、重规划和迟到结果 |
| 删除与文件访问 | 高 | 对话删除必须脱离资产生命周期；测级联关系及引用文件保护 |
| 上下文与压缩 | 中高 | 注入内容减少可能影响模型行为；通过查询工具与索引弥补，检验报告质量和调用成本 |
| 引用与审核 | 高 | 作用域变化可能破坏旧链接或失效传播；冻结版本、迁移引用、沿用依赖校验 |
| Web／CLI | 中高 | 新增工作区选择、会话绑定与共享项目进度；保留现有 session 路由并明确歧义处理 |
| Skills／提示词 | 中 | 更新记忆工具动作和作用域说明；报告业务计算逻辑不在本次改写 |
| 存储后端 | 中 | 仍用 PostgreSQL 和现有 Local/S3 ContentStore；不新增数据库依赖 |
| 后续因子／策略／回测 | 正向扩展 | 复用 run、成果、输入版本与验收接口；本次不预设收益指标或回测引擎 |

最大实施风险是“只把表按工作区共享，却仍使用会话隐式任务、恢复及删除逻辑”。因此身份、run 绑定、删除关系、事务提交必须在同一版本完成切换。

性能目标是上下文查询成本与所选任务及索引上限相关，而非与全工作区历史总量相关。项目级完整验收允许更高成本，但必须与每轮上下文准备分开统计。

## 11. 验证方式与回归测试要点

### 11.1 新增行为测试矩阵

| 场景 | 关键断言 |
|---|---|
| 同工作区跨对话读取 | B 可检索 A 创建的证据与成果；B 的普通消息列表不含 A 的聊天 |
| 跨工作区隔离 | 即使猜中记录 ID、cursor、内容 hash，也不能读取其他工作区数据 |
| 两个对话并行 | 不同任务同时执行；同任务只有一个 run 领取成功 |
| 停止与改目标 | 停 A 不影响 B；项目改目标使相关旧租约失效，迟到提交无有效资产落库 |
| 幂等与失败注入 | 相同操作重试只生成一份成果；不同内容复用 key 报错；事务中断无半完成任务 |
| 删除对话 | 共享来源、证据、任务、成果、交付与内容仍存在；不再拥有活动运行 |
| 引用保护 | 已被证据引用的附件不能直接物理删除；历史回答仍指向原版本 |
| 压缩与恢复 | 压缩不删情节记录；重启暂停运行、重建摘要，旧 runtime 注入块不重复出现 |
| 实际模型可见性 | 捕获 fake provider 的最终请求，和 WorkingContext 一致；包含工具 schema 与多模态处理后的实际内容 |
| 有界读取 | 10、1000、10000 条历史记录下，自动索引不超过 12，摘要不超过配置预算；SQL 不读全文／全 history |
| 分页与修订 | 无变化时分页无重复遗漏；目录变更后旧 cursor 明确过期；撤回证据默认不返回 |
| 版本与审核 | 替代证据使依赖结论和已批准交付失效；不同工作区同 ID 不受牵连 |
| 迁移演练 | 同目录多项目正确拆分、多对话正确合并、孤立会话与子代理存档正确归属、重复运行幂等 |
| 迁移故障 | 缺正文、坏哈希、绑定冲突、记录 ID 冲突会阻止切换；备份可恢复 |

有界读取用 SQL 捕获与返回数据体积断言，避免只断言最终提示词长度而漏掉数据库全量读取。性能基准记录上下文准备时间、读取行数、读取字节数、锁等待和模型工具调用次数；不使用依赖机器性能的固定毫秒断言。

### 11.2 现有回归测试映射

- 上下文与会话：`tests/test_services/test_context_budget.py`、`test_compact.py`、`test_session_storage.py`；`tests/test_engine/test_query_engine.py`、`test_prompt_cache.py`。
- 研究控制与成果：`tests/test_research/test_report_runtime.py`、`test_workspace_memory.py`、`test_memory_compatibility.py`、`test_parallel_research.py`、`test_business_delivery.py`、`test_architecture_remediation.py`。
- 持久化：`tests/test_storage/test_postgresql.py`、`test_content_backends.py`；新增工作区数据迁移测试，覆盖全新 schema 和旧 schema 升级。
- 工具与调度：`tests/test_research/test_dispatch_subagents.py`、`test_planning_tools.py`；`tests/test_harness/test_turn_journal.py`、`test_parallel_resources.py`。
- Web：`tests/test_web/test_runtime_lifecycle.py`、`test_research.py`、`test_app.py`、`test_sse.py`；前端 `frontend/web/e2e/workspace.spec.ts` 增加工作区切换、共享成果和对话删除场景。
- 业务端到端：`tests/test_entrypoints/test_research_product.py`、`tests/test_research/test_skill_workflows.py`，并使用现有 research eval 对比深度报告和研报解析摘要的引用完整性、任务完成率及工具调用成本。

旧测试中“读取 MEMORY.md 就自动注入正文”“删除会话即删除研究记录”的预期按新契约修改；来源真实性、路径隔离、版本校验和工具调用配对要求不得弱化。测试通过新的模块接口验证行为，避免为旧 `ResearchMemory` 字段排列编写镜像测试。

### 11.3 执行验证的方法

后端测试必须使用迁移后的独立 PostgreSQL 测试库。现有 `tests/conftest.py` 要求 `RESEARCHX_TEST_DATABASE_URL` 已配置，且数据库名为 `test_*` 或 `*_test`，不能直接对用户研究库运行。

示例命令（在实现完成、测试库迁移后执行）：

```bash
uv run pytest tests/test_services/test_context_budget.py tests/test_services/test_compact.py tests/test_services/test_session_storage.py
uv run pytest tests/test_research tests/test_storage tests/test_harness
uv run pytest tests/test_engine tests/test_web tests/test_entrypoints/test_research_product.py
npm --prefix frontend/web run build
npm --prefix frontend/web run test:e2e -- e2e/workspace.spec.ts
```

真实模型评估使用固定输入、同一模型配置及相同来源夹具，区分模型随机性与架构变化。必须检查“结果质量正常但工具查询次数大增”的情况，必要时优化自动索引内容，而不是重新注入整个证据池。

## 12. 分阶段交付与完成条件

1. **身份与迁移准备：** 新 schema、显式 scope、工作区／会话接口、迁移预检和演练。产出可核对的数据映射报告。
2. **执行与情节拆分：** 独立查询、原子写入、run 任务绑定、租约与恢复、删除隔离。并发和故障注入测试通过。
3. **上下文与工具切换：** 有界索引、按需读取、WorkingContext、压缩恢复及工具说明更新。确认热路径不再调用全量聚合加载。
4. **前后端贯通与回归：** 工作区选择、共享项目进度、成果访问、旧引用及报告生成回归；完成数据迁移演练和新旧质量对比。
5. **正式切换：** 停写、备份、迁移、完整性校验、冒烟、开放服务。全部阶段交付同一套最终契约，不在生产环境维持两套权威研究状态。

本文只细化修改方案，尚未修改运行时代码、执行数据库迁移或运行上述功能回归测试。
