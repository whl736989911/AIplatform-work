<p align="center">
  <img src="docs/assets/readme-banner-zh.png" alt="Octop Banner" width="600" />
</p>

<p align="center">
  <strong>支持多用户、多 Agent 的自托管 AI 助手 — 更聪明，更懂你。</strong>
</p>

<p align="center">
  <a href="https://trendshift.io/repositories/95504?utm_source=repository-badge&utm_medium=badge&utm_campaign=badge-repository-95504" target="_blank" rel="noopener noreferrer">
    <img src="https://trendshift.io/api/badge/repositories/95504" alt="TencentCloud/Octop | Trendshift" width="250" height="55" />
  </a>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white" /></a>
  <a href="https://github.com/TencentCloud/Octop/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green" /></a>
  <a href="https://github.com/TencentCloud/Octop/releases"><img alt="Version" src="https://img.shields.io/badge/version-0.9.33-orange" /></a>
  <a href="https://pypi.org/project/octop/"><img src="https://img.shields.io/pypi/v/octop" alt="PyPI" /></a>
  <a href="https://github.com/astral-sh/ruff"><img alt="Code Style: Ruff" src="https://img.shields.io/badge/code%20style-ruff-000000?logo=ruff&logoColor=white" /></a>
  <a href="https://github.com/TencentCloud/Octop"><img alt="GitHub stars" src="https://img.shields.io/github/stars/TencentCloud/Octop?style=social" /></a>
  <a href="https://github.com/TencentCloud/Octop/fork"><img alt="GitHub forks" src="https://img.shields.io/github/forks/TencentCloud/Octop?style=social" /></a>
  <a href="https://discord.gg/jPas5J8Ua"><img alt="Discord" src="https://img.shields.io/badge/Discord-%E5%8A%A0%E5%85%A5-5865F2?logo=discord&logoColor=white" /></a>
</p>

<p align="center">
  <a href="#-概述">概述</a> ·
  <a href="#-亮点">亮点</a> ·
  <a href="#-核心技术">核心技术</a> ·
  <a href="#-功能特性">功能特性</a> ·
  <a href="#-workbuddy">WorkBuddy</a> ·
  <a href="#-规划">规划</a> ·
  <a href="#-快速开始">快速开始</a>·
  <a href="#-目录">目录</a>
</p>

<p align="center">
  <a href="README.md">English</a> · <b>中文</b>
</p>

---

## 📌 概述

**Octop** 是一个开源、自托管的 AI 助手。它不仅是工具，更是可并行运作的数字生命体。通过多 Agent 架构，它为团队、家庭和个人构建了既独立又协作的智能环境。并且这一切都运行在你的机器上——完全自托管的设计让隐私不再是妥协，而单进程启动的便捷性，则让强大的 Web 控制台、CLI 与 IM 集成触手可及。

借助飞书、钉钉、QQ、Discord、企业微信或 HTTP/SSE/WebSocket API 与任意 Agent 对话；通过**专家库**一键创建专业角色，通过 **Connector**（OAuth + MCP）接入外部服务，通过 **ACP** 与 IDE / 终端 AI 工具双向协作。

> Octop 的设计目标：让每一次对话、工作区与凭据都留在你自己的机器上，同时为每个用户配备一组可按场景切换的专业 Agent。

## ✨ 亮点

