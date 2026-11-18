# Aegis · 多 Agent 协同与可信交付

> 一条流水线怎么跑，写在 YAML 里；模型写出的代码能不能算"通过"，由物理断言裁决，而不是由模型自己说。

Aegis 是一个 **编排即数据（orchestration-as-data）** 的多 Agent 执行平台：

- **编排**：`workflows/*.yaml` 描述节点、分支、重试、熔断、并行与汇合，引擎只做解释执行，源码里没有一行业务分支硬编码。
- **角色**：`agents/*.yaml` 声明每个 Agent 的模型、温度、工具集、记忆策略与版本，换角色不改 Python。
- **验收**：模型产出的代码被**真实执行**，通过与否取决于断言（`CONTAINS` / `NOT_CONTAINS` / `REGEX` / `FILE_EXISTS`），不是模型的自述。
- **可控**：修复有预算，预算用尽即熔断转人工；人工建议下发后预算重置再跑，全过程落库可审计。

---

## 1. 它解决什么问题

赛题把"硬编码流水线"列为 Agent 应用的头号痛点：节点写死在代码里、加一步就要改 Python、失败只能整体重跑、过程无法复盘。

Aegis 的四条对应做法：

| 赛题痛点 | Aegis 的做法 |
|---|---|
| 编排写死在代码里 | `workflows/*.yaml` 声明式定义；引擎与业务节点彻底解耦 |
| 失败处理只会整体重试 | 节点级 `on_failure` + `max_retries` + `on_exhausted` 熔断 |
| 过程黑盒、无法复盘 | 任务黑板（SQLite）+ 逐节点日志 + trace 级工作记忆，全部可导出 |
| "通过"靠模型自述 | `execute_spec` 真实执行 + 物理断言，模型无权宣布成功 |

---

## 2. 架构

```
            ┌─────────────────────────────┐
            │  Streamlit 控制台 (app.py)   │  只为展示与下发，不含调度逻辑
            └──────────────┬──────────────┘
                           │ start_workflow() / resume_with_advice()
            ┌──────────────▼──────────────┐
            │  编排引擎 (core/engine.py)   │  claim → execute → plan → persist
            └───────┬─────────────┬───────┘
                    │             │
     ┌──────────────▼───┐   ┌─────▼──────────────────┐
     │ 工作流定义        │   │ 任务黑板 (SQLite)       │
     │ core/workflow.py  │   │ core/db.py             │
     │ workflows/*.yaml  │   │ tasks / logs / trace_vars│
     └──────────────┬───┘   └─────┬──────────────────┘
                    │             │
     ┌──────────────▼─────────────▼──────────────┐
     │ Agent 注册表 (core/agents.py)              │
     │ agents/*.yaml · 超时 · 退避重试 · 工具调用  │
     └──────────────┬────────────────────────────┘
                    │
     ┌──────────────▼─────────────────────────────┐
     │ 工具层 (core/tools.py)                      │
     │ ① tool 节点：execute_spec（落盘/装依赖/验收）│
     │ ② Agent 可调用工具：list_files / read_file / │
     │    run_python（Function Calling）            │
     └────────────────────────────────────────────┘
```

节点类型只有四种：`llm` / `tool` / `join` / `human`。控制流全部由字段驱动：
`routes` · `next` · `on_success` · `on_failure` · `max_retries` · `on_exhausted` · `on_resume` · `parallel` · `join`。

---

## 3. 目录结构

```
Aegis/
├── app.py                  # Streamlit 控制台（展示与下发）
├── ui/theme.py             # 前端视觉层（原生 HTML/CSS）
├── ui/canvas.py            # 编排画布：WorkflowSpec -> 分层 SVG（离线、零依赖）
├── core/
│   ├── engine.py           # 编排引擎：认领 → 执行 → 转移 → 落库
│   ├── workflow.py         # YAML 解析、校验、路由匹配、拓扑导出（mermaid/DOT）
│   ├── agents.py           # Agent 注册表 + 大模型调用（超时/重试/工具循环）
│   ├── tools.py            # 工具层：规格解析、工作区、依赖安装、断言验收、Agent 工具
│   ├── db.py               # 任务黑板（原子认领、僵尸回收、trace 变量）
│   └── config.py           # 全部可调参数
├── agents/                 # router / coder / fixer / analyst / chat
├── workflows/              # 6 条工作流（软件交付 / 数据产线 / 三个企业日常场景 / 并行演示）
├── examples/               # 入参夹具：真实输入才谈得上"复现"（日志、脏数据、销售明细）
├── scripts/verify/         # 分层验证脚本（控制结构矩阵 / 调度器 / 工具边界 / 前端交互）
├── tests/                  # 158 项离线测试（不联网）
├── docs/编排引擎改造说明.md  # 改造前后对照、字段参考、等价性证据
└── requirements.txt
```

