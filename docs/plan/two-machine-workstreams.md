# 两机开发工作流（企业 AI 平台 · 以公司工作流为核心）

> 本文件是**两台机器（含本机）的唯一任务源**。任何人/机器开始工作前先读它，并读 `docs/plan/ledger/` 下的状态文件。
> 最后更新：2026-09-20

---

## 0. 定位（不可偏离）

这是**给企业用的 AI 平台，核心是公司工作流**；其余一切（知识库、工具、模型、连接器、助手、市场）都为工作流服务。
三条主线：**① 非技术员工能方便搭工作流 ② 搭好后能方便地用（含运行中提问、结果审核）③ 工作流能持续改进（自改进 + 从用户纠正中学习）**。
权限必须支持四层：**公司 / 部门 / 个人 / 单独授权**。

---

## 0.1 对端机器如何开始（**只用 git 远端与仓库相对路径，不依赖任何本机路径**）

### 仓库

```
远端：https://github.com/whl736989911/AIplatform-work.git   （origin）
```

获取代码（二选一）：

```bash
# A) 还没有本地副本
git clone https://github.com/whl736989911/AIplatform-work.git && cd AIplatform-work

# B) 已有本地副本
git fetch origin --prune && git checkout main && git pull --ff-only
```

> 若 `main` 上还没有本文件（PR 未合并），先取它所在的分支：
> `git fetch origin docs/two-machine-workstreams && git checkout docs/two-machine-workstreams`
> 等合并后切回 `main` 继续。

### 开工前必读（全部为仓库相对路径）

| 文件 | 用途 |
|---|---|
| `docs/plan/two-machine-workstreams.md` | 本文件：任务表、契约、批次、协议 |
| `docs/plan/ledger/integrator.json` | `next_batch`（当前该做第几批）、`verified`、`blocked` |
| `docs/plan/ledger/machine-a.json` | A 线进度（对端只读） |
| `docs/plan/ledger/machine-b.json` | B 线进度（**B 线机器写自己的**；A 线只读） |

### 环境准备（同一套命令可用于 Linux / macOS / Windows）

```bash
# Python 3.12 + 依赖
uv sync

# 前端依赖（涉及前端任务时）
cd dashboard && npm ci && cd ..

# 数据库（WorkBuddy 需要 PostgreSQL + pgvector；SQLite 仅个人面）
export OCTOP_TEST_DATABASE_URL='postgresql://<user>:<pass>@<host>:<port>/<db>'
# 限流相关测试需要 Redis（可选）
export OCTOP_TEST_REDIS_URL='redis://<host>:<port>/0'
# 需要 pgvector：
#   psql -d <db> -c 'CREATE EXTENSION IF NOT EXISTS vector;'
```

### 定位自己的任务

1. 看 `integrator.json` 的 `next_batch`（例如 `1`）→ 到第 4 节批次表找到该批次属于你那条线的任务号；
2. 到第 2 节任务表看每个任务的"主要文件 / 依赖 / 验收"；
3. 只改第 3 节契约 5 里属于你那条线的文件；
4. 完成后按第 5.2 节写你自己的 ledger 并提 PR。

### 分支与提交

```bash
git checkout main && git pull --ff-only
git checkout -b feat/<你的线>-<主题>        # A 线: feat/orchestration-*；B 线: feat/rbac-kb-*
```

---

## 1. 两机分工

| 机器 | 线 | 范围 | 迁移号段 | i18n 命名空间 | 分支前缀 |
|---|---|---|---|---|---|
| **machine-A（本机）** | **A 线：工作流与编排** | A-01…A-24 | `031–049` | `workflow.*` `inbox.*` `proposal.*` | `feat/orchestration-*` |
| **machine-B** | **B 线：权限与知识库** | B-01…B-16 | `050–069` | `knowledge.*` `access.*` `catalog.*` | `feat/rbac-kb-*` |

---

## 2. 任务表

### 2.1 A 线：工作流与编排（machine-A）