| | 特性 | 说明 |
|---|------|------|
| 👥 | **多用户多 Agent 专家团** | 一人管理，全家共用；内置专家库，按场景切换专业角色 |
| 🎭 | **MBTI 人格** | 16 种人格模板与互动测试，为每个 Agent 赋予鲜明性格 |
| 🔒 | **更安全** | JWT 多用户隔离、工具审批、Shell 命令防护与敏感信息脱敏，数据留在本地 |
| 🔌 | **Connector 拓展体系** | 一键接入腾讯全家桶（文档 / 微博 / 新闻等），OAuth 与 MCP 网关轻松扩展 |
| 💾 | **可插拔后端存储** | 本地目录、Docker 容器、PostgreSQL 或 COS/S3，AI 在隔离边界内操作 |
| 🧠 | **可迁移记忆系统** | 基于 harness-memory，记忆随工作区迁移 |
| 📚 | **知识库** | 基于文档的 RAG 检索，让 Agent 的回答锚定你的私有知识库 |
| 🧩 | **插件** | 支持第三方插件扩展；内置插件随安装注入，按需一键启用 |
| ↔️ | **ACP 双向集成** | `octop acp` 增强 IDE 与终端 AI；对话中委派 OpenCode / Claude Code 等 |
| 💻 | **终端 AI+** | 浏览器内交互式 Shell，AI 辅助命令执行与排障 |
| 🌐 | **浏览器 AI+** | 基于 Chromium 的无头浏览器会话，支持网页自动化、截图与远程操控 |
| 🖥️ | **远程桌面** | 控制台内实时看屏与键鼠操控，跨 Linux / Windows / macOS；适合远程办公、GUI 软件操作，无图形 Linux 可一键搭建隔离桌面 |
| 🏢 | **WorkBuddy 企业多租户工作流** | 同一份进程内的企业面：租户/部门/角色、可版本化的工作流、审批、知识库、触发器与模板市场——按需开启，依赖 PostgreSQL |
| ⚙️ | **可恢复的执行层** | 被接纳的执行由 Worker 从 PostgreSQL 领取：租约 + 单调递增 fencing、租户并发上限、受工具声明约束的重试预算，以及至少一次投递的 outbox |
| 🏠 | **可自托管** | 一条 `octop run` 即可运行控制台、CLI、IM 通道与定时任务，数据存于 `~/.octop/` |

<details>
<summary>🐾 你能用 Octop 做什么</summary>

- **个人助理** — 让专属 Agent 帮你写周报、整理资料、定日程，记忆随工作区长期保留。
- **家庭共享** — 一个管理员账号，全家共用；按成员分配不同 Agent 与专家角色。
- **团队助手** — 多 Agent 并行协作，对接飞书 / 钉钉 / 企业微信，把任务自动分发到群里。
- **开发者增效** — 通过 ACP 把编码任务委派给 OpenCode / Claude Code，或在终端用 AI 辅助排障。
- **网页自动化** — 用浏览器 AI+ 自动填表、截图、采集公开信息。
- **定时任务** — 用自然语言配置 Cron，让 Agent 每天按时推送或执行任务。

</details>



## 🧠 核心技术

| 层级 | 技术 |
|------|------|
| **语言** | Python 3.12+ |
| **Web 框架** | FastAPI + uvicorn |
| **Agent 运行时** | harness-agent |
| **IM 桥接** | harness-gateway |
| **控制平面数据库** | SQLite (WAL，默认) 或 PostgreSQL（可选） |
| **前端** | React 18 + TypeScript + Vite + Ant Design |
| **调度** | APScheduler |
| **ACP** | agent-client-protocol |
| **构建 / 质量** | hatchling · ruff · mypy · pytest |

Octop 基于一系列 Harness 工程实践构建——它将这些专注的运行时组合进同一个进程：

- **harness-agent** — Agent 运行时：模型路由、工具、技能与对话检查点。
- **harness-gateway** — 多平台 IM 通道桥接，将各类入站消息归一为统一的处理管线。
- **harness-memory** — 分层记忆与全文检索，让 Agent 的记忆随工作区一同迁移。
- **harness-browser** — 基于 CDP 的浏览器自动化，支持持久化配置，用于网页类任务。

Octop 不依赖外部消息队列或中间件，而是通过进程内的 `HarnessProcessor` 统一路由所有入口——Web UI、IM 与定时任务。最终呈现为一个可重启恢复的单进程：启动时整个状态都从控制面数据库重建（默认 `~/.octop/octop.db`，亦可配置 PostgreSQL）。

## 🤔 功能特性

### 服务器与认证
- 多用户 JWT 认证，支持管理员角色
- 首次运行向导（`octop init`）
- 交互式 API 文档：`/api/docs`（默认关闭 — 在 `config.json` 中设置 `"enable_api_docs": true` 开启）