---

## 3.5 六条工作流（三条行业线 + 三个企业日常场景）

同一个引擎，差异全部写在 YAML 里。每条都能用固定入参复现，产物可核算。

| 工作流 | 业务场景 | 入参（`examples/`） | 交付物 | 失败策略 |
|---|---|---|---|---|
| `code_gen` | 软件交付：一句话需求 → 带验收的代码 | 无（自生成） | `main.py` + 验收报告 | 自动修复 ≤2 次 → 熔断转人工 |
| `data_pipeline` | 数据产线：拆解 → 两条支线并行 → 合并 | 无（自生成） | 两份明细 + `summary.csv` | 验收失败直接终止 |
| `log_triage` | **运维值班**：nginx 日志 → 值班周报 | `logs/nginx_access_20261118.log`（120 行） | `report.md` + 结论 | 自动修复 1 次 → 转人工 |
| `data_quality_gate` | **数仓上游**：脏订单表清洗 + 质量门禁 | `data/orders_dirty_202611.csv`（45 行脏数据） | `clean_orders.csv` + `quality_report.csv` | **门禁语义：直接终止，不自动改写** |
| `excel_report` | **周会报表**：明细 → Excel（明细 + 汇总 + 合计） | `data/sales_202610.csv`（24 行） | `report_202610.xlsx` | 自动修复 1 次 → 转人工 |
| `parallel_demo` | 并行编排演示（不落盘） | 无 | 汇总文本 | — |

实测（真实模型、真实沙箱）：

| 工作流 | trace | 断言 | 成本 | 交付物 |
|---|---|---|---|---|
| `log_triage` | `d8d78c4f` | 13 条全过 | 6,480 tokens | `report.md` 1,241 B（120 请求 / 5xx 20 / 占比 16.67%，与人工核对一致） |
| `data_quality_gate` | `c670d464` | 12 条全过 | 19,311 tokens | `quality_report.csv` 707 B + `clean_orders.csv` 1.7 KB |
| `excel_report` | `f9e8ab41` | 8 条全过 | 3,825 tokens | `report_202610.xlsx` 6,131 B（明细 24 行 + 汇总合计 32312.00） |

**入参文件怎么声明**（`inputs:` 是工作流级字段，启动时复制进该 trace 的工作区）：

```yaml
inputs:
  - logs/nginx_access_20261118.log
  - data/orders_dirty_202611.csv
```

只允许 `examples/` 下的相对路径，绝对路径与 `..` 在加载时就被拒绝——入参是"可复现的输入"，不是随手能读的任意文件。

---

## 4. 快速开始

```bash
# 1) 依赖（建议 Python 3.10+；开发环境为 3.14）
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 2) 配置模型密钥
copy .env.example .env            # 然后填入 DEEPSEEK_API_KEY
#   可选：DEEPSEEK_BASE_URL 覆盖为任意 OpenAI 兼容端点

# 3) 启动控制台
streamlit run app.py
```

打开后：左侧选工作流 → 输入需求 → 点「启动任务」。页面每 3 秒自动刷新，能看到拓扑、节点明细、实时日志、人工介入卡与交付物。

侧边栏顶部「视图」切到 **编排画布**，即可图形化查看与修改工作流定义（详见第 8 章）。

**一条命令验证（不需要网络）**：

```bash
python -m unittest discover -s tests -t .
# Ran 47 tests ... OK
```

---

## 5. 工作流字段速查

