# 验证脚本（可复现）

这组脚本不是单元测试的重复，而是**分层验证**：每一层都真的跑起来看结果，而不是断言代码形状。

| 脚本 | 覆盖 | 是否花钱/联网 | 期望输出 |
|---|---|---|---|
| `verify_matrix.py` | 编排控制结构矩阵：顺序 / 条件分支 / 失败回边 / 重试预算 / 熔断转人工 / 人工恢复 / 并行汇合 / sub_workflow 拒绝 / 僵尸回收 / 并发认领 | 否（假 LLM + 假沙箱 + 临时库） | `14/14 通过` |
| `verify_scheduler.py` | 调度器：两 Worker 能力分区、认领计数、快照、LLM 并发闸门 | 否 | `9/9 通过` |
| `verify_tools_edge.py` | 工具边界：沙箱超时、路径逃逸拒绝、Agent 工具、第三方依赖安装 | 依赖安装需要网络 | `11/11 通过` |
| `verify_ui.py` | 前端交互（CDP 真点击）：空状态示例回填、trace 视图各卡片、深链、画布渲染/编辑/保存/校验提示 | 需要先启动 app（见下） | `30/30 通过` |

## 怎么跑

离线三件套（不碰真实数据库、不调真实模型）：

```bash
python scripts/verify/verify_matrix.py
python scripts/verify/verify_scheduler.py
python scripts/verify/verify_tools_edge.py
```

前端（需要 app 与一个 headless 浏览器）：

```bash
# 1. 起应用
streamlit run app.py --server.port 8507 --server.headless true
# 2. 起 headless 浏览器并开 CDP
msedge --headless=new --remote-debugging-port=9223 --remote-allow-origins=* --user-data-dir=<临时目录> about:blank
# 3. 跑（第二个参数是页面要打开的 trace id，可任意取一条已完成记录）
python scripts/verify/verify_ui.py <trace_id>
```

`verify_ui.py` 会在 `workflows/` 下临时创建 `_ui_probe.yaml` 作为画布编辑的探针，测完请自行删除；
可用 `AEGIS_UI_URL` 环境变量改应用地址。

## 说明

- 三个离线脚本会**替换** `engine.ask_agent_spec` 与 `TOOL_REGISTRY["execute_spec"]`，并指向临时数据库，
  因此不消耗 token、不污染 `aegis_platform.db`。
- `verify_ui.py` 无法用 CDP 合成 Streamlit 受控组件的文本输入（真实键盘可以），
  所以"新增节点"只验证到"按钮有响应 + 后端校验提示"，新增逻辑本身由 `tests/test_canvas.py` 覆盖。