| ID | 任务 | 层 | 主要文件 | 依赖 | 验收 |
|---|---|---|---|---|---|
| A-01 | 前端 IA 重组：新侧边栏/路由、工作流详情页做中枢、去"个人版"痕迹 | 前端 | `layouts/sidebarNav.tsx`、`routes/index.tsx`、`pages/WorkBuddy/{Home,Inbox,Runs}/**` | — | 员工只看 工作台/收件箱/工作流/运行；旧路由重定向 |
| A-02 | 运行详情增强：逐节点输入输出/耗时/token、重跑到此、重试、取消、对账 | 前端+读接口 | `pages/WorkBuddy/Workflows/ExecutionsPanel`、`workbuddy_runtime.py` | A-01 | 一次运行可逐节点排障 |
| A-03 | 版本对比（定义级 diff） | 前端 | 版本页签 | A-01 | 两版差异可读 |
| A-04 | 节点元数据导出（字段/类型/必填/引用语法） | 后端契约 | `workflow_compiler.py` + 新 `node_schema` | — | 前端可据此渲染表单 |
| A-05 | 编译器诊断聚合（收集式多错 + 错误码→修复提示） | 后端 | `workflow_compiler.py`（46 处 raise 收敛） | — | 一次返回多条带 path 的诊断 |
| A-06 | 创建向导（输入/步骤/数据/输出 + 实时校验 + 试运行） | 前端 | 新 `pages/WorkBuddy/Workflows/Create/` | A-04 A-05 | 非技术员工可建出可运行工作流 |
| A-07 | 节点集扩展：`input`/`knowledge`/`output` 显式化 | 后端+前端 | 编译器 + 画布 | A-04 | 编译通过且旧定义兼容 |
| A-08 | **`ask` 人类表单节点 + `waiting_input` + 输入请求表 + `resume` 载数据** | 后端+迁移 | `runtime.py`、`workbuddy_runtime.py`、迁移 031+ | A-05 | 运行中提问→收件箱填表→执行继续 |
| A-09 | 产出审核（修正后确认/采纳/重跑） | 后端+前端 | 同 A-08 | A-08 | 末节点产出可被人工修正 |
| A-10 | 超时与升级（SLA） | 后端 | `runtime.py` | A-08 | 超时按规则升级并可审计 |
| A-11 | 收件箱（审批+提问+审核三合一 + 表单抽屉） | 前端 | `pages/WorkBuddy/Inbox/**`（批次 1 已建入口与骨架，批次 4 补提问/审核与表单抽屉） | A-08 | 员工每天只来这一页 |
| A-12 | 纠正/反馈采集表 `workbuddy_execution_feedback` + 接口 | 后端+迁移 | 迁移 031+ | A-08 | 每次修正留结构化记录 |
| A-13 | 纠正归因与范围界定 | 后端 | 新分析模块 | A-12 | 输出"改哪、影响面多大" |
| A-14 | 分析器（PG 版）+ 生成提案候选 → 接既有治理链 | 后端 | `proposals.py` | A-13 | 自动产出提案并可走影子/灰度/提升 |
| A-15 | Agent 生成工作流：受限 DSL + 编译闭环 + 补丁式生成 + 可解释输出 | 后端+前端 | 新 `authoring/` | A-04 A-05 A-06 | 自然语言→≤2 轮编译通过→人工确认 |
| A-16 | 画布（React Flow）+ 字段映射面板 + JSON 双向 | 前端 | 新依赖 `@xyflow/react` | A-04 A-07 | 拖拽连线可编辑，JSON 同步一致 |
| A-17 | Chatflow 形态（对话触发流程） | 后端+前端 | 会话页 + 运行时 | A-01 | 消息可触发流程 |
| A-18 | 会话续期（refresh token / 滑动续期） | 后端+前端 | `auth.py` | — | 不再每天重登 |
| A-19 | 修缺陷：提案"分配审核人" `/reviewers` | 后端 | `workbuddy_proposals.py` | — | 接口可用不再 404 |
| A-20 | 修缺陷：生命周期"重新认证/download challenge"未接线 | 后端+前端 | `workbuddy_lifecycle.py` | — | 按钮行为与后端一致 |
| A-21 | 修缺陷：市场"安装列表"接口 | 后端 | `marketplace.py` | — | 安装台账可查 |
| A-22 | 触发器投递执行接线 | 后端 | triggers 服务 | — | Webhook 事件真正启动执行 |
| A-23 | outbox 传输实现（端口→实体） | 后端 | `outbox.py` | — | 事件真正投递 + 死信可查 |
| A-24 | 租户级指标口径与暴露 | 后端+前端 | 指标模块 | A-01 | 员工看自己、管理员看租户 |

### 2.2 B 线：权限与知识库（machine-B）

