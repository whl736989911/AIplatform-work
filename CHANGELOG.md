# Changelog

本文件记录项目的所有重要变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)，版本号遵循 [语义化版本规范](https://semver.org/spec/v2.0.0.html)。

## [Unreleased]

### 新增

- WorkBuddy 租户**岗位授权**（迁移 052）：新增 `workbuddy_tenant_duty_grants`，把五种岗位——作者 `author`、发布者 `publisher`、审批人 `approver`、库管理员 `kb_admin`、运维 `ops`——按「公司 / 部门（含子部门）/ 个人」三种主体授予；`GET /duties`、`POST|DELETE /duties/{duty}/grants`（租户 admin）。岗位只**加路不夺权**：租户管理员隐式持有全部岗位，原本需要管理员的知识库治理与平台许可路由改为「管理员或库管理员/运维」，工作流的保存与发布额外接受作者/发布者岗位——员工不必提权即可干活。
- WorkBuddy **默认可见性回填**（迁移 053）：B-02 之前创建的工作流没有权限行——列表靠 legacy 回退仍算公司可见，但权限模型本身看不到它，发布者或管理员按模型读会读不到。上线前给每一条既有工作流补齐它本来就隐含的 `enterprise` 行（`workbuddy_object_scopes`），语句幂等、可重复运行；已跳过该文件的库由 `migrate.py::_ensure_workbuddy_visibility_backfill` 补齐——上线不「丢东西」。
- WorkBuddy **补丁式生成**（A-15 收尾）：同一份受限编排稿现在也能**改**工作流——每步带 `op`（`add`/`update`/`remove`），降级为**治理链原生的 JSON Patch**（`/nodes/<i>`、`/edges/<j>`），于是「用一句话改」与「人手工改」走**完全相同**的审核、策略、风险分级与推广路径。候选定义由**提案链自己的** `apply_patch` 构建（不是另写一个应用器），因此「这里编译通过的，链上一定应用得上」是结构性保证而非巧合。两个刻意的语义决定：**删除按索引降序发出**（RFC 6902 的位移陷阱：升序删除会删错元素）；**`update` 保留既有 `save_as`**（产出键是下游与历次运行认这个步骤的方式，一段散文式描述不是改名请求）。新端点 `POST /workflows/{id}/authoring-proposals`（与创建提案同权，记 `improvement_proposal` 作业）：描述→补丁→提案，**永不直接写入**——改动落在候选版本里，人评审后才推广；改动若无编译通过（例如删掉唯一步骤使图不成立），按 400 带**编译器原生诊断**回报且不产生任何提案。单元 17 例（含索引位移、依赖随步骤替换、产出键保持、两入口被拒）+ 端到端 2 例（描述→候选版本待审提案、改坏图→400 且提案列表为空）。

- WorkBuddy **「用一句话新建」**（A-15 前端）：工作流定义面板新增入口与弹窗——写一段描述 → 调 `POST /workflow-authoring` → 生成的**草稿**里列出每一步 `id`、作者给该步写的 `purpose`（可解释输出）与所用编译轮数；确认后打开草稿做人工确认，发布仍走既有流程。**落败时展示的是编译器自己的诊断**（`code`/`message`/`path` 逐条列出，并按 `code` 走既有错误码翻译），而不是一句「生成失败」——这正是这条链路的价值所在：模型说不清楚的地方，编译器说得清楚。顺带修掉一处与 A-06 记录不符的实现：向导的步骤类型是**硬编码候选表**（选项虽被元数据过滤，但 schema 声明而表里没有的类型会**选不到**），现在类型**集合**来自 `definition_metadata()`，数组只决定展示顺序，元数据新增类型会自动出现。弹窗 3 例（发送描述并解释步骤、拒绝时展示编译器诊断、空描述不发送）。

- WorkBuddy **自然语言作者路由**（A-15 接口）：新增 `POST /workflow-authoring`（与创建工作流同权）——描述进、**草稿**出（`active_version_id` 仍为空），发布照旧走既有的 `/workflows/{id}/activate`，因此「人工确认」是流程事实而不是提示语。返回体除管理员视图与该版本定义外，额外带 `authoring`：每步 `purpose`（模型自己的解释）与所用轮数。模型调用发生在**事务之外**（慢模型不该占着租户事务思考），随后在同一租户事务内用 `PostgresWorkflowSemanticResolver` 以 `require_semantic_resolution=True` 做**权威校验**并落库：草稿里点了一个没被授权的工具，会在这里被拒，而不是等到运行时。两轮仍编译不过 → **400 + 编译器原生诊断**（`details.rounds`/`details.diagnostics`）且**不落任何工作流**；未接线作者模型 → 503。端到端 3 例（描述→草稿→经既有发布路由激活；两轮失败→400 带诊断且列表里查不到；未接线→503）；顺带补齐清单一致性检查：把 `/attribution`、`/improvement-analysis` 登记为「他模块路由」，并让 `/workflow-authoring` 也纳入清单比对（新路由从此不会漏检）。

- WorkBuddy **受限 DSL 与编译闭环**（A-15 核心，让模型「描述」工作流而不是「写」工作流）：新增 `octop.infra.workbuddy.authoring`。产品契约里那句「运行时只执行校验器接受的 definition、不直接执行模型生成的任意代码」在这里落地为一条通道：模型产出的是**受限编排稿**（`trigger`/`inputs`/`steps`，每步只有 `id`/`kind`/`purpose`/`uses`/`config`），它经**校验**与**确定性降级**变成 definition，再由**同一个编译器**裁决——模型始终碰不到图结构。限制落在编排稿上：`kind` 必须存在于**由 schema 导出的** `definition_metadata()`（不手写第二份词表）、`config` 键必须是该类型**声明过的**字段（多一个就拒）、步骤 id 必须满足 schema 的标识符模式且唯一、`uses` **只能指向前面的步骤**（于是环在表达上就不存在，文档顺序即拓扑序）、模板引用只能指向更早的步骤或已声明输入。作者写 `{{ steps.<id> }}`，降级时机械翻译为运行时的 `{{ nodes.<id> }}`，「模型写的形状」永远不是「运行时读的形状」。**编译闭环**：编译器拒绝时，把它自己的诊断（`code`/`message`/`path`/`node_id`）回给模型再试一次，**至多 2 轮**（一次起草 + 一次修正；第三轮说明模型没有在读诊断）；两轮仍不过就**如实报失败并带上诊断**，绝不下沉一个未经证明的定义。可解释输出随结果返回：每步的 `purpose`（模型自己的话）、用了几轮、以及失败时的诊断。模型端口 `AuthoringSource`（`draft`/`revise`）与工具/知识/分析器同款，可注入、可离线测试。单元 9 例（描述→可编译 definition 且引用被翻译；编译器拒绝→诊断回流→一次修好；两轮仍失败→报错不存储；前向引用/未知类型/未声明字段/缺必填字段/重复 id 一律拒）。

- WorkBuddy **改进分析器**（A-14，把纠正接上既有治理链）：新增 `octop.infra.workbuddy.improvement`，把 A-13 的纠正簇变成**治理链原生语言（JSON Patch）**的建议，并交给既有的提案服务去编译、查策略、定风险等级——分析器**不写工作流**，它只提提案，所以一条错建议最坏的结果是「一份没人愿意审的待审提案」。三条护栏：**证据阈值**（同一处只被改过一次是数据点、两次才是模式，阈值下的簇只报 `skipped` 且**根本不调用分析器**，不为一句「不行」付模型钱）；**目标限定**（建议只能指向被分析过的那一步，别的都是模型在回答另一个问题）；**拒绝照录**（链条拒了就记下 code/message 并继续下一条，不绕过它）。分析器本身是**可注入端口**（与工具、知识库同款），未接线且确有证据时按 `DEPENDENCY_UNAVAILABLE` **fail-closed**（503），而「分析后无事可做」不是错误。新增 `POST /workflows/{id}/improvement-analysis`（租户 admin，按契约 §4.6.2 记为 `improvement_proposal` 作业，202 返回 `created`/`rejected`/`skipped` 与所分析的版本；提案服务新增只读的 `current_revision`，让过期修订在动手前就以 409/404 拦下）。单元 8 例 + 端到端 3 例（两次纠正→一份 `pending` 提案且能在提案列表里查到；未接线→503；仅一次纠正→`skipped` 且分析器零调用）；顺带把 runtime 路由的 actor 构造助手公开复用（`runtime_actor`），未改任何既有行为。

- WorkBuddy **纠正归因与范围界定**（A-13，把「被改了」变成「该改哪」）：新增分析模块 `octop.infra.workbuddy.attribution`，把上一批采集到的纠正按「哪个步骤的哪个键」聚成簇，并算出**范围**。方向按类别相反且这是刻意的：**纠正**针对的是运行**已经产出**的值，所以只向上看——共同产出它的那些步骤，才是修复唯一可能的落点（终态键的下游恒为空，往下看等于永远说「没人受影响」）；**补充事实**是本来缺失的输入，所以向下看——这个答案**解开了什么**。归因**按版本分组**：范围来自该版本实际执行的那张编译图，读版本时校验其存储 hash，因为改版会换掉图，把纠正挂到当前结构上会让人去修一个已经不存在的步骤；键在新版本里已消失的行**直接丢弃而不猜**。新端点 `GET /workflows/{id}/attribution`（**仅租户 admin**，因为这是分析器 A-14 的输入、以平台作业身份运行），每簇给出次数、涉及执行数、首末时间、范围节点与范围键，以及**至多 3 条**「改前→改后」样本（取最新的），按被纠正次数降序；单元 5 例 + 端到端 1 例（纠正 `polished` → 归因到步骤 `polish`、范围指向 `greet`/`greeting`；非管理员 403）。

- WorkBuddy **纠正/反馈采集**（A-12，改进闭环的原料）：人把工作流的产出改成了什么、以及人在运行中补充了什么事实，现在都留下**结构化记录**（`workbuddy_execution_feedback`，迁移 037）——每条都绑定到**产出它的那个工作流版本**（`workflow_id` + `workflow_version_id` + 版本 hash）与那次执行，纠正类另存 `output_key` 与 `before`/`after` 成对值。两类原料用 `kind` 区分：`correction`（产出审核里"修正后确认"的**每个被改键各一条**，`source=output_review`）与 `supplied_fact`（`ask` 节点的答案，`source=ask`，含 `node_id` 与 `after`）。采集发生在**决定/提交的同一事务内**——纠正被拒（未声明键、类型不符等）时不会留下任何记录。读取入口 `GET /executions/{id}/feedback` 与运行本身同一判定（发起者或受控 admin，读不到即 404）；分析器（A-14）走内部仓储按工作流读取，不经过 HTTP。
- **会话续期（A-18）**：登录与续期现在同时签发**刷新令牌**（响应新增 `refresh_token` 与 `refresh_expires_in`，默认 30 天；访问令牌仍是 24 小时，并由既有的滑动续期在活跃期自动延长）。`POST /api/auth/refresh` 用它换一对新令牌：**每次使用都轮换**、库里只存 sha256、一次登录等于一个"家族"；再次提交已轮换的令牌只可能是重放 → **整个家族被撤销**并要求重新登录（两个请求抢跑同样按重放处理）。`POST /api/auth/logout` 可带上刷新令牌以**真正结束该会话**——此前登出只写审计事件，无状态 JWT 无法撤销。于是"安静超过访问令牌有效期再回来"的客户端不必重登，而登出第一次有了实际效果。表由核心迁移 `036_session_refresh_tokens` 建立（PostgreSQL 与 SQLite 两侧都真建表）。
- WorkBuddy **运行中向人提问**（A-08）：新增 `ask` 节点——运行到某一步缺少只有人能给的事实时（发票号、两个账户选哪个），执行**停在原地等回答**，而不是取消重跑（后者会丢掉已完成的工作与图上的位置）。节点在权威 schema 里声明 `prompt`、`fields`（名字/标签/类型/是否必填/占位/选项）与 `assignee_user_ids`；编译器校验字段名在同一节点内**唯一**（字段名就是提交结果的键）、`select` 必须给 `options`、受派人须是租户内**当前可见**成员——与审批同一条规则：只有"没人能答"才拦发布，已离职的受派人在运行时表现为该步失败（`ASK_NO_VALID_ASSIGNEE`），不会让本来健全的工作流发布不了。执行状态新增 `waiting_input`，与 `waiting_approval` 同类：停放时不占运行槽，回答后重新排队、由 Worker 再次接纳。
- 提问落库为 `workbuddy_input_requests`（问题、表单与表单摘要、锁定版本 id/hash、截止时间、答案与答案摘要、作答人/时间）与 `workbuddy_input_assignees`（谁该答、是否已回答），两张表 ENABLE + FORCE RLS 并配租户内复合外键；`workbuddy_step_runs.status` 同步接纳 `waiting_input`。**问题文案在开单时渲染**（`{{ … }}` 按本次运行的实际输入与已结算输出渲染），受派人拿到的是一句关于这次运行的话，而不是模板原文。
- 新增三个端点：`GET /executions/{id}/input-requests`（某次执行提出的问题，含表单与受派人）、`POST /executions/{id}/input-requests/{rid}/answer`（提交答案；未声明的键、类型不符、选项外、必填缺失一律 400 `WORKBUDDY_VALIDATION_FAILED` 且**不改动任何状态**；CAS 保证一个问题只被回答一次，重复提交冲突）、`GET /input-requests`（本人待填队列；admin 的 `scope=tenant` 需有管理目的并审计）。**非受派人一律 404**，与审批一致，不泄漏问题是否存在。运行详情新增等待原因 `input`（`wait_reasons`/`waiting_steps`），前端据此给出答题入口；`input.requested` 通知与 `workbuddy.input_request.created` 发件箱事件与审批同构。
- WorkBuddy **运行产出审核**（A-09，**事后只追加、绝不阻断**）：`POST /executions/{id}/output-review` 让人审阅某次运行的产出（仅对**已结算**的执行，一次执行至多一条审核）——执行保持原状、产出照旧，审核是**新的只追加记录**（复制产出并记 `produced_sha256`，附受指派审核人）。决定三选一（`POST …/output-review/decisions`）：`accept` 确认产出原样；`correct` 记录**修正后的值**（只允许替换运行**实际产出**的键——修正不是发明结果的地方）；`rerun` 启动一次**新执行**并把 id 回链到审核上，于是"用修正值重跑"成为**两次运行之间**的事实，而不是改写其中一次。重跑还要求被审运行的锁定版本仍是当前版本，否则拒绝（改版后拿同一份修正去跑另一个版本会答非所问）。审核对**非受指派者一律 404**（与审批、提问一致）；`GET /output-reviews` 给出本人待审队列（admin 的 `scope=tenant` 需管理目的并审计）；请求与决定各发通知与发件箱事件，并写审计（`output_review.request` / `output_review.decide`，含被修正的键列表）。
- WorkBuddy **超时与升级（SLA）**（A-10）：`ask` 节点的 `timeout_hours` 此前只是落库的字段，逾期没有任何后果——运行会永远等下去。现在逾期未答的问题会被**原子置为 `expired`**（平台上下文清理，`SKIP LOCKED` + UPDATE 即 CAS，两个清理者不会重复处理同一条），随即**升级**：通知受派人与租户管理员（`input.expired`）、写 `workbuddy.input_request.expired` 发件箱事件、写审计（`action=input.expire`，含通知人数），并把执行重新排队，使下一次引擎尝试以 `INPUT_REQUEST_EXPIRED` **失败该节点**而不是再次停放同一问题。Worker 在每次领取前先结算截止时间，因此即使租户没有别的工作，逾期问题也会被处理。
- WorkBuddy **待我填写**界面（A-08/A-11 前端）：收件箱新增「待我填写」面板（问题、来自哪次执行、截止时间、空态与失败重试），回答抽屉按 `form.fields` 逐类型渲染输入（文本/多行/整数/数字/布尔/日期/下拉），必填缺失在提交前拦下、后端错误原样展示；运行详情在等 `input` 时给出同一抽屉的入口。收件箱三合一中的「待我审核」随 A-09 落地，本轮不放假数据。
- WorkBuddy **创建向导**（A-06）：面向非技术员工的四步引导——① 输入（声明工作流输入）② 步骤（选类型，字段**由 `GET /workflow-definitions/metadata` 驱动**渲染该类型的必填/可选/枚举/范围，向导里没有任何硬编码的字段清单）③ 数据（为每步设置结果名并列出可引用写法）④ 输出。能否放行"下一步"由**服务端校验**决定：定义变化后防抖调用 `POST /workflow-definitions/validate`，回来的诊断按 `path`/`node_id` 归到所在页面，当前页有错就不放行——而不是等到保存时才被编译器拒绝。保存按 建草稿 → 存版本 → 发布 走既有路由，发布后可直接**试运行**；原自由 JSON 编辑器保留为进阶路径，两条路并存。
- WorkBuddy 节点集**显式化**（A-07）：新增三类节点——`input`（把某个已声明的输入接入图）、`knowledge`（检索成为独立一步）、`output`（显式声明运行结果）。三者同时进入权威 schema（`contracts/workflow-v1.schema.json` 的节点枚举与 `allOf` 分支）与编译器校验（输入节点必须指向**已声明**输入；输出节点**唯一且必须为终态**；知识库节点按调用者可达性校验），并**自动出现在 `GET /workflow-definitions/metadata`**（元数据从 schema 同源导出，不手写副本）。
- 运行时执行三类新节点：`input`/`output` **本地执行**（读声明输入 / 渲染运行结果，不离开进程）；`knowledge` 走**可注入的检索端口**（与工具、模型适配器同类）；未接线时该步 **fail-closed**（明确报依赖不可用），而不是静默返回"没有检索结果"而看起来像空答案。
- 旧定义**行为完全不变**：入口规则只在定义里确实存在 `input` 节点时放宽（额外的输入根不算"多入口"），可达性从**所有源点**计算（单根定义与原行为逐字等价）；编译器 41 项既有用例与运行时 59 项既有用例全部保持通过。
- WorkBuddy 工作流接入权限四层（B-02）：工作流创建时在同一事务内注册权限对象（默认 `enterprise`），列表按「公司 / 部门 / 个人 / 单独授权」四层过滤且与纯解析器逐 actor 一致；详情、保存、发布、回滚、归档统一走同一判定，读不到或管不到的对象一律 404（跨租户同码，不泄漏存在性）。此前无权限行的历史工作流保持公司可见（与列表同一回退），管理权仍归创建者与租户管理员。
- WorkBuddy 工具与模型授权支持部门/成员主体（B-03）：两个授权表加入 `subject_key`（`tenant` / `department:<id>` / `member:<user_id>`）并进入主键，配可空的 `user_id`/`department_id`、形状 CHECK 与租户内复合外键，既有行默认 `tenant` 语义不变；`GET /tenant-capabilities` 返回 `tool_grants`/`model_grants`，新增 `POST|DELETE /tenant-capabilities/{kind}/{revision_id}/grants`（租户 admin）。替换租户级批准集只影响 `tenant` 行，部门/成员授权不被覆盖；撤销租户级模型授权会清空租户默认模型（默认模型必须保持租户级批准，否则会留下没人能用的默认值）。已跳过 016 的既有库由 `_ensure_workbuddy_grant_subjects` 幂等补齐同样的列、主键与约束。
- WorkBuddy 编译器按**调用者**解析工具与模型可达性（B-04，与 A 线的唯一交叉点）：`PostgresWorkflowSemanticResolver(conn, tenant_id, *, user_id=None)` 的回答改为「租户级授权行 ∪ 调用者所属部门及其父链上的部门授权 ∪ 调用者本人授权」的并集，默认模型同样须对该调用者可达（否则 `MODEL_NOT_CONFIGURED`），缺 `user_id` 时 fail-closed；知识库嵌入模型的授权校验同步改为按调用者解析。A 线三处构造点未改实现（构造时已传 `user_id=principal.user_id`）。
- WorkBuddy 通用权限骨架（迁移 050 + `octop.infra.rbac`）：把知识库已有的四层权限模型（公司/部门/个人/单独授权）抽成与对象类型无关的一套表与解析——`workbuddy_object_scopes` 记隐式范围（`personal` 仅所有者、`department` 仅当前成员、`enterprise` 全员，形状与知识库同款 CHECK），`workbuddy_object_acl` 记可增可撤的显式授权（`read`/`write`/`admin`，主体为一人或一部门）。解析取「隐式等级与命中授权等级的最大值」，授权只能加不能夺；不可见对象对外与不存在一致（404 而非 403）。两张表强制 RLS（ENABLE + FORCE）与租户内复合外键，SQLite 侧只推进水位并在事务入口 fail closed。列表查询与单对象判定共用同一套可见性谓词，并有测试比对二者，防止两边漂移。
- WorkBuddy 知识库文本-only 迁移通道（迁移 051）：文档新增 `source` 判别列（`upload`/`text`/`migration`），`file_ref_id` 放宽为可空并配 `source` 形状约束与部分唯一索引——这样「个人版只留分块文本与向量、不保留原文件」的库也能导入，同一知识库可有多篇无原文件的文档，而「一个已存文件只对应一篇文档」的约束仍然成立。服务新增 `index_text_document`（对传入文本分块、嵌入、原子发布，全程不触碰对象存储），路由 `POST /knowledge-bases/{id}/documents` 接受 `source` 与 `text`；上传类文档走原路径不变，走错入口会被明确拒绝而不是静默降级。
- 知识库迁移对账脚本 `scripts/migrate_kb.py`：读取个人版控制库（`~/.octop/octop.db`）与各库的 `index.sqlite`（分块文本 + `<Nf` little-endian float32 向量），逐库给出导入路径与理由——A 原样搬（模型修订已发布且已授权、维度一致）、B 重新嵌入（模型可用但维度不符）、C 挂起（模型未发布或未授权，需管理员先授权），未给目标库时如实报 `unknown` 而不猜测。报告含每库文档数、分块数、向量维度、目标范围（`shared=1 → enterprise`）与总计；只读、可重复运行，本批次不写入任何一侧。
- WorkBuddy 编译器**一次报出多条诊断**（A-05）：`WorkflowCompileError` 新增 `diagnostics`（每条含 code/message/path/node_id/hint_key），五个检查阶段从"首错即停"改为**收集器**（阶段之间仍按依赖顺序推进，前阶段有错不进入后阶段），诊断按 `(path, code)` 稳定排序、上限 20 条并在 `details.diagnostics_truncated` 标出总数；HTTP 错误响应不再丢弃 path/details（`details.compiler_code` + `details.diagnostics`）；定义编辑器逐条列出问题并给出 `workflowDiagnostics.<CODE>` 的中英修复提示（覆盖 24 个编译器错误码）。
- 节点元数据契约 `GET /workflow-definitions/metadata`（A-04）：五类节点的必填/可选与 config 字段（类型、枚举、范围、默认值）、输入类型、模板可写字段、引用语法、CEL 可引用命名空间——**全部从 `workflow-v1.schema.json` 与编译器常量同源导出**（不手写副本，并有"元数据 ↔ Schema 必填集合一致"的断言）。前端新增 `getDefinitionMetadata()`，不再硬编码节点类型。
- 运行详情支持**逐节点排障**（A-02）：执行详情返回 `inputs`/`outputs`/`active_duration_ms`/`token_usage`（整数总量）；每个步骤返回 `input`（该节点派发时实际使用的输入）、`output`（此前入库却从未返回）、`duration_ms`/时间窗/`attempt`/`skip_reason`，以及该节点的 `token_usage`（对象；非 LLM 节点为 null）。迁移 032 复用 018 早已声明却一直没有写入者的 `step_input` payload 类型与 `input_sha256`，把输入放进既有 append-only payload 账本而不是复制到热点步骤表。**只增加记录与返回，不改执行/重试/对账语义。**
- 运行列表新增「**重新运行**」（复用既有执行接口，不新增路由）：行内缺少工作流/版本/输入时按钮禁用并给出原因，不猜测参数。
- 版本**定义级比较** `GET /workflows/{id}/versions/diff?from=&to=`（A-03）：复用从提案抽出的**唯一** keyed `semantic_diff`（按 id 键控列表，节点重排不算变更，输出 path/kind/old/new）；版本页勾选两个版本即可查看按 JSON pointer 分组的差异（新增/删除/替换着色，旧值/新值可展开且闭合时不挂载）。
- WorkBuddy 控制台改为**员工优先的信息架构**：侧边栏分成「日常工作」（工作台 / 收件箱 / 工作流 / 运行）与「治理与设置」（知识库 / 提案 / 审批 / 市场 / 合规 / 生命周期）两组，日常四类入口排在最前。
- WorkBuddy **工作台**（`/workbuddy`）：一屏给出待我处理的审批、我的工作流与最近运行，每个区块独立降级——某个接口未合并只影响那一块，其余照常显示真实数据。
- WorkBuddy **收件箱**（`/workbuddy/inbox`）：待我审批与待我审阅的提案集中在一处，审批页签复用既有可操作的审批面板。
- WorkBuddy **运行**（`/workbuddy/runs`）：跨工作流的执行记录，支持按范围/状态过滤与从 `?workflow=` 预过滤，点开任意一条复用既有执行详情抽屉。
- 工作流详情页新增**改进提案**页签，并让选中的工作流进入 URL（`?workflow=`）：工作台与运行页可以深链到具体工作流，提案详情复用同一份 `ProposalDetailPanel`，控制台只有一份「读/审/提升提案」的实现。
- 市场**本租户安装台账**列表接口 `GET /marketplace/installations`（租户来自登录主体，不来自请求），并把安装页签接上该列表。
- 提案**分配独立审核人**接口 `POST /improvement-proposals/{id}/reviewers`：校验审核人为同租户 active 成员且不是提案创建者，越权与自审一律拒绝。
- 生命周期**再认证 → 导出下载挑战**链路：`POST /auth/reauthenticate` 签发五分钟一次性再认证凭据（绑定 tenant/user/purpose、`no-store`），`POST /exports/{id}/download-challenge` 凭该凭据签发兑换挑战（`no-store`，不延长导出 72 小时窗口）。
- WorkBuddy 知识索引记录为真实作业（合同 §4.6.2）：`POST /knowledge-bases/{id}/documents` 返回的 `job_id` 指向 `workbuddy_jobs` 中一条 `knowledge_index` 作业（此前是一个查不到对应行的随机 uuid）；索引开始时作业置为 `running`，结束时写入 `result`（文档、世代、分块数），失败时写入失败码。
- WorkBuddy 提案生成记录为真实作业（合同 §4.6.2）：`POST /workflows/{id}/improvement-proposals` 的 202 返回 `job_id` 指向 `workbuddy_jobs` 中一条 `improvement_proposal` 作业，成功后 `result` 带 `proposal_id`/`workflow_id`，失败时记录拒绝码；客户端丢失响应后可用 `GET /jobs` 找回。
- WorkBuddy 节点重试：按定义声明的 `retry.max_attempts`/`backoff_sec` 执行重试，只对「可证明未发出调用」的失败（依赖不可达、请求被拒）与已声明只读/幂等的工具生效；每次重试复用同一逻辑操作键 `execution_id:node_id`；未知结果仍进入对账而不是重发。
- WorkBuddy 平台工具注册表补齐合同 §4.6.1 的声明字段：`input_schema`、`output_schema`、`effect_class`（`read_only`/`external_write`）、`supports_idempotency`、`supports_result_lookup`、`sandbox_verified`，默认值一律取保守值；引擎按租户已授权修订解析声明，用于重试判定与工具结果按注册 schema 校验。
- WorkBuddy 发件箱派发器（合同「Scheduler / Outbox dispatcher」）：已提交的 outbox 事件此前只写不读，永远停在 `pending`；现在由派发器按 `available_at` 领取（`FOR UPDATE SKIP LOCKED` + 可见性窗口，跨租户平台上下文）、经可注入的发布端口送出，成功标记 `dispatched`，失败按指数退避重排，超过尝试上限后进入死信（`failed` 并保留最后错误）。发送通道是端口（合同只规定语义：PG 为事实源、至少一次投递、消费者按 PG 去重）；未配置发布端口时派发器拒绝运行，不会把没人收到的事件标记为已送达。
- WorkBuddy 执行 Worker：接纳执行只写入 `queued`，由 Worker 从数据库原子领取（锁租户行、占运行槽、取租约并单调递增 fencing token）。整体等待（审批/对账）释放运行槽，恢复时重新排队申请；Worker 崩溃后租约到期由下一个 Worker 接管。
- `octop workbuddy worker` 命令行与 `deploy/compose.production.yml` 的 `worker` 服务；单进程安装默认在 `octop run` 内托管该 Worker（`OCTOP_WORKBUDDY_WORKER=off` 可关闭）。

### 修复

- 修复**无源点的环导致编译崩溃**：拓扑检查先取入口节点、之后才找环，于是「每个节点都有入边」这种没有源点的纯环定义会以 `IndexError` 中断编译，`POST /workflow-definitions/validate` 返回 503 而不是调用方要用来修定义的环诊断。现在没有源点时直接交由环检查报出节点列表（422 + `WORKFLOW_CYCLE`），并补上这条此前缺失的用例——它是由 CI 的 live-PostgreSQL 用例发现的：该用例带 PG 标记，本地只跑了定向用例。
- 修复阶段遗漏的**步骤节点类型约束**：迁移 018 建表时把 `workbuddy_step_runs.node_type` 限定为当时存在的五类（`tool`/`llm`/`condition`/`approval`/`transform`），此后新增的 `input`/`knowledge`/`output`（A-07）与本次的 `ask` 都不在其中——**任何含这些节点的工作流一旦真正运行，第一步落库就会以 check 违例整次失败**；而 A-07 的验证只到单元层（不写数据库），所以没有暴露。迁移 034 把该词表扩到与编译器节点集一致的九类。这个缺陷是批次 4 的端到端用例（真实 PostgreSQL 上跑停放 → 作答 → 恢复）第一次执行就抓到的——它也是"单元测试全绿不等于功能可用"的实例。
- 修复**停在表单上的运行取消不掉**：`request_cancel` 的状态白名单只有 `queued`/`waiting_approval`，漏了新增的 `waiting_input`，于是员工在「等待填写」的执行上点取消不会有任何效果（接口既不改状态也不报错）。补入后取消会立即结算该执行——停在任何人工等待上的运行都必须能直接取消，因为它已经没有在途调用需要等。该修复由端到端用例（停放 → 取消 → 状态为 `canceled`）证明。
- 修复 `workbuddy_executions` 写入方法 `insert_execution` 的**绑参缺列**：列清单已含迁移 024/025 加入的路由列（`proposal_id`/`cohort`/`bucket`/`route_canary_percent`/`subject`），绑定值却只有 12 个，调用即报占位符数量不符。该方法当前无调用方（运行时走 `insert_execution_if_absent`），属潜伏缺陷而非线上故障。现两者列集与绑参完全一致，并以真实 PG 探针逐字段回读证明同一入参写出同样形状的行（含 5 个路由列非空回读）。
- 企业治理面板把租户角色判定写死为 `admin`，而后端 `TENANT_ADMIN_ROLES` 与租户创建流程都以 **owner** 作为首位治理者：结果是每个租户的第一位用户在企业治理页只看到只读的「企业成员」视图，成员、邀请、配额、凭据、能力许可五个管理页签全部不可见，尽管接口本会放行。现改为共享的 `utils/tenantRole.ts`，并新增一条读取后端 `roles.py` 的一致性测试，防止两侧角色集合再次漂移。
- 生产 Worker 容器启动即失败：`deploy/scripts/app-entrypoint.sh` 执行的是 `octop workbuddy-worker`，而命令行只提供 `octop workbuddy worker`（组 + 子命令），容器会以「No such command」退出。同步修正 `docs/architecture.md`、`.env.example` 与 CHANGELOG 中的同一处写法，并新增 `tests/unit/test_deploy_cli_commands.py`：解析 `deploy/scripts/*.sh` 中所有 `octop …` 调用并与真实命令行注册表比对，防止部署脚本与命令面再次漂移。
- WorkBuddy 作业状态：此前没有任何路径把作业从 `queued` 置为 `running`，正在执行的作业对客户端仍显示 `queued`；新增 `start_job`（同时写入 `started_at`）。
- WorkBuddy 作业结果写入：`finish_job` 未按 jsonb 绑定 `result`，任何以对象作为结果的作业都会在写入时失败。
- WorkBuddy 租户并发配额按实际运行槽计数：审批等待释放槽位，恢复时原子重取，重叠执行不再越过上限。
- 延迟取消（对账中取消）不再遗留每月执行预留与运行槽。
- 运行详情抽屉的「记录外部写证据（对账）」对话框此前发的请求体与后端契约不符（后端 `extra="forbid"` 且 admin-only，要求 `step_id`/`decision`/`evidence_ref`/`reason`/`external_reference`），**每次提交必然 422**，对账功能整体不可用。现按后端真实形状重建请求与响应类型（`step_id` 语义即被挂起步骤的 node id），表单要求填写理由、决策限定 `confirmed_success`/`confirmed_failed`，并在缺理由时不提交。
- 运行与步骤的状态词表此前与后端不一致（前端写 `running/succeeded/failed/skipped/waiting_approval`，后端按迁移 023 是 `queued/running/waiting_approval/waiting_reconciliation/success/failed/skipped/canceled`）：`succeeded` 这类取值永远匹配不上，界面会显示异常状态；现改为以后端词表为准，并把不存在的 `RECONCILIATION_STATUSES` 换成 `RECONCILIATION_DECISIONS`（`confirmed_success`/`confirmed_failed`）。
- 提案详情页的「分配审核人」按钮此前调用一个后端从未实现的路由（恒 404）：现按冻结合同补上 `POST /improvement-proposals/{id}/reviewers`，并在前端去掉「该路由尚未实现」的提示。
- 生命周期页的「重新认证 / 签发下载挑战」两步此前对着两个不存在的 stage-D 路由（恒 404）：现补齐 `POST /auth/reauthenticate` 与 `POST /exports/{id}/download-challenge`，凭据一次性且五分钟有效，签发挑战不延长导出 72 小时窗口。
- 市场「安装台账」页签此前无列表可调（合同只发布按 id 查询），页面只能提示无法展示：现发布 `GET /marketplace/installations`（租户主体从登录上下文解析、分页），并把台账接上列表。
- 新增提案/生命周期两块路由的合同对齐测试：与冻结合同做双向比对（路由必须两边都在、成功状态码一致、租户路由必须依赖租户主体）。这三个缺陷的共同成因正是「合同声明了路由、代码里没有、也没有任何测试比对」。

### 文档

- **README 改为描述本仓库自身**（`README.md` 与 `README_CN.md`）：此前的两份 README 是上游 Octop 的产品介绍（上游 banner、Trendshift 徽章、"自托管 AI 助手"定位、上游的安装形态与功能巡礼），读者会把另一个产品当成这个仓库——这正是"误导其他用户"的来源。现在开头先声明本仓库与上游的关系（以 Octop（MIT）为**基座**、产品方向是**多租户企业工作流平台**、上游 README 不代表本仓库），随后给出「这个仓库是什么 / 不是什么」、本仓库实际交付的能力（WorkBuddy 为主，继承基座单独标注并注明不属于本仓库方向）、快速开始、命令行、接口速查（并指向 `contracts/route-manifest.json` 作为权威清单）、验证（`make lint/typecheck/test` + PG/Redis 标记用例 + 前端命令，并说明 CI 中唯一覆盖迁移与 RLS 的是 live database 作业）、部署要点（迁移时机、RLS、运行角色属部署方、Worker 必需）、当前边界、仓库结构与许可归属。文中引用的路径、端点与命令逐条对照代码核实（20 个端点、11 个路径、4 个 Makefile 目标）。

## [1.0.1] - 2026-09-18

### 新增

- 飞书 / 钉钉 / 企业微信 OAuth SSO 登录与绑定
- 可选登录验证码（滑块及多家云验证码），并提供 `octop captcha reset`
- 容器工作区默认、专家头像，以及聊天 HITL / 技能选择体验优化
- 模型列表搜索；ACP 纳入个性化「工具」页签

### 修复

- 超长 URL 导致历史消息加载极慢
- 手动创建渠道默认启用；PostgreSQL 知识库缺列；损坏 config 被清空
- 若干 Dashboard / 构建相关问题（权限页签、抽屉滚动、Windows 构建等）

### 变更

- 升级 harness-agent / harness-browser

## [1.0.0] - 2026-09-14

### 新增

- GA 版本正式发布
- 更新检查默认仅查稳定版，可选包含预发布

### 变更

- 登录页与浏览器标题 slogan 更新为「懂你、帮你、陪你成长的智能伙伴」

### 修复

- 备份列表大归档时不再全量扫描 tar
- 腾讯云语音探测失败在中文界面本地化

## [0.9.35] - 2026-09-13

### 新增

- 聊天支持 @ 提及工作区文件
- STT 探测改为真实识别调用，与 TTS 对称

### 修复

- 语音模型探测不再因凭证/网络错误返回 500，直接展示云厂商真实错误原因
- 浏览器录音上传前转码为 WAV，修复腾讯/Mimo 语音识别不支持 webm 容器导致的识别失败
- STT 探测改为真实识别调用，与 TTS 探测对称，凭证/网络错误以 ok:false 返回
- 语音探测按界面语言返回中文/英文错误（含腾讯云 SecretId 等常见鉴权失败）
- 语音探测失败直接展示错误，不再返回 500；录音上传前转码为 WAV
- `/compact` 回复隐藏本机绝对路径
- `octop run` 正确应用 `OCTOP_PORT` / `OCTOP_BIND_HOST`

## [0.9.34] - 2026-09-12

### 新增

- 聊天头像改到输入框两侧，失败轮次保留内容和错误
- 定时任务空状态改用专家任务示例

### 修复

- 刷新后消息时间戳不再丢失，用户气泡与头像对齐
- FnOS 安装向导接管管理员密码，避免默认弱密码导致无法启动

### 变更

- README 补齐知识库、插件、PostgreSQL 等说明

## [0.9.33] - 2026-09-11

### 新增

- 知识库支持下载原文、按原排版预览，聊天内可直接预览引用
- 技能可在技能包与专家工作区之间复制
- 记忆树支持手动新建与修正；Token 统计支持日期筛选与 Excel 导出
- 按用户限制存储根目录与 Token 配额；创建专家可带默认知识库与连接器
- 聊天支持 @ 子专家、技能斜杠插入，以及 ask_agent 独立对话线程
- 滴滴连接器；可选分段历史归档；便携运行时升级并支持 SQLite 备份

### 修复

- 工作区 zip 导出不再被 backend 根目录同名文件顶替；导入时保留隐藏系统状态
- 知识库文本文档编辑保存生效；共享专家技能可在聊天中使用
- 斜杠命令刷新后仍可见；新会话标题即时更新
- 升级检查失败提示本地化；OpenCode Go 请求携带会话 ID

### 变更

- 聊天点技能改为插入 `/slug`；创建定时任务必须填写名称
- 依赖 harness-gateway ≥ 0.9.6、orcakit-harness-agent ≥ 1.0.8
### 新增

- QQ 私聊默认走官方 `stream_messages` 替换流式：进站先发不可见换行 hold，再按完整 markdown 块更新同一条气泡；`<think>` 会剥掉且不 `.trim()` 掉换行
- 流式失败、前缀被拒（`40007`）或只发出 hold 时，回落为一条静态 markdown（`msg_type=2`，失败再 `0`）

### 修复

- 飞书话题内回复改走话题回复接口并带 `reply_in_thread`，失败时回退为群内普通发送；话题以 `thread_id` 作为会话主体

### 变更

- QQ 私聊不再使用通用「回复模式」开关，旧键 `streaming` / `response_mode` 无效；只有显式 `c2c_streaming: false` 才退出流式
- QQ 群聊 / 频道 / 频道私信没有 stream API，仍只发静态消息
- 控制台保存 QQ 通道时写入 `c2c_streaming: true`，并提示工具过程会另占每条入站约 4 条被动回复配额
- 依赖 `harness-gateway>=0.9.7`（含 QQ C2C 替换流式与飞书话题修复）

## [0.9.32] - 2026-09-06

### 新增

- 聊天运行轨迹抽屉与回合时间轴；人机确认以可读审批卡片展示
- 知识库支持更多文档格式与可选 OCR，上传显示进度
- 定时任务支持名称，计划回合持久化
- 备份可选择归档内容；连接器支持多实例与共享；插件可按智能体开关
- OpenSandbox 远程沙箱；工作台浏览器按用户隔离 profile
- 飞书扫码创建应用；远程手机可自动安装 Docker
- 桌面端 Windows NSIS 安装包与 macOS DMG；未知路由 404 页

### 修复

- 斜杠命令写入 harness 会话 checkpoint，刷新后仍可见，且下一轮模型能读到
- 桌面客户端打包把 `pyproject.toml` 版本写入 macOS Info.plist、Windows 文件版本和 NSIS 安装器，不再沿用写死的旧号
- 用户发布专家卡片展示快照内自定义头像（`icon_url` + `/api/experts/published/{id}/avatar`），不再只显示默认 Lucide 图标
- 用户管理表格（桌面端）横向滚动时固定用户名与操作列（与专家列表一致；移动端不固定）
- 运行轨迹流式事件在内存聚合、回合边界持久化，避免逐 token 写库和重复存储上下文全文
- 运行轨迹 Turns / Calls 折叠与耗时投影：历史缺 `turn_id` / 时长时回退为 USER 边界与内容体量估算，避免开关无效
- 知识库表格模式宽屏仍出现多余水平滚动条（列宽拖拽手柄越出最后一列）
- 聊天右侧停靠/弹窗工具栏避开无边框窗口按钮，避免与红绿灯重叠
- 桌面启动页改为白底卡片和底部进度条；Windows 设置窗加高，避免底边距被裁掉
- 桌面壳内隐藏「安装为桌面应用」，避免在已原生窗口里再提示 PWA 安装
- 日志按大小+按日轮转，并采用 logrotate 风格的 `compress` + `delaycompress`（最新一份轮转文件暂不 gzip，下一轮再压；用 Python 标准库，Windows 可用）
- 远程手机自动安装 Docker 后，非 root 时用 `sudo -n` 写 `daemon.json` 并重启 dockerd
- 旧备份在 schema 变更后可恢复；桌面打包版本与应用元数据对齐
- 主动关怀时区、专家卡片头像、用户表固定列
- 运行轨迹写库开销、日志轮转、通道二维码轮询与远程 Docker 安装权限

### 变更

- 语音与搜索设置迁到模型页；控制台改用 OctopSpinner
- 默认管理员凭据改为首次运行写入 `~/octop-login.txt`

## [0.9.31] - 2026-09-01

### 新增

- 聊天流式输出时默认展开思考/工具过程，回答完成后收起（历史记录仍收起）
- 聊天侧栏与 @ 选择仅展示运行中的专家
- 默认专家与控制台创建的专家一样使用家目录存储；聊天待办按计划顺序展示
- 工作台浏览器可显式结束本地 Chrome 进程；空闲超时后也会回收（登录态仍保留在磁盘 profile）
- 桌面端右上角窗口控制（最小化 / 最大化 / 关闭进托盘）

### 修复

- 过期 hashed 静态资源返回 404，避免升级后桌面壳加载旧脚本
- 桌面无边框窗口改为 CSS/JS 拖拽，去掉会挡住右上角按钮的 `InvisibleTitleBarHeight`
- macOS 点击程序坞只恢复主窗口，不再同时弹出托盘设置窗
- 专家详情技能/子智能体卡片：图标在标题左侧，状态或 id 在标题右侧，描述单独一行
- 桌面端等待本机服务就绪失败时改为中英文说明（跟随桌面语言设置），不再显示 `/api/health` 英文报错
- 桌面启动页补齐右上角窗口按钮，并收紧启动/失败状态的展示
- 桌面托盘设置窗去掉多余空白，右上角只保留关闭；macOS 单击菜单栏图标也会弹出设置

## [0.9.30] - 2026-08-31

### 新增

- 腾讯云 Token Plan 企业版与 Hy 套餐
- WeKnora、Dify 连接器，以及自定义 MCP 的 OAuth
- 备份/恢复、聊天工具栏、SSO 预设与更友好的供应商错误提示
- 技能展示本地化；知识库可配置文档数量上限；钉钉扫码注册
- 对话接入 ask-user-question 人机确认流程

### 修复

- 专家根目录、连接器排序、飞牛图标及主题确认对话框等界面问题
- 聊天中文语音识别跟随界面语言
- MCP OAuth 刷新失败需重新授权；渠道异常 thinking 输出过滤
- 数据库 v10 迁移遗漏 thread projection 表
- 飞牛 FPK 无效在线升级与原生版启动加载；长会话相关问题
- 通道弹框文案统一为「通道」，新建默认实时过程
- `octop acp` 启动即崩溃（CLI 注册表属性应对齐 `acp_cmd`）

## [0.9.29] - 2026-08-27

### 修复

- 长会话卡死：聊天历史改为独立投影分页加载，并支持后台迁移旧会话（不再同步扫 checkpoint）

### 变更

- 依赖：`orcakit-harness-agent[all]>=0.9.27`、`harness-memory>=0.9.7`、`harness-browser>=0.7.6`（自动 full VACUUM 关闭，空闲维护只走 lifecycle GC + incremental `nudge_vacuum`）

## [0.9.28] - 2026-08-26

### 修复

- 无更新权限时隐藏检查更新入口
- `/compact` 兼容 `.octop/conversation_history/` 卸载路径
- FnOS 镜像改为 Docker Hub `jubaoliang/octop`

### 新增

- 基层医生学习助手增加普通医学问答快路径、国内专业学会/专科分会与国际指南精确路由，并完善受控信源降级和检索预算。

## [0.9.27] - 2026-08-26

### 新增

- 内置插件随包装分发（默认关闭，卸载后升级不重建）
- 可配置上传上限（`max_upload_mb` / `OCTOP_MAX_UPLOAD_MB`，默认 100MB）
- Dashboard 推送通知（定时任务与主动关怀 toast）
- 聊天音视频附件预览播放，并扩展 inbound 附件 MIME
- 火山方舟 Seedream / Seedance 生成模型配置、测试与结果展示
- 连接器：Ardot、滴答清单；远程 MCP OAuth 改为 catalog 驱动
- 单工具开关热更新与插件工具目录
- ACP 内置 Runner：Kimi Code、Cursor CLI、Pi
- 知识库文件夹重命名
- ONNX 模型下载竞速 Hugging Face 与 hf-mirror
- 远程手机 ADB shell（旋转与分屏布局）
- FnOS NAS 应用打包（Docker / native `.fpk`）
- 专家模板扩充（通用、Karpathy、临床来源策略）
- 中文子智能体约 49 个（HR / 法务 / 供应链）
- Dashboard 剪贴板回退与聊天 UI 打磨

### 修复

- 知识库文件夹操作按钮误开文件夹
- 工具预期失败不再误报为 `stream_error`
- 连接器 OAuth 公网回调 / HTTPS / 自动保存；npm 不可写时回退用户级 prefix
- SSO ID token issuer 校验
- Dashboard：SW 激活后再 reload、Firefox 无限刷新、选择器 popover、文案全球化
- 浏览器 runtime 目录在 Windows 上可写探测

### 变更

- FnOS 打包拆分为 `docker/` 与 `native/`

## [0.9.26] - 2026-08-23

### 新增

- 远程手机（实验性）：安装时探测主机移动能力（`capabilities.mobile`，物理机 / Redroid / KVM）；能力开启后开放 `GET /api/settings/capabilities` 与 `/api/mobile/*`。控制台「远程手机」支持 adb H.264/JPEG 推流、触控、画质预设、设备信息与 AI 助手面板；智能体移动工具绑定当前远程手机会话
- 控制台布局支持经典 / 极简模式，聊天记录统一承载；用户可选填邮箱（邀请 / 登录）；知识库支持应用内编辑 markdown / txt；远程桌面与远程手机合并为统一控制入口

### 修复

- 邀请链接统一为 `/invite?code=`，修复邀请页居中与移动端在 overflow-hidden 壳下的滚动；启动前显示 logo 加载动画；设置页邮箱输入图标对齐；移除聊天坞中的远程手机入口
- 加固 dashboard 鉴权与请求层（setup 锁定 503、401 刷新）、登录页与 AuthGuard 体验；按服务器能力门控移动端功能；远程控制中枢页签文案缩短为「服务器 / 手机」

## [0.9.25] - 2026-08-21

### 新增

- Token 计量新增缓存命中支持：按模型调用累计未缓存输入、缓存读取、缓存写入、推理 Token 与模型调用次数；用量页和消息气泡展示缓存命中数据。
- 编辑专家抽屉可修改标题语（欢迎语），与创建时同一字段，写入智能体实例而非仅页面配置
- 专家支持上传自定义头像，写入工作区 `.octop/avatar.png`（或 jpg/webp/gif）并通过 `agents.icon_url` 展示；发布快照会带上头像，安装后自动绑定。未设置时仍用配色 + Lucide 图标
- 实例化专家的欢迎语改为单一 `welcome_message` 字段（用户自填，不再分中英）；专家模板仍保留双语欢迎词
- 智能体状态接口返回 `memory_maintenance`（queued / pruning / compacting）。聊天页显示阶段进度条，整理本库时暂停发送；专家卡片显示「整理记忆」标签。
- 聊天页：文件工具卡片显示「编辑了 N 个文件」（不含截图）；文件面板标题改为「文件变更」。会话 `artifacts` 由 Octop 工具中间件在写文件 / 发文件 / 桌面截图成功后写入，切换会话后仍可在文件变更中查看。路径优先取工具 args，仅在 args 没有 path 时才扫结果文本。专家选择器「共享」标记与名称同一行。右侧增加工作区入口；工作区目录树支持将文件拖到其他文件夹。
- 知识库、技能包、已发布专家改为整数自增 `id` + 对外字符串 ID（`knowledge_base_id` / `skill_package_id` / `published_expert_id`；文档用 `kb_id` 关联）。`agents` 仍用字符串引用技能包与已发布专家。知识库文档支持文件夹路径；分片大小等仍存实例 `settings`。删除未使用的 `knowledge_base_members`。上述库变更与专家资料列、会话 artifacts、实例欢迎语单字段一并作为 schema v7。
- 对话检索结果附带知识库引用标记；聊天页在回答下方展示可点击的来源文档（跳转知识库页）

### 修复

- 技能：修复编辑已导入技能并保存后，技能内其余文件与文件夹（README.md、references/ 等）被整体清除的问题——内容编辑（仅 SKILL.md）现原地覆盖清单文件、保留全部同级文件；携带完整 `files` 载荷的更新仍整目录替换（与覆盖重装语义一致）
- Harness usage 事件按稳定调用 ID 去重，避免流重放重复计费；上下文环保留路由模型的真实窗口上限，并将构成拆分明确显示为近似估算。
- Admin 环境变量未进入正在运行的 Agent：本地 shell 默认不继承进程 env，Docker exec 也不传 env，工作区 `.env` 只落盘不注入。现本地 shell 每次 execute 继承当前进程环境（含 `~/.octop/env`）并 overlay 工作区 `.env`；Docker 热读全局文件 + 工作区 `.env` + 最小 PATH（不含完整宿主机环境）。保存 Admin 列表会从进程环境删除已去掉的键；仅搜索类 key 变化时后台 reload Agent。MCP stdio 只注入 SDK 安全子集 + 全局/连接器 env，不再灌入整份 `os.environ`。
- Token 用量账本每轮只记下最后一次模型调用，工具循环中前面若干次调用被丢弃，页面合计会远低于聊天里看到的用量；现按该轮全部 AI 调用的 `usage_metadata` 累加，以 `state_snapshot` 为权威终值（snapshot 之后的增量不再相加），畸形 usage 字段跳过以免打断对话
- 插件工具使用中文等非 ASCII 名称时 LLM 调用失败：主流 API 要求工具名匹配 `^[a-zA-Z0-9_-]{1,64}$`，现自动将非法名称转写为合法拼音名（`pypinyin` 缺失时退回下划线替换），冲突追加 `_2`/`_3` 后缀，并在工具描述前缀 `[原名: …]` 保留原名映射；`config_json.plugins` 配置键与插件内部仍使用原始名称，路由不受影响
- 修复聊天页在"生成中"时于输入框持续打字导致消息列表上下轻微抖动的问题：输入框高度测量改为在离屏克隆节点上进行，不再瞬态改变页面布局
- 工作区读写在 harness 后台重建窗口（DB 仍为 running）回退到 `workspace_for_agent`，避免误报 `AGENT_NOT_RUNNING`
- 聊天页上下文占用环：旧会话没有分段快照时，从消息上已有的 `usage_metadata` / `response_metadata.token_usage` 回填已用量；相对 1M 级窗口不再把真实占用四舍五入成 0%
- 专家抽屉保存页面配置时合并写入 `manifest.json`：保留其它字段与另一语言欢迎语；加载未完成或未改动时不写文件
- 修复个性化「通道」面板在页面放大后不出现纵向滚动条、被挤出的通道卡片无法查看的问题：工具栏固定、卡片网格改为内部滚动区（与技能面板一致），移动端仍整页滚动

## [0.9.24] - 2026-08-15

### 新增
- 知识库：新增知识库与对话检索，支持本地 ONNX 向量嵌入模型运行
- 认证：新增 OpenID Connect（OIDC）单点登录
- 权限：新增按用户模块权限（RBAC）及管理员绕过
- 专家：支持将工作区快照发布为可安装模板（专家市场）
- 智能体：支持将智能体共享给其他用户
- 技能：新增对话式技能管理器（SkillHub），并兼容 Windows
- 备份：新增自动定时系统备份
- 频道：新增 final-only 仅终稿回复模式
- 线程：支持从 AI 回复处分叉会话（fork）
- 体验：HITL 工具选择器、运行时按需安装、KB/技能 UX 优化；对话与镜像等界面打磨；知识嵌入初始化流程加固

### 修复
- 媒体/预览白名单补充音频 MIME 类型
- 修正 ONNX 下载检测在未安装 fastembed 时的误判
- 加固更新状态缓存与存储处理
- 预提交门控：修复 staged 变更检测，避免 testmon 门控误报为绿

### 变更
- 升级 harness-browser 依赖至 0.7.5
- 数据库 schema 收敛为 v5
- 备份恢复面板图标更新为 CalendarClock；对话/镜像等界面打磨

## [0.9.23] - 2026-08-13

### 修复
- 取消首次引导时删除 `octop-login.txt` 引导密码文件的逻辑，避免引导密码意外丢失
- 修复安装脚本版本显示问题，并将安装输出调整为英文

### 新增

- Octop-owned built-in `skill-manager` for conversational Skill lifecycle
  management from uploaded files, archives, Git/GitHub or web URLs, and
  SkillHub. It is seeded into every agent instance without modifying
  harness-agent and installs user Skills only under that agent's `skills/`.

## [0.9.22] - 2026-08-11

### 新增
- 专家工作区支持 `.docx` 在线编辑：以 Markdown 在 Monaco 中打开/保存，保存时转回 docx 覆盖原文件（标题/加粗/斜体/列表/表格保留，复杂格式简化）；工作区新建的 `.docx` 即初始化为合法文档包，预览/编辑立即可用。基于可扩展注册表，新增可编辑后缀只需注册一个后端转换器类 + 前端注册表一行

### 变更
- 依赖新增 `python-docx==1.2.0`（含 `lxml`），用于工作区 `.docx` 的 Markdown 往返转换

### 修复
- 仪表盘发版后或长时间未打开时白屏：Service Worker 不再 Cache-First 钉死旧 `index.html`；hashed 资源改为 CacheFirst；入口脚本失败时清除 SW 缓存并自动刷新一次 (#236)

### 安全
- 仪表盘 SPA 静态回退路由加固：在拼接路径前显式拒绝绝对路径与 `..` 父目录引用，并保留最终 `relative_to` 校验，杜绝路径穿越读取 dashboard 目录之外的文件（修复 CodeQL 标记的 Uncontrolled data used in path expression）

## [0.9.21] - 2026-08-11

### 新增
- 插件管理页支持从本地 ZIP 上传安装插件，可选覆盖已安装的同名插件，无需先把插件托管到 HTTP 直链
- Docker 沙箱 backend（agent `config.backend.type=docker` 或存储 `kind=docker`）；Admin Docker 卡片、本机 Docker 探测/安装；详见 [docs/agent-backend-file-io.md](docs/agent-backend-file-io.md) §13
- 强制密码策略并优化账户与子代理（subagent）使用体验
- 浏览器 HITL 流式交互与网关抢占能力
- 新增每用户模型与推理（reasoning）偏好设置

### 变更
- 依赖 `orcakit-harness-agent[all]>=0.9.20`；FilesystemGuard / ModelSettings 由 harness 自动挂载（Octop 仅保留 BinaryReadGuard 与 runtime_limits）
- 专家 `workspace_dir`：创建时写入 `config_json.workspace_dir`（默认 `{OCTOP_HOME}/agents/<id>/`），所有 backend 共用；Docker 在容器内镜像同名路径为专家工作区，宿主同路径放 sessions/memory/checkpoints
- Docker：`sandbox_scope`（agent/user/fixed）+ `sandbox_prefix`（默认 `octop_sandbox`）；删专家不删容器；专家工作区在 running 时可预览；Admin 存储 `previewable` 仅控制浏览（默认仅 fixed）；探测用 test 沙箱做真实读写
- 删除被专家 `named` 引用的存储后端时返回 `STORAGE_BACKEND_REFERENCED` 并列出引用专家
- 将 IM 频道定时任务从 ACE 迁移至 Octop cron

### 修复
- 超大图片不再降级为附件路径提示：超过视觉嵌入上限（2 MB）的图片由 Pillow 压缩缩放至最长边 1568px 后仍以内联图片嵌入请求（保留 EXIF 方向与透明通道，仅当压缩失败时才回退为路径提示），视觉模型自动升级随之生效 (#219)
- 保留技能 ZIP 导入时的空目录与根级技能的子文件夹
- 修复移动端个人设置抽屉，并恢复玫瑰色主题配色

## [0.9.20] - 2026-08-09

### 新增
- QQ 频道二维码扫码绑定，支持群聊上下文（仪表盘频道抽屉 + `octop channel` CLI）(#160)
- 语音接入小米 MiMo STT / TTS 供应商（`mimo-v2.5-asr` / `mimo-v2.5-tts`），设置页可选择计费端点与 9 种预置音色，TTS 标注限免 (#186)
- 仪表盘自定义品牌配色：8 套调色板（玫瑰 / 科技 / 靛蓝 / 青绿 / 紫罗兰 / 翠绿 / 琥珀 / 石墨），与浅色 / 深色模式正交且本地持久化

### 修复
- Windows 下新建 agent 时，本地后端 `root_dir:"/"` 被解析为当前盘根目录，导致读取工作区（通常位于另一盘符）时抛 `Path ... outside root directory`；现在后端规格解析会在 Windows 上将主机根 `/` 的 `root_dir` 改写为工作区路径（保留原 `type` 等字段）
- 删除专家时同步清理 `~/.octop/agents/<id>/` 工作区目录（rmtree 移出事件循环执行）；清理失败不阻断数据库删除；仪表盘与 CLI 删除确认提示工作区将永久删除且不可恢复
- 乐享连接器 MCP URL 补充 `preset=meta` 参数并简化快捷授权链接 (#213)
- 专家卡片的编辑 / 删除按钮默认可见，不再仅在悬停时显示 (#187, #193)
- 登录页滑动验证通过后，提示文案居中显示在滑块左侧的可见区域 (#185)

### 变更
- 企业微信客户群二维码与文档有效期更新至 2026-08-16

## [0.9.19] - 2026-08-05

### 新增
- 登录页滑动验证控件；侧栏与 Agent 资料抽屉 UI 优化 (#170)
- 聊天历史 API 返回 `turn_active`，重连客户端可 re-subscribe WebSocket 恢复流式输出 (#168, #157)
- Workbench 与聊天 Dock 共用同一 terminal 会话；旧式硬切会话标题迁移为带省略号的裁剪标题 (#157)
- 局部 `root_dir` 下 Linux bubblewrap execute jail（`POST /api/filesystem/ensure-bwrap`、仪表盘 root 目录树 mkdir/rename）(#167)
- 虚拟工作区路径 I/O：host 绝对路径经 `file://` 与 `BackendWorkspace` failback 对齐 (#167)
- 高级设置「更新」页提供按安装方式升级说明与一键检查升级双栏布局；HTTPS 页优化签发状态与预检展示 (#143)

### 修复
- 401 会话过期时通过 React Router 跳转登录，避免整页 reload 导致 lazy chunk 白屏 (#169)

### 变更
- `make all` 先执行前后端 `format-all`（Ruff + Prettier）；pre-commit 在 format 后回写已暂存文件并构建 dashboard (#143)
- harness runtime 诊断日志写入 `~/.octop/logs`（与 `octop.log` 并排），不再落到各 agent workspace 的 `logs/`；行内带 `[agent=…]`
- 依赖 `orcakit-harness-agent>=0.9.19`、`harness-gateway>=0.9.1`（scoped root execute jail）
- 企业微信客户群二维码与文档有效期更新至 2026-08-08 (#149)

## [0.9.18] - 2026-08-02

### 新增
- 聊天 Dock 支持可关闭的文件列表 / 预览 / 浏览器标签页，以及 PR 风格路径树与路径去重；账户气泡与侧栏交互打磨 (#130)
- 内置示例插件（greeting / toolkit / turn-logger）与中英文插件说明文档
- 搜索设置页显性展示当前搜索源：未配置第三方服务时提示内置搜索，配置后展示实际服务 (#109)

### 修复
- 已停止或禁用的专家统一返回 `AGENT_NOT_RUNNING`（不再误报未找到）；管理员 Token Usage 支持按用户筛选；聊天会话频道图标与创建用户角色选择优化 (#137)
- 强化插件安装错误诊断与自定义 MCP 校验；网关流式错误支持本地化
- 聊天流式错误在界面可见；Token Usage / Memory 图表与空状态展示优化；弹层 Dock 几何与全屏行为修正

### 变更
- 设置、连接器、插件管理与管理用户等页面统一到共用仪表盘布局语言
- Docker / 安装文档中的国内加速镜像示例改为腾讯云镜像 (#116)
- 依赖抬升：`orcakit-harness-agent` ≥0.9.18、`harness-memory` ≥0.9.5；对齐 Python 3.12 目标与依赖刷新 (#118)

## [0.9.17] - 2026-07-31

### 新增
- 全局技能包：实例级可复用技能集合，支持挂载到专家、从 SkillHub 导入技能集，以及本地 ZIP / URL 导入技能
- 个性化页整合技能 / 子专家 / 频道 / MBTI / 记忆；技能包管理页支持移动端列表详情切换
- 搜索设置页显性展示当前搜索源：未配置第三方服务时提示使用内置搜索（免 API Key，不保证稳定），配置后展示实际使用的服务 (#109)

### 变更
- 技能相关域逻辑迁至 `infra/skills/`；数据库迁移合并为 schema v2（cron MCP + skill_packages 含图标）(#108)
- 备份/恢复纳入 `skill-packages/` 目录，恢复前清空避免残留 (#108)
- 统一聊天生成中 / 滚动辅助逻辑；antd message 经 App.useApp 绑定，支持主题感知 toast (#119)

### 修复
- Memory 原始事件列表的时间戳按服务器时区展示，与其余 Memory 页保持一致 (#110)

### 修复
- 记忆提取 / 提升等 harness 内部辅助 LLM 默认跟随全局偏好模型（此前切换全局模型后仍回退到首个可用模型）(#110)

## [0.9.16] - 2026-07-29

### 新增
- 统一自定义 / 预设 / 配置提供商弹窗的模型编辑流程，支持拉取 OpenAI 兼容远程模型列表，并仅在显式保存时落库 (#91)
- 支持从 LightClaw 迁移导入（备份快照与系统归档兼容，含外键约束处理）(#58)

### 修复
- 修复自定义提供商弹窗 TypeScript 错误（未使用导入 / 可选 `input`），恢复 release 构建
- 从 GitHub URL 导入技能时保留完整技能目录（含引用文件与脚本），并加固归档下载的分支名、文件数与体积限制 (#92)
- 浏览器配置不再对系统路径执行 chmod，改为使用共享目录 `~/.octop/browser-profiles` (#87)
- 部署后静态资源哈希不匹配导致白屏时，自动软刷新一次并防止重载死循环 (#88)
- 修正网易邮箱 IMAP 主机解析，并在登录前发送 IMAP ID；同时加固 QQ / 网易 / Gmail 邮件主机预设与探测 (#89)

### 变更
- 最低依赖 `orcakit-harness-agent` 提升至 ≥0.9.16
- README 补充中长期 Roadmap / 规划说明
- 新增可选 `.githooks` 提交前检查（`make install-hooks`）

## [0.9.15] - 2026-07-27

### 修复
- 加固聊天导航：切换专家时避免残留旧会话 URL，并稳定流式 Markdown 渲染
- 优化 Memory / Token Usage 页面布局，消除嵌套滚动并改善信息密度
- 补充企业微信客户群二维码相关文档说明

## [0.9.14] - 2026-07-25

### 新增
- 控制平面支持 PostgreSQL 双后端（统一 DatabasePool、并行 PG 迁移、安装向导选择/绑定、pg_dump 备份；PostgreSQL 下记忆默认复用控制平面 DSN）(#60)
- SkillHub 改为走 HTTP API，支持来源中立的技能包安装与搜索 (#55)
- 远程浏览器/桌面支持真实拖拽（转发 CDP 指针事件），并共享推流连接中指示 (#50)
- 聊天界面布局与交互打磨：历史侧栏、消息队列、自动滚动与欢迎页等体验优化 (#66, #69, #70)

### 修复
- 修复 macOS/Linux 上 Agent 上下文历史写入主机根目录的问题：依赖 harness-agent≥0.9.12 将 deepagents artifacts 落到 Agent 工作区 (#57)
- Provider catalog 的 `context_window` 映射为 harness `max_input_tokens`，修复 Auto/摘要阈值与 UI 上下文环按错误上限计算的问题
- 元宝扫码绑定后保存官方 API 与 WebSocket 地址，并升级网关至 0.8.7 以支持完整媒体收发 (#56)
- ChatGPT/Codex OAuth 改为 device code 流程，修复非 localhost 部署下授权失败 (#54)
- 技能 CLI 安装不再根据用户输入的 slug 推导路径，避免装错包 (#63)
- 删除会话时同步清理 harness checkpoint，避免「删除」后消息历史仍残留 (#60)
- 修正 PostgreSQL 记忆可移植导出的误导性 pg_dump 提示（共享 schema 下按 namespace 隔离，不可整库导出单 agent）(#60)
- 技能启用/禁用与 SkillHub 安装不再触发整机 Agent rebuild，避免切到技能列表时短暂「未找到 Agent」
- 修复聊天向上滚动加载更早消息失效，并在列表未溢出时提供可点击回退
- 工作区路径语义澄清（`from_workspace`），并加固 Windows 下 file URL / 主机路径校验

### 变更
- `/compact` 改为在当前话题强制触发一次 Summarization（总结较早消息并 offload 到 `conversation_history/`），不再新建线程；新建空话题请用 `/new`
- `/compact` 成功提示明确：聊天界面仍保留完整历史，压缩的是下一轮模型可见上下文
- 文档与发布流程改为 develop 日常集成、先合入 main 再打 tag (#48)

## [0.9.13] - 2026-07-23

### 新增
- SkillHub 改为走 HTTP API，支持来源中立的技能包安装与搜索 (#55)
- 远程浏览器/桌面支持真实拖拽（转发 CDP 指针事件），并共享推流连接中指示 (#50)

### 修复
- 修复 macOS/Linux 上 Agent 上下文历史写入主机根目录的问题：依赖 harness-agent≥0.9.12 将 deepagents artifacts 落到 Agent 工作区 (#49, #57)
- Provider catalog 的 `context_window` 映射为 harness `max_input_tokens`，修复 Auto/摘要阈值与 UI 上下文环按错误上限（如 128k）计算的问题
- 修复取消聊天任务后再次提问会一直停留在思考状态的问题 (#42, #43)
- 技能启用/禁用与 SkillHub 安装不再触发整机 Agent rebuild，避免切到技能列表时短暂「未找到 Agent」
- 内置专家卡片标题与图标水平对齐
- SkillHub / 专家市场在 Python SSL 失败时给出可操作提示，并修正技能市场错误态「Retry」未本地化为「刷新」(#44, #46)
- 元宝扫码绑定后保存官方 API 与 WebSocket 地址，并升级网关至 0.8.7 以支持完整媒体收发 (#56)
- ChatGPT/Codex OAuth 改为 device code 流程，修复非 localhost 部署下授权失败 (#54)
- 远程桌面安装拒绝不支持的 EL10 环境 (#41)
- 聊天上下文占用图例在空会话时对齐 (#40)
- 修复聊天向上滚动加载更早消息失效，并在列表未溢出时提供可点击回退
- 工作区路径语义澄清（`from_workspace`），并加固 Windows 下 file URL / 主机路径校验

### 变更
- `/compact` 改为在当前话题强制触发一次 Summarization（总结较早消息并 offload 到 `conversation_history/`），不再新建线程；新建空话题请用 `/new`
- `/compact` 成功提示明确：聊天界面仍保留完整历史，压缩的是下一轮模型可见上下文
- 文档与发布流程改为 develop 日常集成、先合入 main 再打 tag (#48)

## [0.9.12] - 2026-07-21

### 新增
- 备份恢复后可在进程内同步 providers 并重载 agent；提供商变更后仅重载受影响的 agent
- 新增服务端时区 API（`default_timezone` / `GET /api/settings/timezone`），控制台时间展示对齐服务端时区
- 记忆提炼支持为每个 agent 单独指定提取模型，并在整理记录中展示每次 extract_run 结果

### 修复
- 修复记忆提取模型无法 fallback 导致提炼失效的问题
- 修复语音输入 STT 回退处理
- 修复内部 MCP gateway 在事件循环上阻塞的问题
- 修复高级搜索探测接口缺失、表格分页卡在 10 条、新建会话图标提示，并加固安装脚本
- 改进 Notion OAuth HTTPS 错误提示

### 变更
- Memory 页签「全部」更名为「记忆沉淀」

## [0.9.11] - 2026-07-19

### 新增
- 新增 SkillHub 专家市场：支持浏览、安装与管理专家，并完善安装安全校验与欢迎页快捷卡片体验
- 新增自定义 MCP 连接器管理，支持探测、工具缓存与连接器配置

## [0.9.10] - 2026-07-18

### 新增
- 新增工作区文件预览与浏览器工作区支持，并完善相关工具链
- 新增聊天面板停靠式文件预览、HTML 预览与历史下拉刷新

### 修复
- 修复连接器 Notion OAuth 弹窗阻塞的问题 (#19)

### 变更
- 重构聊天界面，将浏览器面板与文件面板统一为 ChatDock
- 调整工作区路径透传逻辑，不再重写 BackendWorkspace 路径
- 将上下文使用统计委托给 harness-agent 0.9.10

### 移除
- 移除内置的临床医生专家 (#20)

## [0.9.9] - 2026-07-16

### 新增
- 新增远程桌面安装与连接器探测能力增强 (#16)

## [0.9.8] - 2026-07-15

### 新增
- 远程浏览器/远程桌面安装日志面板新增「复制日志」按钮，并在安装失败时提示可将日志交给 Octop 协助排查
- 新增前端 `copyText` 工具，在非安全上下文（如 plain-http 管理页）下通过临时 textarea + execCommand 回退，保证剪贴板复制可用
- 桌面安装脚本新增 `A-F4`（关闭窗口）与 `C-A-D`（显示桌面）openbox 快捷键，对应桌面快捷键

### 修复
- 修复桌面安装脚本的 Python 构建依赖检测：改用 venv Python（而非系统 `python3`）解析 `pythonX.Y-dev`，避免 evdev 编译时找不到 `Python.h`；`setup.py` 安装构建依赖时显式传入 `--python` 指向当前 venv Python
- 修复连接器类型漂移导致聊天弹窗 logo 解析失败的问题

### 变更
- Docker 构建与 `make build-frontend` 的 `NODE_OPTIONS --max-old-space-size` 由 4096 调低为 2048，降低构建内存占用
- 新增 `docker-publish.yml` 工作流，构建并推送镜像到 Docker Hub
- 移除 `release.yml` 中多余的 `id-token: write` 权限
- 删除已与现行 Docker Hub 发版流程脱节的离线部署脚本 `docker_deploy.sh`，并清理 `docker/README.md`、`README_CN.md` 中的相关章节
- 修正 `docker/README.md` 标题笔误（`ODocker` → `Octop`）

## [0.9.7] - 2026-07-14

### 新增
- 新增多款连接器网关适配器：百度地图、携程问道、飞猪、美团旅游助手、QQ 音乐、元典 (#14)
- 重构连接器网关目录与注册机制，支持更灵活的连接器安装 (#14)

### 修复
- 修复 Linux 远程桌面安装脚本在 EL7（TigerVNC 1.8）下的兼容性，避免 xfdesktop 阻塞安装

## [0.9.6] - 2026-07-13

### 新增
- 新增远程桌面（Remote Desktop）功能，支持跨 Linux、Windows、macOS 的桌面串流 (#7)

### 修复
- 从 .dockerignore 中移除 uv.lock，修正 Docker 构建无法 COPY 锁文件的问题 (#9)
- 修复远程桌面、浏览器、终端及安装向导的本地化（i18n）问题 (#11)

## [0.9.5] - 2026-07-12

### 新增
- 新增 Linux、Windows、macOS 三端的远程桌面串流能力
- 完善远程桌面的安装/卸载交互，并打包 Linux 端安装脚本

### 修复
- 修复 Windows 与 Linux CI 下桌面配置/捕获/输入相关单测与 mypy 报错
- 修复 Mac 端远程桌面安装时误导性的提示文案
- 加固桌面安装 SSE 流式推送并清理 dashboard 端 lint 问题

## [0.9.4] - 2026-07-11

### 新增
- 新增 agent backend 的主机 root_dir 浏览器与权限探测能力
- 改进聊天流式滚动行为与思考计时器

### 修复
- 修复 Windows 下 sqlite 路径测试、媒体路径与 POSIX 专属测试导致的 CI 失败
- 修复 Windows 测试收集问题（惰性导入 pwd 模块）
- 修复 harness-memory Bridge 导入路径
- 修复 CI 流水线并让测试套件通过，项目重命名为 Octop

### 变更
- Windows 兼容：默认 agent backend 限定到 workspace，并集中 POSIX 专属 stdlib 调用以适配 Windows mypy CI

## [0.9.1] - 2026-07-08

### 新增
- 远程浏览器控制页面与浏览器 AI 面板，支持远程浏览器自动化操作
- 附件下载的 `Content-Disposition` 头（RFC 5987，兼容非 ASCII 文件名）
- 前端 UI 语言偏好持久化（自动检测浏览器语言并记忆）
- 专家目录欢迎语（默认欢迎内容 / 工作区清单读取 / 专家目录播种）
- 附件相关国际化域（`i18n/domains/attachment.py`）
- 聊天欢迎语支持

### 变更
- 重构聊天附件与上传处理链路，精简接口与实现
- 重构网关媒体层：附件提示、入站存储、工具媒体展示重写
- 重构 harness 请求构造与消息处理器
- 调整上下文拆分、专家目录、provider 存储与 agent 管理器
- 重构前端聊天界面：输入框、消息气泡、工具媒体条、上下文窗口环等组件大量更新
- 更新登录、初始化向导、终端 AI 面板等前端页面

### 修复
- 修复附件路径解析与内容分发相关问题

### 移除
- 移除模型配置提示弹窗、旧聊天流模块、slash 上下文与附件签名测试