```yaml
name: code_gen
version: 1
entry: router                # 入口节点
nodes:
  router:
    type: llm                # llm | tool | join | human
    agent: router            # 对应 agents/router.yaml
    context: "{prompt}"
    routes:                  # 按模型输出匹配分支
      "[CODE]": coder
      "[CHAT]": chat_reply
  verify:
    type: tool
    tool: execute_spec       # 对应 core/tools.py 的 TOOL_REGISTRY
    context: "{spec}"
    on_success: __done__
    on_failure: fixer
  fixer:
    type: llm
    agent: fixer
    context: "{prompt}\n{last_error}\n{files}"
    max_retries: 2           # 修复预算
    on_failure: verify       # 回边：再验一次
    on_exhausted: human      # 预算用尽 → 熔断
  human:
    type: human
    on_resume: fixer         # 人工建议下发后回到哪
```

占位符来自 trace 级工作记忆（`{prompt}` `{spec}` `{last_error}` `{files}` `{advice}` `{retry}`），由节点的 `publish` 写入。

---

## 6. Agent 与工具

Agent 定义（`agents/fixer.yaml` 节选）：

```yaml
name: fixer
version: 1
model: deepseek-chat
temperature: 0.0             # 修复要可复现
memory: trace                # 读取 trace 工作记忆
tools: [list_files, read_file, run_python]
can: [失败归因, 全量重写修复, 断言补齐]
```

两类工具，边界很清楚：

| 类别 | 谁决定 | 例子 | 作用 |
|---|---|---|---|
| **tool 节点工具** | YAML 写死，引擎无条件执行 | `execute_spec` | 落盘、装依赖、真实执行、断言验收 |
| **Agent 可调用工具** | 模型在推理中自己决定 | `list_files` `read_file` `run_python` | 只读探查 + 试跑，帮模型看清现状再动手 |

Agent 工具走标准 Function Calling：模型返回 `tool_calls` → 引擎在**本 trace 工作区**内执行 → 结果以 `role=tool` 回灌 → 模型继续推理，直到给出最终文本。轮数上限默认 4 轮（`AEGIS_TOOL_ROUNDS`），单轮最多 4 个调用（`AEGIS_TOOL_CALLS`），路径逃逸一律拒绝。

**模型不能自己宣布成功**：Agent 工具只做探查，真正决定成败的是 `execute_spec` 里的断言。

### 断言词汇表（= 平台的验收边界）

`core/tools.py: ASSERTION_KINDS`。能写出什么样的断言，就决定了这个平台能接什么样的场景。

| 断言 | 含义 | 典型用途 |
|---|---|---|
| `CONTAINS: 文本` | 输出包含 | 汇总值、关键提示 |
| `NOT_CONTAINS: 文本` | 输出不含 | `NOT_CONTAINS: Traceback` 防裸崩 |
| `REGEX: 正则` | 输出**按行**匹配 | 格式、行数、耗时 |
| `FILE_EXISTS: 文件名` | 产物存在且非空 | 交付物是否真的生成了 |
| `REGEX_FILE: 文件名 :: 正则` | **直接校验产物内容** | CSV 表头 / 字段 / 格式 |
| `FILE_MIN_SIZE: 文件名, 字节` | 产物大小下限 | 防"只有表头"的空 CSV |
| `NEAR: 期望值, 容差` | 输出中任一数值落在容差内 | 科研 / 统计类数值任务 |
| `EXIT_CODE: 0` | 校验退出码 | 用非 0 退出表达业务失败时 |

两条纪律：

- **拼错的断言类型判失败**（`CONTANIS:` 不会被悄悄降级成 `CONTAINS` 放过去）；
- 只有显式写了 `EXIT_CODE`，非 0 退出才交给断言裁决——否则一律直接判失败。

### 两条行业线，策略写在 YAML 里

同一个引擎，两条工作流的失败策略相反，引擎一行都不用改：