| ID | 任务 | 层 | 主要文件 | 依赖 | 验收 |
|---|---|---|---|---|---|
| B-01 | **权限骨架**：通用 `scope + ACL` 表与解析（照抄 KB 模型） | 后端+迁移 | 迁移 050+、新 `rbac/` | — | 任意对象可挂四层权限 |
| B-02 | 工作流可见性：`visibility` + ACL + 列表查询改造 | 后端+迁移 | `workbuddy_workflows.py` | B-01 | 我的/部门/公司/授权可见 |
| B-03 | 工具/模型 grants 加 `subject`（部门/成员） | 后端+迁移 | `workbuddy_catalog.py` | B-01 | 部门/个人级授权 |
| B-04 | **编译器 Resolver 签名扩展**（按调用者解析可达性）← **与 A 线唯一契约点** | 后端 | `PostgresWorkflowSemanticResolver` | B-02 | 编译期按调用者校验 |
| B-05 | 岗位授权（作者/发布者/审批人/库管理员/运维） | 后端+前端 | 权限策略页 | B-01 | 员工不必提权即可干活 |
| B-06 | 默认可见性回填策略 + 数据回填（既有对象→`enterprise`） | 迁移 | 迁移 050+ | B-02 | 上线不"丢东西" |
| B-07 | 权限策略页 | 前端 | 新 `pages/Settings/Access/` | B-05 | 管理员可配 |
| B-08 | 知识库迁移脚本（A/B/C 路径 + 报告 + 幂等可重跑） | 脚本 | 新 `scripts/migrate_kb.py` | — | 逐库出结论与计数 |
| B-09 | 文本-only 迁移通道（放宽 `file_ref_id` 或占位） | 后端+迁移 | `workbuddy_knowledge.py`、迁移 050+ | B-08 | 无原文件也能导入 |
| B-10 | 保留能力并入：文本型文档/文件夹/预览/抽取文本下载/单·整库重建索引/默认打开库/每库上限/KB 级 OCR | 后端+前端 | `workbuddy_knowledge.py`、知识库页 | B-08 | 个人版能力不缺 |
| B-11 | 本地 ONNX 模型纳入平台模型目录（一类 revision + 管理员授权） | 后端+前端 | `workbuddy_catalog.py` | B-03 | 模型可审计且可选 |
| B-12 | 旧接口适配层（读投影 + 双写）+ `knowledge_source` 回滚开关 | 后端 | `knowledge_bases.py` 适配 | B-08 | 个人页零改动 |
| B-13 | ACL/偏好映射与回填（`shared`→scope、members→ACL、`default_open`→偏好） | 迁移 | 迁移 050+ | B-08 | 可见范围不变 |
| B-14 | UI 合流（一页四层过滤 + 文档编辑/文件夹/预览） | 前端 | 知识库页 | B-10 B-13 | 只有一个入口 |
| B-15 | 校验工具（chunk 计数对账 + 检索抽样对比） | 脚本 | 同 B-08 | B-08 | 迁移可验证 |
| B-16 | 合规后端（清单/规则版本/批准/撤销/归档）— **需先签合规政策** | 后端 | 新 `compliance.py` | 政策 | 占位按钮变真功能 |

### 2.3 迁移规则（知识库合流，B 线）

| 个人版 | 企业版（目标） |
|---|---|
| `shared = 1` | `scope='enterprise'`（该布尔在个人版语义即"全公司可见"，有代码依据） |
| `shared = 0` | `scope='personal'` |
| `knowledge_base_members(user, role)` | ACL（`user_id` + `read/write/admin`）= **单独授权**层 |
| （无对应物） | `scope='department'`：由管理员在界面上设置 |
| `default_open` | 用户偏好（非权限） |

**向量迁移三路径**：A 直接搬（模型标识在目录且已授权、维度一致）/ B 重新嵌入（模型不同或维度不符）/ **C 挂起待配置**（租户未授权模型 → 管理员在"能力许可"授权后重跑）。**源文件不保留** → 只迁 `index.sqlite` 的分块文本 + 向量，`file_ref_id` 走专用迁移标记。

---

## 3. 共享契约（违反必然冲突）