### Agent
- 每位用户可创建多个 Agent；各自拥有独立工作区、供应商、通道和定时任务
- 16 种 MBTI 人格模板 + 自定义系统提示词
- 启动时扫描专家库（`infra/agents/experts/library/`）
- 工作区后端：本地磁盘、COS、S3 及其他远程存储

### 通道与自动化
- IM 通道：飞书、钉钉、QQ、Discord、企业微信等
- 主动定时任务，支持自然语言和斜杠命令触发
- Web UI、IM、定时任务共用同一套消息处理链路

### 使用入口
- **Web 控制台** — 对话、Agent 管理、连接器、通道、定时任务、设置
- **CLI** — `octop run`、`octop chat`、`octop acp`、管理命令
- **HTTP/SSE/WebSocket API** — 完整的程序化访问能力

### 知识库与插件
- **知识库** — 基于文档的 RAG 检索；上传文件后，语义检索让 Agent 的回答锚定你的私有知识库
- **插件** — 安装并管理第三方插件（`octop plugin`）；内置插件随安装注入，按需在控制台一键启用

### WorkBuddy（企业多租户工作流）

同一进程内的可选企业面：一个部署服务多个租户，每个租户有自己的成员、凭据、工作流与数据边界。它需要 PostgreSQL 控制平面；单机安装把执行 Worker 托管在进程内，拆分部署则把 Worker 作为独立层级运行。

- **租户与治理**——租户、部门、成员关系、角色、邀请、能力与配额设置；每张租户表都由行级安全隔离，接口在每次请求时从数据库校验调用者当前角色
- **工作流作者面**——JSON 定义被编译为规范形式（边界、工具授权、知识引用与 schema 都会校验），发布为不可变版本，并按灰度桶激活；激活以修订号做 CAS
- **持久化执行**——接纳执行只写入 `queued`；Worker 在一个事务内领取（锁租户行 → 并发上限 → 占运行槽 → 取租约并单调递增 fencing token → `running`），因此多副本无需协调，Worker 崩溃后租约到期即被接管
- **审批**——节点可要求指定审批人；决定是一次性令牌，重试或重放的请求无法重复批准
- **工具治理**——平台工具注册表声明每个工具的影响面（`read_only` / `external_write`）、幂等与结果查询能力、沙箱验证状态，以及输入/输出的 JSON Schema（Draft 7）；引擎在重试或采信结果前先查该租户已授权的修订
- **知识库**——绑定 Embedding 模型、接收上传、把文档索引为原子世代并支持检索；索引本身是一条可追踪的作业
- **触发器**——Webhook 与 cron 注册，支持原始报文签名、时间窗容差与持久化去重键
- **改进闭环**——提案携带生成的补丁、风险等级与审批算术；影子回放真实录制，灰度门禁按真正跑过的执行来判定，通过后才允许提升
- **模板市场**——工作流模板的发布、评审、安装与升级
- **作业与事件**——对外可见的操作统一记录在作业表（类型、状态、尝试次数、结果），响应丢失后仍可找回；已提交的事实通过传输无关的 outbox 派发器至少一次投递