| | `code_gen`（软件交付） | `data_pipeline`（数据/报表） |
|---|---|---|
| 结构 | 分诊 → 生成 → 验收 → 修复回边 | 拆解 → **两条支线并行** → 汇合 → 合并 |
| 失败策略 | 自动修复最多 2 次 → 熔断转人工 | **直接终止，不自动改写**（数据口径错了自动改写风险高于收益） |
| 关键控制结构 | `routes` / `on_failure` 回边 / `max_retries` / `on_exhausted` / `on_resume` | `parallel` / `join` / 多段产物传递 |
| 适用判据 | 产出可被断言裁决 | 同上，且判据来自"列名 + 汇总值 + 产物存在" |

产物隔离是**节点级**的：同一节点重试会清掉自己上一轮的产物（`FILE_EXISTS` 不会被残留文件骗过），不同节点的产物跨节点保留（数据管线要靠它把上游清洗结果交给下游合并）。

**实测一次 `data_pipeline`（trace `4b24a710`）**：8 个节点全部 done、0 失败、9 步执行、6 次 LLM 调用、4 次工具调用、**10,823 tokens**；交付物 4 个文件（`east_sales.csv` 92 B、`south_sales.csv` 92 B、`summary.csv` 54 B、`main.py`）。产物可核算：`线上 1200+1200=2400`、`门店 850+850=1700`、`分销 430+640=1070`，三条支线的数据血缘对得上。

---

## 7. 调度器（能力匹配 + 动态优先级）

编排层决定"下一步是谁"，调度层决定"什么时候、交给谁"。派工要过三道闸：

| 闸门 | 做法 |
|---|---|
| **能力匹配** | 节点语义派生成能力标签：`llm:<agent>` / `tool:<tool>` / `join` / `human`。Worker 只认领自己会跑的任务（`capability` 为 `NULL` 表示不限人手） |
| **动态优先级** | 基础优先级来自节点语义：`human 100`、`fixer 60`、`verify 50`、`coder 30`、`router 20`、`join 10`；人工介入恢复的任务再 `+100` |
| **老化防饿死** | 有效优先级 = 基础优先级 + 老化加成（每等 15 秒 +1，最多 +20），低优先级任务不会被永久压死 |

三条规则在**同一条 SQL** 内完成过滤、排序与认领，仍然包在 `BEGIN IMMEDIATE` 里，多 Worker 并发不会重复消费。

- **Worker 能力分区**：`start_engine(workers=2, capabilities=[{"llm:coder","llm:router"}, {"tool:execute_spec","join"}])`
- **LLM 并发闸门**：`_LLM_SEM` 限制同时在跑的 llm 节点数（默认 2），多 Worker 时不会把 provider 打爆
- **可观测**：`db.scheduler_snapshot()` 给出各能力的积压与最长等待，`engine.WORKER_STATE` 给出每个 Worker 的认领数，控制台「调度器」卡直接展示

一次真实派工（`parallel_demo`，默认单 Worker）会看到：`split` 之后两条分支都被下发到黑板，`merge` 以低优先级延迟轮询等分支齐活，`summarize` 收尾；日志里每次认领都能对上。把 `WORKER_COUNT` 调大或给能力分区，就是多 Worker 并行。

---

## 8. 可视化编排画布

侧边栏「视图」切到 **编排画布**，得到一块能看也能改的工作流编辑器，改的就是 `workflows/*.yaml` 本身。

| 能力 | 说明 |
|---|---|
| **画布** | `ui/canvas.py` 把 WorkflowSpec 画成分层 SVG：BFS 分层 + join 依赖下沉，节点按类型着色，边按时语义分色（成功/条件分支/失败回边/人工恢复/汇合依赖），回边从下方绕回，终态画成圆圈。纯 SVG，不拉 CDN，断网可用 |
| **节点编辑** | 选中节点后可改类型、Agent / 工具、上下文模板、`publish`、`next` / `on_success` / `on_failure` / `on_exhausted` / `on_resume`、`max_retries`、条件分支（每行 `标签=目标`）、`parallel`、`join` |
| **增删节点** | 新增（选类型给 id，自动带上必填字段）、删除；改动实时反映到画布 |
| **实时校验** | 每轮用 `validate_data()` 校验（可达性、悬空目标、重试与 on_failure 配对等），不合法时画布位置直接给出错误清单 |
| **原子保存** | `save_workflow_data()` 校验通过才写盘（临时文件 + replace，不留半截 YAML）；引擎按文件 mtime 热加载，**不用重启** |
| **YAML 预览** | 右侧实时显示将要落盘的内容，保存前可对照 |