1. **迁移编号**：A 线只用 `031–049`，B 线只用 `050–069`；分叉迁移落在本地最大号之后并在文件头注明来源。**禁止交叉占用**。
2. **`errors.py`**：只追加、不重排、不删除；合并取并集（AST 校验：成员全为字符串、两侧不缺、全仓引用无悬空）。
3. **i18n**：`en.json`/`zh.json` 必须同步；命名空间按 1 表分段。
4. **CHANGELOG**：各自只追加自己的条目（新增/修复/文档 分区）。
5. **文件所有权**（按文件族划分；**对方的文件只读**，跨线只经接口调用）：
   - **A 线**：`src/octop/infra/workbuddy/{compiler,runtime,proposals,marketplace,lifecycle}.py`、`src/octop/infra/workbuddy/authoring/**`、`src/octop/api/routers/workbuddy_{runtime,proposals,marketplace,lifecycle}.py`、`src/octop/infra/db/repos/workbuddy_{runtime,proposals,marketplace,lifecycle}.py`、`dashboard/src/pages/WorkBuddy/{Workflows,Proposals,Approvals,Marketplace,Home,Inbox,Runs}/**`、`dashboard/src/layouts/**`、`dashboard/src/routes/**`、`dashboard/src/api/modules/workbuddy{Workflows,Runtime,Proposals,Marketplace,Lifecycle}.ts`
   - **B 线**：`workbuddy_knowledge.py`、`workbuddy_catalog.py`、`workbuddy_identity.py`、**`workbuddy_workflows.py`**、`pages/WorkBuddy/{Knowledge,Lifecycle}/**`、新 `rbac/`、`scripts/migrate_kb.py`、`dashboard/src/api/modules/workbuddy{Knowledge,Catalog,Identity}.ts`
   - **共享但只在"追加"上并发**（谁都不许重排/删除）：`contracts/route-manifest.json`（各自只加自己任务的条目；`counts` 后合并方 rebase 后重算）、`dashboard/src/locales/{en,zh}.json`（中英必须同步）、`CHANGELOG.md`、`src/octop/infra/errors.py`。
   - **归属冲突时以本表为准**：例 A-20（再认证/下载挑战）后端在 A 线的 `workbuddy_lifecycle.py`，而 `pages/WorkBuddy/Lifecycle/**` 仍归 B 线——A 线只改 `dashboard/src/api/modules/workbuddyLifecycle.ts`。
   - **一处按路由的例外**：`/auth/reauthenticate`（A-20，stage D）实现在 `workbuddy_identity.py`（B 线文件）内；该文件其余身份/租户路由仍归 B 线。A 线对该文件只保留这一条路由及其私有辅助函数，B 线改动时不得重排/删除它。
6. **唯一交叉点**：B 线提供 `Resolver(actor, …)` 新签名（B-04）；A 线只调用、不改实现。
7. **合并顺序**：先合 A 线批次 1–3（A-01…A-07、A-19…A-21）→ 再合 B-01…B-07（权限）→ 然后 A-08+ 与 B-08+。**迁移号段依然分离**：A 线批次 1 的 A-20 已占用 `031`（再认证凭据表，PG 实体 + SQLite 水位），后续 A 线迁移从 `032` 起，B 线仍从 `050` 起。
8. **合并纪律**：不要 `--delete-branch` 删掉别人 PR 的 base；squash 会重写基线，后合并方需 rebase（只冲突 CHANGELOG 时用并集脚本）；上游同步时迁移冲突落到本地最大号之后。
9. **推送信息**：commit message 与 PR 说明用**详细简体中文**（背景/根因/改动/可复现证据/验证命令与结果/未包含项）。

---

## 4. 批次计划与并行性

| 批次 | A 线 | B 线 | 可并行 |
|---|---|---|---|
| 1 | A-01、A-19、A-20、A-21 | B-01、B-08（脚本骨架）、B-09 | ✅ |
| 2 | A-04、A-05、A-02、A-03 | B-02、B-03、B-04 | ✅ |
| 3 | A-06、A-07 | B-05、B-06、B-07 | ✅ |
| 4 | A-08、A-09、A-11 | B-10、B-12 | ✅ |
| 5 | A-12、A-13、A-14、A-15 | B-13、B-14、B-15 | ✅ |
| 6 | A-16、A-17、A-22、A-23、A-24 | B-11、B-16（待政策） | ✅ |

**关键路径**：`A-05 → A-08 → A-14 → A-15`；`B-01 → B-02/B-04 → B-06`。

---

## 5. 协作协议（两台机器自动流转，无需人工）

