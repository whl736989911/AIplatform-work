# WorkBuddy — 基于 Octop 的多租户企业 AI 工作流平台

> **请先读这一段。** 本仓库**不是**上游 [Octop](https://github.com/TencentCloud/Octop) 的发布分支，也**不是**它的产品文档。
> 本仓库以上游 Octop（MIT，见 `LICENSE`）为**基座**，但产品方向是**企业多租户的工作流平台**：让员工把工作流建起来、跑起来、并且能持续改进，
> 其中"人在环"的审批、提问、审核是工作流的一部分而不是外挂。上游 README 描述的另一个产品（自托管个人/团队 AI 助手、通道、
> FnOS NAS 打包、远程手机、桌面安装包、上游的插件与专家市场等）**不代表本仓库的方向**，也不在本仓库的验收范围内。
> 如果你在找上游项目，请去它的仓库；如果你要看本仓库到底交付了什么，请读下面的「已交付的能力」与 `docs/plan/`。

## 这个仓库是什么

- 一个**多租户企业平台**：租户 / 部门 / 成员的治理、四层权限（公司 / 部门 / 个人 / 单独授权）、跨租户隔离由 PostgreSQL 行级安全（RLS）保证。
- 一个**工作流平台**：定义 → 编译（一次报出多条诊断）→ 不可变版本 → 发布 / 回滚 → 执行 Worker → 逐节点排障与版本 diff。
- 一个**人在环平台**：审批（授权外部写或一般复核）、**运行中向人提问**（缺一个只有人知道的事实时停在原地等回答）、**产出审核**（事后只追加：采纳 / 修正后确认 / 重跑）。
- 一个**会改进的平台**：纠正采集 → 归因 → 分析器产出提案 → 影子 / 金丝雀评估 → 提升为版本；模板市场与行业模板。
- 一个**可运维的服务**：迁移（PostgreSQL 与 SQLite 两种控制面）、作业与审计、发件箱（outbox）派发、限流、配额与租约 / fencing。

## 这个仓库不是什么

- 不是上游 Octop 的镜像或发布通道；上游 README、徽章与截图属于上游项目。
- 不以"个人助理 / 聊天机器人"为产品定义——聊天与 Agent 基座能力仍在代码中（见下），但那不是本仓库要交付的东西。
- 不承诺上游面向个人与 NAS 场景的打包与安装形态。
- 当前**不是**一个可以直接对外生产的成品：连接器 / 模型网关适配器与触发器投递执行属**部署方接线**，outbox 需要先接发布端口，
  租户级指标口径与生产注销合规签署属**产品决策**（详见「当前边界」）。

## 已交付的能力

### WorkBuddy（本仓库的方向）

| 能力 | 说明 |
|---|---|
| 租户与身份 | 租户、部门、成员、角色；成员状态与租户状态是运行时的事实来源 |
| 工作流 | 定义 schema（权威文件 `contracts/workflow-v1.schema.json`）、编译器聚合诊断、不可变版本与 ETag、发布 / 回滚 / 归档 |
| 执行 | 只入队不执行；Worker 原子领取（锁租户行、占运行槽、租约 + 单调 fencing），崩溃后由下一个 Worker 接管 |
| 人在环 | 审批（含一次性挑战令牌、候选审批人）、**提问**（`waiting_input`，回答问题后继续）、**产出审核**（事后只追加） |
| 治理 | 工具 / 模型 / 知识库 / 审批人的可达性按**调用者**解析；能力授权支持部门与成员主体；对象级四层权限 |
| 知识库 | 文档索引为真实作业；文本-only 迁移通道（个人版只留分块文本与向量） |
| 改进闭环 | 纠正采集 → 归因 → 提案 → 影子回放 → 金丝雀评估 → 提升；提案审核人独立指派 |
| 平台服务 | 作业、通知、审计、发件箱 + 派发器、限流、配额（月度执行预留与并发运行槽）、平台工具注册表 |

### 继承自 Octop 的基座（仍在仓库中，但不是本仓库的产品方向）

聊天与 Agent、技能 / 子智能体、知识库前端、连接器网关、通道（飞书 / 钉钉 / 企业微信等）、Dashboard 控制台、CLI、桌面壳与打包脚本。
它们随构建存在、部分仍被 WorkBuddy 复用（例如控制台、认证、数据库层），但**不要**把上游对这些能力的产品化描述当作本仓库的承诺。

## 快速开始

### 环境要求

- Python **≥ 3.12**；用 [`uv`](https://docs.astral.sh/uv/) 管理依赖（Makefile 里的命令都走 `uv run`）。
- **PostgreSQL 是必需的**（WorkBuddy 的租户工作流需要它），并且需要 `vector` 扩展（部署前作为前置条件安装）。
- **Redis** 承载限流；生产部署建议同时运行 Worker 层，或让单进程安装托管它。

### 安装与初始化

```bash
uv sync                       # 安装 Python 依赖
uv run octop init             # 初始化本机控制面（含数据库迁移）

cd dashboard && npm install   # 控制台依赖
```

### 启动

```bash
uv run octop run              # API + Web 控制台（前台）
make dev                      # 同时起后端与控制台开发服务器
cd dashboard && npm run dev    # 仅控制台（vite --host）
```

### Docker

```bash
docker compose -f deploy/compose.production.yml up -d --build
```

生产编排包含 `worker` 服务（执行 Worker 独立成层）；单进程安装默认在 `octop run` 内托管该 Worker，
`OCTOP_WORKBUDDY_WORKER=off` 可关闭。

## 命令行

```bash
uv run octop init                  # 初始化控制面 + 迁移
uv run octop run                   # 启动 API 与控制台
uv run octop workbuddy worker       # 执行 Worker 层（需要 PostgreSQL；启动时执行迁移）
uv run octop workbuddy cel -e '1 + 1'   # 在有界 CEL 沙箱里求值一条表达式（不启动服务，用于验证契约）
```

## 接口速查

所有业务接口位于 `/api/v1` 之下，**权威清单**是 `contracts/route-manifest.json`（含每条路由的来源、成功状态码与授权要求；
`counts.source` 对应上游契约文档的路由数，其余为实施过程新增）。常用入口：

| 领域 | 入口 |
|---|---|
| 工作流 | `POST /workflows`、`POST /workflows/{id}/activate`、`POST /workflows/{id}/execute`、`GET /workflow-definitions/metadata` |
| 执行 | `GET /executions`、`GET /executions/{id}`（含逐节点排障与等待原因）、`POST /executions/{id}/cancel` |
| 审批 | `GET /approval-requests`、`POST /approval-requests/{id}/challenge`、`POST /executions/{id}/resume` |
| 提问（人在环） | `GET /executions/{id}/input-requests`、`POST /executions/{id}/input-requests/{rid}/answer`、`GET /input-requests` |
| 产出审核 | `POST /executions/{id}/output-review`、`GET /executions/{id}/output-review`、`POST /executions/{id}/output-review/decisions`、`GET /output-reviews` |
| 治理与运营 | `GET /tenant-capabilities`、`GET /jobs`、`GET /notifications`、`GET /audit-logs`、`GET /usage` |

## 验证

```bash
make lint          # ruff check + ruff format --check（src 与 tests）
make typecheck     # mypy src/octop
make test          # pytest -n N -m "not live"（不需要外部凭据的全部用例）

# 需要真实 PostgreSQL / Redis 的用例（CI 的 Live database tests 作业同一套）
OCTOP_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/octop_test \
OCTOP_TEST_REDIS_URL=redis://127.0.0.1:6379/0 \
uv run pytest -m "postgresql or redis"

cd dashboard && npx tsc -b && npx vitest run && npx eslint .
```

CI 会跑：`Python 3.12`、`Windows / Python 3.12`、`Live tests (real credentials)`、`Dashboard`、`CodeQL`。
其中 `Live database tests` 是**唯一**会覆盖"迁移 + RLS + 真实数据库行为"的作业——只跑单测不足以判定可用性。

## 部署要点

- **迁移**：`octop init`（以及 `octop workbuddy worker`、备份 / 管理命令）会应用迁移；PostgreSQL 与 SQLite 两侧的版本水位都保持单调，
  WorkBuddy 的表与执行事实只存在于 PostgreSQL，SQLite 侧只推进水位并 fail closed。
- **RLS**：WorkBuddy 的业务表都 `ENABLE` + `FORCE ROW LEVEL SECURITY`，策略基于事务内的 `app.tenant_id`；平台级（跨租户）操作走
  显式的平台上下文，不靠放宽策略。
- **角色**：本仓库不创建运行角色，也不对 `PUBLIC` 之外授予隐式权限；迁移只做 `REVOKE ALL ... FROM PUBLIC`，运行角色的创建属部署方。
- **Worker**：执行、发件箱派发与截止时间结算都需要 Worker 在跑；它们都是幂等可重入的。

## 当前边界

1. 连接器 / 模型网关适配器与触发器投递执行属**部署方接线**：未接线时相关步骤 fail closed（明确报依赖不可用），而不是静默返回空结果。
2. 发件箱只写不送：需要先注入发布端口，派发器才会把事件送出去；未配置时它拒绝运行，不会把没人收到的事件标记为已送达。
3. 租户级指标口径与生产注销 / 合规签署是**产品决策**，未落地前不应对外承诺。
4. 批次进度、契约偏差与集成结论记录在 `docs/plan/`（`two-machine-workstreams.md` 是计划，`ledger/` 是两台机器与集成者的状态）。

## 仓库结构

```
src/octop/
  api/routers/            HTTP 层（workbuddy_*.py 是 WorkBuddy 的接口）
  infra/workbuddy/        工作流编译 / 运行时 / 审批 / 知识库 / 提案 / 市场 / Worker
  infra/db/               数据访问与迁移（migrations/ 含 PostgreSQL 与 SQLite 两侧）
  infra/rbac/             对象级权限骨架（四层范围 + 显式授权）
  cli/                    命令行（含 octop workbuddy …）
contracts/                权威契约：workflow-v1.schema.json、route-manifest.json
dashboard/                控制台（React + Vite + antd）
docs/                     架构、API、CLI、配置与 ADR
tests/                    unit / integration（PG 与 Redis 标记的用例需真实依赖）
docs/plan/                交付计划、账本与集成结论
```

## 许可与上游

- 本仓库遵循 **MIT**（`LICENSE`，版权归上游 Octop）。本仓库的改动同样以 MIT 提供。
- 上游项目：[TencentCloud/Octop](https://github.com/TencentCloud/Octop)。本仓库与上游**不共享产品方向**；
  引用上游代码的部分仍受其许可与版权约束。