实现上刻意避开了 Streamlit 的 session_state 坑：控件值每轮直接写回「草稿 dict」（普通 dict，可自由改），只有增删节点/还原这类会改变选项集合的操作才 bump 一代控件版本号，让输入框回到新的默认值。

---

## 9. 可靠性加固（都是实际踩过的坑）

| 隐患 | 加固 |
|---|---|
| 大模型调用没有超时 | 120s 超时 + 指数退避重试（超时/限流/5xx） |
| 节点异常只能标失败 | 异常走 `on_failure`，进入修复分支而不是判死 |
| 僵尸任务卡在 `running` | 超时自动重新入队，连续 3 次才判死 |
| YAML 写出无边界环 | 单 trace 步数上限 200，超限强制终止 |
| GBK 控制台打 emoji | stdout/stderr 降级为 `replace`，日志不再致命 |
| 整份代码规格塞进库 | 落库前 4000 字截断，全文留工作区 |

---

## 10. 已知限制

诚实声明，以下能力**尚未实现**，不要当成已有功能：

- `sub_workflow`：解析与校验已支持，**展开执行未实现**；执行时明确报错（不会退化成"按节点类型跑一遍并返回成功"）。
- 沙箱是**进程级 + 逐 trace 临时目录 + 超时**，不是容器级隔离；不可信代码请自行改用 Docker。
- 记忆只有 **trace 级**（`trace_vars`），没有跨任务长期记忆。
- 调度器已实现能力匹配 / 动态优先级 / 老化 / LLM 并发闸门，但默认仍是**单 Worker**（`WORKER_COUNT=1`），要多 Worker 需显式配置。
- `data_pipeline` **刻意不做自动修复**（数据口径错误自动改写风险高于收益，验收失败直接终止）；需要自动修复请用 `code_gen`。
- 并行支线的**落盘/执行按 trace 串行**（LLM 调用仍并行）：工作区按 trace 共享且 `execute_spec` 固定入口 `main.py`，并发跑沙箱会互相覆盖入口文件。真正的并发沙箱需要"每个 tool 节点独立工作目录"，尚未实现。
- 编排画布是**表单 + SVG 实时预览**式编辑，**不支持鼠标拖拽连线**（拖拽需要自建双向 Streamlit 组件；当前用表单+校验规避了 iframe 通信与半损坏的 YAML）。
- 画布保存会按定义重排 YAML：**文件头部注释保留，逐节点注释不保留**（界面上有提示）。
- 依赖安装：整批 `pip install` 失败时会**逐个重装**，装不上的包跳过并在报告里点名（一行错依赖不会拖死整条 trace）；但这意味着"部分依赖缺失"是可能状态，断言里的 `import` 会如实暴露它。
- Agent 工具轮数用尽时，会再做**一次不带工具的收尾调用**要结论；只有收尾也拿不到内容才判失败。
- **规格格式是硬门槛**：输出里没有 `=== ASSERTIONS ===` 段就直接拒绝执行（不会把散文当代码写进工作区）。
- **引擎是单写者**：同一个数据库只允许一个引擎进程持有租约（心跳 20s、90s 过期）。多开一个进程会打印"已有引擎在运行（pid …）"并拒绝启动 Worker——这条是踩过三次坑后加的：旧进程用着启动时的 Agent 快照抢走新任务，改完提示词却"没生效"。需要强制多开时设 `AEGIS_ALLOW_MULTI_ENGINE=1`。

---

## 11. 文档

- [编排引擎改造说明](docs/编排引擎改造说明.md)：改造前后对照、字段参考、等价性与迁移证据。
- [方案讲解 PPT](docs/Aegis-赛道一-方案讲解.pptx)：基于官方模板，16 页（封面 + 正文 14 页 + 结束页），含架构、编排字段、6 条工作流、8 类断言、自我循环、演示实拍、158 项测试；每页都写了演讲备注。
  - 重新生成：`python docs/build_ppt.py`（需按脚本内 TEMPLATE 路径放置官方模板）。
