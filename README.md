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
├── agents/                 # router / coder / fixer / chat
├── workflows/              # code_gen / parallel_demo
├── tests/                  # 47 项离线测试（不联网）
├── docs/编排引擎改造说明.md  # 改造前后对照、字段参考、等价性证据
└── requirements.txt
```

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

- `sub_workflow`：解析与校验已支持，**展开执行未实现**。
- 沙箱是**进程级 + 逐 trace 临时目录 + 超时**，不是容器级隔离；不可信代码请自行改用 Docker。
- 记忆只有 **trace 级**（`trace_vars`），没有跨任务长期记忆。
- 调度器已实现能力匹配 / 动态优先级 / 老化 / LLM 并发闸门，但默认仍是**单 Worker**（`WORKER_COUNT=1`），要多 Worker 需显式配置。
- 编排画布是**表单 + SVG 实时预览**式编辑，**不支持鼠标拖拽连线**（拖拽需要自建双向 Streamlit 组件；当前用表单+校验规避了 iframe 通信与半损坏的 YAML）。

---

## 11. 文档

- [编排引擎改造说明](docs/编排引擎改造说明.md)：改造前后对照、字段参考、等价性与迁移证据。