接口与命令行速查见下方 [WorkBuddy](#-workbuddy)。

### ACP（Agent Client Protocol）

Octop 支持两个方向的 ACP 集成：

1. **入站** — 外部工具使用**你的** Octop Agent
   ```bash
   octop acp --agent main   # 为 Zed、OpenCode 等提供 stdio ACP 服务
   ```

2. **出站** — Octop 委派给外部编程 Agent
   - 控制台 → **ACP**（`/acp`）：配置 Runner（按用户全局）
   - 为 Agent 启用 **acp_runner** 后，在对话中委派任务

内置出站 Runner 包括 OpenCode、CodeBuddy、Claude Code 和 Codex。

完整配置：**[docs/acp.md](docs/acp.md)**。

## 🏢 WorkBuddy

WorkBuddy 是 Octop 的企业层：一套运行在同一进程、同一控制平面内的多租户工作流平台，把 Octop 从个人助手扩展成企业可以部署的受治理系统。它是**按需开启**的——不开启不影响个人版功能。

### 组成

| 层级 | 位置 | 职责 |
|------|------|------|
| 接口 | `src/octop/api/routers/workbuddy_*.py` | `/api/v1` 下的租户、工作流、执行、审批、知识库、提案、市场与生命周期接口 |
| 领域 | `src/octop/infra/workbuddy/` | 编译器、策略、运行时、Worker、outbox 派发器、知识库、提案、市场、生命周期、CEL 沙箱 |
| 数据 | `src/octop/infra/db/migrations/015…029` | 租户级 schema，`ENABLE`/`FORCE ROW LEVEL SECURITY`、不可变版本、租约与 fencing、作业、outbox |
| Worker | `octop workbuddy worker` | 领取并运行被接纳的执行；API 层不持有队列，也不在进程内保存执行状态 |

### 接口速查

企业面接口与个人版同在 `/api/v1` 下：

```bash
GET  /api/v1/tenants                       # 租户、成员、邀请、部门
POST /api/v1/workflow-definitions/validate # 入库前先编译校验
POST /api/v1/workflows/{id}/execute        # 接纳一次运行：202 + execution id
POST /api/v1/workflows/{id}/activate       # 在灰度桶中激活版本
POST /api/v1/workflows/{id}/rollback
GET  /api/v1/executions/{id}               # 步骤级状态、token 用量、派发意图
POST /api/v1/executions/{id}/cancel
GET  /api/v1/approval-requests/{id}
POST /api/v1/knowledge-bases/{id}/documents
GET  /api/v1/knowledge-bases/{id}/search
POST /api/v1/improvement-proposals         # 已编译的变更 + 风险等级 + 审批数
POST /api/v1/improvement-proposals/{id}/promote
POST /api/v1/marketplace/templates/{id}/install
GET  /api/v1/jobs/{job_id}                 # 请求创建的作业，响应丢失后仍可找回
GET  /api/v1/audit-logs
```

### 命令行

```bash
octop workbuddy cel -e 'execution.status == "success"' --context '{"execution": {"status": "success"}}'
octop workbuddy worker          # 运行执行层（需要 PostgreSQL）
octop workbuddy dependencies    # 探测锁定组件与已配置依赖（0 正常 / 2 失败 / 3 阻塞）
```

### 部署

- **数据库**：WorkBuddy 必须使用 PostgreSQL（个人面仍默认 SQLite）；知识库向量需要 `vector` 扩展，且迁移不会自行创建它——这是控制平面的前置条件
- **中间件**：Redis 承载接口限流（`REDIS_URL`）；执行队列本身在 PostgreSQL，因此 Redis 故障不会丢失已接纳的运行
- **Worker 层级**：单机安装默认在进程内托管（`OCTOP_WORKBUDDY_WORKER=on`）；拆分部署在 API 上设 `OCTOP_WORKBUDDY_WORKER=off`，改用 `deploy/compose.production.yml` 的 `worker` 服务
- **迁移**：`015_workbuddy_*` … `029_workbuddy_*` 建立企业 schema，在控制平面打开时应用；生产拓扑先跑 `deploy/scripts/migrate.sh` 的 `migrate` 作业，它会失败即停地复核：后端确为 PostgreSQL、`vector` 扩展已安装、每张租户表都已 ENABLE 且 FORCE 行级安全

### 验证

WorkBuddy 由合同驱动的测试覆盖，运行在真实 PostgreSQL（限流相关还需真实 Redis）上：

```bash
pytest tests/unit/workbuddy tests/integration/test_workbuddy_runtime_postgres.py \
       tests/integration/test_workbuddy_proposals_postgres.py \
       tests/integration/test_workbuddy_business_smoke.py
```

### 当前边界

- 连接器适配器、模型网关适配器与触发器投递执行属于部署方接线；引擎侧（声明、重试策略、派发意图、对账）已就绪
- outbox 派发器需要先接上发布端口，事件才会离开数据库；未接时它拒绝运行并把事件留在 `pending`，而不是谎报已送达
- 租户级指标口径与生产注销的最终合规签署属于产品决策，不是代码默认值

## 🧭 规划

以下是我们的中长期规划：

- [ ] **资源共享池** — 构建技能 / 子智能体共享池，用户在创建专家时可直接取用，无需从零搭建。
- [ ] **专家共享** — 支持用户将自有专家共享给其他用户使用，沉淀优秀配置、避免重复造轮子。
- [ ] **浏览器与终端能力补全** — 增强浏览器技能*录制*（把操作流程录制为可复用技能），并完善终端 AI 助手能力。
- [ ] **AgentTeams** — 支持协调者自主调度、编排多位专家，协同完成复杂多步骤任务。
- [ ] **自进化能力** — 将日常对话自动沉淀为技能，让助手随使用不断成长。
- [ ] **PC / 移动端客户端** — 在 Web 控制台与 IM 通道之外，提供原生桌面与移动端应用。

规划会随社区发展动态调整，以上仅供参考。

## 🚀 快速开始

### 环境要求

- **macOS / Linux / Windows**
- 无需预先安装 Python — 安装脚本通过 [uv](https://docs.astral.sh/uv/) 在 `~/.octop/` 下创建隔离的 Python 3.12 虚拟环境
- 现代多核 CPU，并预留数 GB 内存供进程与模型/Embedding 缓存使用；磁盘需容纳数据库、Agent 工作区与文档语料

### 1. 安装

**macOS / Linux** — 一键安装（推荐）：

```bash
curl -fsSL https://finnie-1258344699.cos.ap-guangzhou.myqcloud.com/octop/install.sh | bash
```

**Windows（PowerShell）**：

```powershell
irm https://finnie-1258344699.cos.ap-guangzhou.myqcloud.com/octop/install.ps1 | iex
```

**Windows（cmd）** — 下载后运行，或从已克隆的仓库执行：

```bat
curl -fsSL https://finnie-1258344699.cos.ap-guangzhou.myqcloud.com/octop/install.bat -o install.bat
install.bat
```

安装完成后，请打开**新终端**，或重新加载 shell 配置：

```bash
source ~/.zshrc   # Zsh
# 或
source ~/.bashrc  # Bash
```

安装脚本会将 `octop` 加入 PATH（`~/.octop/bin`）。可选附加组件：

```bash
# 浏览器自动化（Playwright Chromium）
curl -fsSL https://finnie-1258344699.cos.ap-guangzhou.myqcloud.com/octop/install.sh | bash -s -- --extras browser

# 飞书通道支持
curl -fsSL https://finnie-1258344699.cos.ap-guangzhou.myqcloud.com/octop/install.sh | bash -s -- --extras channels-feishu
```

完整安装选项见 [scripts/README.md](scripts/README.md)（`--version`、`--from-source`、`--mirror` 及 Windows 参数）。

**桌面客户端**（图形界面，无需终端）— 从 [GitHub Releases](https://github.com/TencentCloud/Octop/releases/latest) 下载对应平台的安装包：

| 平台 | 制品 |
|------|------|
| Windows | `Octop-desktop-windows-amd64-<version>.exe`（64 位）/ `Octop-desktop-windows-arm64-<version>.exe`（ARM64）— NSIS 安装程序 |
| macOS | `Octop-desktop-darwin-arm64-<version>.dmg`（Apple 芯片）/ `Octop-desktop-darwin-amd64-<version>.dmg`（Intel） |
| Linux | `Octop-desktop-linux-amd64-<version>.tar.gz` / `Octop-desktop-linux-arm64-<version>.tar.gz` |
| 飞牛 NAS（FnOS） | `Octop-fnos-docker-<version>.fpk`（依赖 Docker）/ `Octop-fnos-native-<version>.fpk`（无需 Docker）— 通过应用中心安装 |

桌面客户端说明见 [desktop/README.md](desktop/README.md)，飞牛打包指南见 [fnos/README.md](fnos/README.md)。

**备选 — PyPI**（若你已自行管理 Python 环境）：

```bash
pip install octop
# 可选：pip install "octop[browser]"
# 可选本地 ONNX Embedding 模型缓存（设置 → 模型 → 本地）：pip install "octop[local-embedding]"
# 仅下载目录模型到 ~/.octop/embedding_models；不用于对话，也不接入 Memory。
```

从源码用 uv 开发时：

```bash
uv sync --extra local-embedding
```

### 2. 启动

```bash
# 前台运行（API + Web 控制台）
octop run

# 自定义主机与端口
octop run --host 0.0.0.0 --port 8088

# 注册为系统服务（systemd / launchd / Windows 服务）
octop service start
```

打开 **http://127.0.0.1:8088**。Docker 首次初始化会自动生成随机管理员密码（写入 `/data/.octop/credential.txt`），除非设置了 `OCTOP_DEFAULT_PASSWORD`。交互式 `octop init` / 设置向导会让你自行设置密码（至少 8 位，且同时包含字母和数字）。

### Docker（推荐用于生产部署）

```bash
# 构建并启动
docker compose -f docker/docker-compose.yml up -d

# 或手动构建
bash docker/docker_build.sh
docker run -d \
  -p 8088:8088 \
  -v octop-data:/data/.octop \
  -e HOME=/data \
  -e OCTOP_DEFAULT_PASSWORD="<自定义强密码，留空则自动生成随机密码>" \
  octop:latest
```

打开 `http://localhost:8088`。首次初始化会创建管理员账号，并把凭据写入容器内 `/data/.octop/credential.txt`。未设置 `OCTOP_DEFAULT_PASSWORD` 时自动生成随机强密码；自行设置的密码须 ≥8 位且同时包含字母和数字（被应用密码策略拒绝的常见弱密码会自动回退为随机密码）。可通过 `OCTOP_ADMIN_USERNAME` 覆盖用户名。

> **密码策略：** 至少 8 位，且同时包含字母和数字。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OCTOP_PORT` | `8088` | HTTP 监听端口 |
| `OCTOP_DEFAULT_PASSWORD` | _(未设置)_ | 首次运行管理员密码（Docker 引导）。未设置 = 自动生成随机密码并写入 `credential.txt` |
| `OCTOP_ADMIN_USERNAME` | `admin` | 首次运行管理员用户名 |
| `OCTOP_DATA` | `~/.octop` | 宿主机数据目录（compose 挂载） |

完整变量列表见 [`.env.example`](.env.example)。


## 📑 目录

- [亮点](#-亮点)
- [概述](#-概述)
- [核心技术](#-核心技术)
- [功能特性](#-功能特性)
- [WorkBuddy](#-workbuddy)
- [规划](#-规划)
- [快速开始](#-快速开始)
- **部署与使用**
  - [安装方式](#-安装方式)
  - [配置](#-配置)
  - [CLI 参考](#-cli-参考)
  - [Web 控制台](#-web-控制台)
  - [数据目录](#-数据目录)
- **架构与开发**
  - [架构](#-架构)
  - [项目结构](#-项目结构)
  - [开发](#-开发)
- **项目信息**
  - [安全与隐私](#-安全与隐私)
  - [参与贡献](#-参与贡献)
  - [更新日志](#-更新日志)
  - [相关项目](#-相关项目)
  - [客户企业微信群](#-客户企业微信群)
  - [许可证](#-许可证)

### 📦 安装方式

| 方式 | 平台 | 说明 |
|------|------|------|
| 远程一键安装 | macOS / Linux | `curl …/octop/install.sh \| bash` |
| 远程一键安装 | Windows | `irm …/octop/install.ps1 \| iex` 或 `install.bat` |
| 本地脚本 | macOS / Linux | `bash scripts/install.sh` |
| 本地脚本 | Windows | `scripts\install.bat` 或 `install.ps1` |
| PyPI | 全平台 | `pip install octop` 或 `pip install "octop[browser]"` |
| Docker | 全平台 | `docker/docker-compose.yml` |

所有安装脚本均在 `~/.octop/venv` 创建隔离环境，并通过 `~/.octop/bin/octop` 包装 CLI，不会影响系统 Python。

### 升级

`octop update` 只替换 wheel / 二进制，你的 `~/.octop/` 数据库、工作区、密钥与 `config.json` 均会保留：

```bash
octop update          # 获取并安装最新版 Octop，若已注册系统服务则自动重启
```

数据库结构会在下次启动时自动迁移；仅当设置向导提示需要迁移时才运行 `octop init`。跨版本升级前请务必先备份（`octop backup`）。

### ⚙️ 配置

所有运行时数据存放在 `~/.octop/`。可通过 CLI 管理，也可直接编辑文件。

```bash
# LLM 供应商与模型
octop models
octop provider list

# IM 通道
octop channel list
octop channel install

# Skill（按 Agent）
octop skills list --agent main

# 定时任务
octop cron list
octop cron create --help

# 用户（管理员）
octop user list
```

### 支持的 LLM 供应商

OpenAI 兼容 API、DashScope（千问）、Ollama 等预设 — 在控制台或通过 `octop provider` 按 Agent 配置。

### 支持的通道

| 通道 | 所需凭证 |
|------|----------|
| **飞书** | App ID、App Secret |
| **钉钉** | App Key、App Secret |
| **QQ** | Bot AppID、Token |
| **Discord** | Bot Token |
| **企业微信** | Corp ID、Agent Secret |
| **Web 控制台** | 默认启用 |

### 📖 CLI 参考

| 命令 | 说明 |
|------|------|
| `octop init` | 初始化 `~/.octop/`（数据库、管理员、JWT 密钥） |
| `octop run` | 前台启动 Octop |
| `octop service start` | 安装并启动系统服务 |
| `octop service stop` | 停止系统服务 |
| `octop agent` | 创建、列出、启停 Agent |
| `octop channel` | 安装与管理 IM 通道 |
| `octop chats` | REPL 与会话管理 |
| `octop acp` | 为 IDE 提供 stdio ACP 服务 |
| `octop cron` | 管理定时任务 |
| `octop models` | 供应商预设与模型解析 |
| `octop skills` | 按 Agent 启用/禁用 Skill |
| `octop plugin` | 安装并管理第三方插件 |
| `octop backup` | 导出 / 恢复备份 |
| `octop clean` | 清理 CLI 状态或清空 `~/.octop/` |
| `octop update` | 检查并安装更新 |

完整参考：**[docs/cli.md](docs/cli.md)**。

### 🖥️ Web 控制台

`octop run` 启动后访问 **http://127.0.0.1:8088**。

<p align="center">
  <img src="docs/assets/readme-chat-zh.png" alt="Octop Web 控制台" width="800" />
</p>

- **对话** — 与 Agent 实时聊天
- **Agent** — 创建 Agent，选择专家库 / MBTI 人格，配置供应商
- **Connector** — OAuth 应用与 MCP 网关
- **通道** — IM 平台配置
- **定时任务** — 可视化 Cron 管理
- **知识库** — 管理文档语料与语义检索
- **插件** — 安装、启用与配置插件
- **ACP** — 配置出站编程 Agent Runner
- **设置** — 用户、安全、TLS、系统

交互式 API 文档：**http://127.0.0.1:8088/api/docs**（默认关闭 — 在 `config.json` 中设置 `"enable_api_docs": true` 开启）

### 📁 数据目录

```
~/.octop/                          ← 安装与数据根目录
├── config.json                    # 进程配置（含可选 database 段）
├── octop.db                       # 默认 SQLite 控制面（用户、Agent、通道、定时任务 …）
├── secrets/                       # JWT 密钥、通道 Token
├── agents/<agent_id>/             # 各 Agent 工作区（SOUL.md、skills …）
├── security/tool_guard/           # Shell 命令允许/拒绝规则
├── logs/                          # 运行日志
├── venv/                          # uv 管理的 Python（安装脚本布局）
└── bin/octop                      # PATH 包装脚本 → venv/bin/octop
```

控制面也可改用 PostgreSQL（`config.json` → `database`，或 `OCTOP_DATABASE_*` / 首次设置向导）。控制面为 PostgreSQL 时，Agent 记忆**默认复用同一 DSN**（per-agent schema）；若需继续用文件记忆，在 agent 配置里设 `"memory": { "backend": { "type": "sqlite" } }`。详见 [docs/configuration.md](docs/configuration.md) 与 [docs/adr/002-database-backends.md](docs/adr/002-database-backends.md)。

环境变量与 `config.json` 详见 [docs/configuration.md](docs/configuration.md)。

### 🏗️ 架构

```
OctopServer
 ├─ DatabasePool         SQLite (WAL) 或 PostgreSQL
 ├─ SharedServices       依赖注入根 — 所有 repo 与配置
 ├─ ExpertCatalog        启动时扫描 agents/experts/library/
 ├─ UserManager
 │   └─ HarnessAgentManager（按用户）
 │       └─ AgentRuntime（按 Agent）
 │           ├─ HarnessAgent      Agent 运行时（harness-agent）
 │           ├─ HarnessProcessor  IM / UI / 定时任务入口
 │           ├─ ChannelManager    IM 连接（harness-gateway）
 │           └─ CronManager       APScheduler
 └─ FastAPI app (uvicorn)
```

单进程架构。重启后从控制面数据库重建状态（默认本地 SQLite；可选 PostgreSQL）。

详见 [docs/architecture.md](docs/architecture.md)、[docs/adr/001-single-process-model.md](docs/adr/001-single-process-model.md) 与 [docs/adr/002-database-backends.md](docs/adr/002-database-backends.md)。

### 📁 项目结构

```
src/octop/
  config.py    环境变量配置
  launch.py    OctopServer 启动 + uvicorn
  infra/       业务核心（agents、gateway、cron、db、users …）
  api/         HTTP 层 — FastAPI 路由、JWT、SSE
  cli/         CLI 层 — Click 命令
  dashboard/   构建后的 React SPA（wheel 产物）

dashboard/     前端源码（Vite）— 在此编辑，运行 make build-frontend

docker/        Docker Compose、入口脚本、构建与部署脚本
tests/         unit/ + integration/
```

### 🛠️ 开发

**前置条件：** Python 3.12+、Node 18+、[uv](https://docs.astral.sh/uv/)

```bash
# 后端
make install          # pip install -e ".[dev]"
make all              # format-all + lint + typecheck + test（发布门槛）

# 前端（另开终端）
make dev-frontend     # Vite 开发服务器 :5173
make build-frontend   # 生产构建 → src/octop/dashboard/
cd dashboard && npx tsc -b
```

单独执行：`make test`、`make lint`、`make typecheck`、`make format`。


### 🔒 安全与隐私

- **本地优先**：配置、对话、工作区与凭证均存储在 `~/.octop/`。
- **多用户隔离**：JWT 认证，按用户隔离 Agent 与工作区。
- **敏感信息脱敏与工具审批**：离开工作区前自动脱敏敏感数据；高风险工具或 Shell 命令需依据护栏规则显式审批。
- **工具护栏**：可在 `~/.octop/security/tool_guard/` 编辑 Shell 命令规则。
- **无厂商锁定**：可自由切换 LLM 供应商、存储后端与 IM 通道。

### 🤝 参与贡献

欢迎贡献代码：

1. Fork 本仓库
2. 创建功能分支（`git checkout -b feature/amazing-feature`）
3. 提交前运行 `make all`（后端）或 `make check-all`（全栈）
4. 发起 Pull Request

完整指南见 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题见 [SECURITY.md](SECURITY.md)。

模块边界与编码规范见 [AGENTS.md](AGENTS.md)。

### 📋 更新日志

详见 [CHANGELOG.md](CHANGELOG.md)。

### 🔗 相关项目

| 项目 | 描述 |
|------|------|
| harness-agent | Agent 运行时 — 模型路由、工具、Skill、检查点 |
| harness-gateway | 多平台 IM 通道桥接 |
| harness-memory | 层级召回与全文搜索 |
| harness-browser | CDP 浏览器自动化，支持 profile 持久登录 |

> 这些 `harness-*` 项目正在筹备开源中，仓库地址将在发布后补充。

### 💬 客户企业微信群

如需加入客户企业微信服务群，请扫码：

<p align="center">
  <img src="docs/assets/qrcode.png" alt="客户企业微信服务群二维码" width="220" />
</p>

> 请扫码进入工作群，如有任何疑问或需求，请直接联系群管理员对接处理。

### 📄 许可证

本项目采用 [MIT License](LICENSE)。

### ✨ 贡献者

感谢所有贡献者：

<a href="https://github.com/tencentcloud/octop/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=tencentcloud/octop" />
</a>