- [方案书（PDF）](docs/方案书.pdf)：按赛题要求的 8 个章节撰写，正文约 1.24 万字 / 27 页，含架构图、代码片段与系统截图。同一份内容的 Markdown 源在 [docs/方案书.md](docs/方案书.md)，图片在 `docs/images/`。
  - 重新生成：`python docs/build_proposal.py`（Markdown → DOCX），再用 Word 打开 DOCX 另存为 PDF；亦可只改 Markdown 后重跑这条命令。

---

## 12. 验证（怎么证明它真的能跑）

分四层验证，每层都是**真跑**而不是断言代码形状。脚本在 [`scripts/verify/`](scripts/verify/)，用法见该目录的 README。

| 层 | 覆盖 | 结果 |
|---|---|---|
| **单元/集成** | `unittest discover -s tests`：引擎、断言、产物隔离、调度、画布、Agent 工具、入参文件、引擎租约、依赖降级、工具轮兜底 | **158 项全绿** |
| **控制结构矩阵** | 顺序 / 条件分支命中与兜底 / 失败回边 / `max_retries` 预算 / 熔断转人工 / 人工建议恢复 / 并行扇出 + 汇合 / `sub_workflow` 明确拒绝 / 僵尸任务回收 / 并发认领不重复 | 14/14 |
| **调度器** | 两 Worker 能力分区、认领计数、`scheduler_snapshot`、LLM 并发闸门排队 | 9/9 |
| **工具边界** | 沙箱超时掐断、产物断言与 Agent 工具拒绝路径逃逸、`list_files`/`read_file`/`run_python`、真实 venv 装 `requests` | 11/11 |
| **前端交互（CDP 真点击）** | 空状态示例回填、「打开最近一条」、`?trace=` 深链、控制台六块区域、画布渲染 / 编辑表单 / YAML 预览 / 保存 / 校验错误提示 | 30/30 |

真实链路（真实 `DEEPSEEK_API_KEY`、真实沙箱、真实数据库）：

| 场景 | 结果 | 成本 |
|---|---|---|
| `code_gen` 成功路径 | 3 节点全 done，`main.py` 86 B | 1,124 tokens |
| `parallel_demo` | 并行两支线 + 汇合，6 任务全 done | 365 tokens |
| `data_pipeline` | 8 节点全 done，产出 `east_detail.csv` / `south_detail.csv` / `summary.csv` + `main.py` | 10,562 tokens |
| 真实人工介入恢复 | 卡住的 trace 下发建议后继续跑到新结论（不再停在 waiting） | 27,230 tokens / 15 次调用 / 17 次工具 |
| `log_triage` / `data_quality_gate` / `excel_report` | 三个企业日常场景，产物经人工核算一致 | 6,480 / 19,311 / 3,825 tokens |

这一轮（三个企业场景）又暴露并修掉 3 个问题：

5. **模型把散文当代码**：`log_triage` 首跑时模型改用工具后没按规格输出，引擎把整段中文说明写成 `main.py` → 改为**缺 `ASSERTIONS` 段直接拒绝执行**；
6. **Agent 提示词里写死了某个工作流的输出格式**：`analyst` 原本硬编码"支线A/支线B"，导致 `data_quality_gate` 拿到的规则是别的流程的格式 → 格式归**编排节点**，Agent 只管能力；
7. **多个引擎进程抢任务**：旧进程用旧 Agent 快照认领新任务，表现为"改了提示词却不生效" → 加**引擎单写者租约**（心跳 + 过期接管），并给画布处理链错列、失败标签缩短，避免图上的回边标签压住节点。

这次全量验证**发现并修掉了 4 个真实缺陷**（都补了回归测试）：

1. `sub_workflow` 节点被当成普通节点执行并返回成功 → 改为执行时明确拒绝；
2. 一行非法 `requirements` 让整批依赖安装失败、整条 trace 判死 → 改为逐个重装 + 跳过 + 点名；
3. 工具轮数用尽直接抛错 → 增加一次无工具收尾调用；
4. 画布保存把 YAML 注释全丢掉（含 Windows 写入的 BOM 场景）→ 保留文件头部注释块。