### 5.1 状态文件（唯一同步点，**每个文件只有一个写者**）

| 文件 | 写者 | 内容 |
|---|---|---|
| `docs/plan/ledger/machine-a.json` | machine-A | 自己每个批次的状态、PR、提交、证据 |
| `docs/plan/ledger/machine-b.json` | machine-B | 同上（B 线） |
| `docs/plan/ledger/integrator.json` | **machine-A（集成者）** | `next_batch`、每批次 `verified` 记录、`blocked` 列表 |

因为**每个文件只有一个写者**，两台机器同时改动也**永不产生冲突**。

`machine-X.json` 结构：

```json
{
  "machine": "machine-A",
  "line": "A",
  "updated": "<ISO8601>",
  "batches": {
    "1": {"state": "pending|in_progress|done", "pr": null, "commits": [], "evidence": "", "notes": ""}
  }
}
```

`integrator.json` 结构：

```json
{
  "integrator": "machine-A",
  "next_batch": 2,
  "verified": {"1": {"at": "<ISO8601>", "by": "machine-A", "checks": [], "fixes": [], "notes": ""}},
  "blocked": []
}
```

### 5.2 每台机器的循环（每轮都不需要人）

1. **同步**：`git fetch origin main && git pull --rebase origin main`；读三个 ledger。
2. **判定**：
   - `integrator.blocked` 里有与自己相关项 → 停下并保留其余可做任务；
   - `integrator.next_batch` 指向的批次 = 自己的下一个批次 → 做它；
   - 自己的该批次已是 `in_progress` → 继续做完。
3. **实施**：只改自己拥有的文件；遵守第 3 节契约。
4. **收尾**：在**自己的** ledger 里写 `done` + PR 号 + 提交 + **可复现证据**（命令与结果）；提 PR（中文详细）并合并。
5. **等待对端**：每 3–5 分钟 `git fetch origin main`（或 `gh pr list --state all`）检查对端该批次是否 `done`；每次等待都更新自己 ledger 的 `updated`（心跳）。
6. **集成者（machine-A）额外职责**：两方同批次都 `done` 后：
   - 跑该批次的**效果验收**（第 6 节门禁 + 相关冒烟）；
   - 修复集成问题（可开自己的修复 PR）；
   - 在 `integrator.json` 写 `verified[<批次>]`（含 checks/fixes/notes）并 `next_batch += 1`。
7. **下一轮**：两机各自 `git pull` 后看到 `next_batch` 前进 → 自动开始下一批次。

### 5.3 停滞规则（防死锁）

- 等待对端**超过 30 分钟**：可以在**自己的分支**上准备下一批次的代码与自测，但**不得合并**，直到 `verified` 出现；
- 每轮等待必须更新自己 ledger 的 `updated` 时间戳（心跳），便于对方判断是否还活着；
- 对端超过 **24 小时**无心跳：在自己的 ledger 写 `notes: "peer stale"`，并**继续推进自己线上不与对方重叠的任务**（不要阻塞）。

### 5.4 唯一需要人介入的情形

产品/合规决策（合规政策签署、默认可见性语义、ONNX 是否保留、迁移归属特例）→ 写进 `integrator.json.blocked`（含问题、影响范围、PR），**停止该子任务**，其余任务继续。其余一切不需要人。

---

## 6. 每批次验收门禁（集成者执行）

```bash
# 后端静态与类型
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src/octop

# 迁移三态（既有库升级 / 全新 PG / 全新 SQLite）
#   —— 既有库用 OCTOP_TEST_DATABASE_URL 指向开发库，验证数据完好且水位前进
uv run pytest tests/unit/db -q
uv run pytest tests/unit/workbuddy tests/integration/test_workbuddy_business_smoke.py \
  tests/integration/test_workbuddy_runtime_postgres.py -q -p no:randomly

# 前端
cd dashboard && npm run lint && npx tsc -b && npx vitest run

# 涉及前端界面：用真实服务跑一次人工冒烟并留证据（截图/接口返回）
```

---

## 7. 交付纪律

- 一个批次 = 一个（或少数几个）PR；**不要跨批次混提**；
- 每个 PR 必须写清"未包含项"；
- 不为了让门禁变绿而放宽断言、跳过测试或删除既有测试；
- 遇到与契约冲突的情况：**停下并在 PR 里标"待决策"**，不要自行改契约。
