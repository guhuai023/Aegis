"""Agent 注册表：agents/*.yaml -> AgentSpec -> 运行时按名字取用。

赛题「Agent 团队定义与管理」的落点：
    角色 / 模型 / 工具集 / 记忆策略 / 版本 全部声明在 YAML 里，
    引擎只认 Agent 名字，不认具体提示词——换角色、加 Agent、发新版本都不用改 Python。

同时修掉旧版两个可靠性硬伤：
    1) 大模型调用没有超时 -> 一次挂起会永久卡死单线程引擎。现在有 timeout。
    2) 网络抖动直接抛到引擎 -> 整条工作流静默死亡。现在有退避重试。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from core.tools import call_tool, tool_schemas

BASE_DIR = Path(__file__).resolve().parent.parent
AGENTS_DIR = BASE_DIR / "agents"


def _load_dotenv():
    """极简 .env 加载器：读取项目根目录 .env（不覆盖已有环境变量），无第三方依赖"""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()

# 单次请求超时与重试次数（秒 / 次），可用环境变量覆盖
REQUEST_TIMEOUT = float(os.environ.get("AEGIS_LLM_TIMEOUT", "120"))
MAX_ATTEMPTS = int(os.environ.get("AEGIS_LLM_ATTEMPTS", "3"))

_CLIENT = None


class AgentError(RuntimeError):
    """Agent 调用失败（含重试耗尽）"""


@dataclass
class AgentSpec:
    name: str
    role_prompt: str
    label: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.3
    max_tokens: int | None = None
    tools: list = field(default_factory=list)     # 能力画像：该 Agent 允许使用的工具
    memory: str = "none"                          # none / trace
    can: list = field(default_factory=list)
    version: int = 1
    path: Path | None = None

    def public(self) -> dict:
        """给前端/接口用的展示信息（不含提示词全文）"""
        return {
            "name": self.name, "label": self.label or self.name, "version": self.version,
            "model": self.model, "temperature": self.temperature,
            "tools": list(self.tools), "memory": self.memory, "can": list(self.can),
        }


# ---------------------------------------------------------------- 注册表

_REGISTRY: dict[str, dict[int, AgentSpec]] = {}


def _parse_agent(data: dict, path: Path) -> AgentSpec:
    name = str(data.get("name") or path.stem).strip()
    if not name:
        raise ValueError(f"{path.name}: 缺少 name")
    role = data.get("role_prompt")
    if not role or not str(role).strip():
        raise ValueError(f"{path.name}: 缺少 role_prompt")
    return AgentSpec(
        name=name,
        role_prompt=str(role).strip(),
        label=str(data.get("label") or ""),
        model=str(data.get("model") or "deepseek-chat"),
        temperature=float(data.get("temperature") if data.get("temperature") is not None else 0.3),
        max_tokens=(int(data["max_tokens"]) if data.get("max_tokens") else None),
        tools=[str(t) for t in (data.get("tools") or [])],
        memory=str(data.get("memory") or "none"),
        can=[str(c) for c in (data.get("can") or [])],
        version=int(data.get("version") or 1),
        path=path,
    )


def load_agents(directory: str | Path | None = None) -> dict[str, AgentSpec]:
    """扫描 agents/*.yaml，装载全部 Agent（同名多版本共存，默认取最高版本）。"""
    directory = Path(directory) if directory else AGENTS_DIR
    _REGISTRY.clear()
    if not directory.exists():
        return {}
    for path in sorted(directory.glob("*.y*ml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            spec = _parse_agent(data, path)
        except Exception as e:  # 单个文件写坏不影响其它 Agent
            print(f"⚠️ [Agent] 跳过非法定义 {path.name}: {e}")
            continue
        _REGISTRY.setdefault(spec.name, {})[spec.version] = spec
    return {name: max(versions.values(), key=lambda s: s.version)
            for name, versions in _REGISTRY.items()}


def register_agent(data: dict, directory: str | Path | None = None) -> AgentSpec:
    """动态注册一个 Agent（赛题要求的「动态注册」入口）。

    data 可以是完整的 Agent 定义字典，也可以只带 name/version（此时从 agents/ 目录重载）。
    不落盘：运行时注册，重载后失效。
    """
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("register_agent 需要 name")
    version = int(data.get("version") or 1)

    if len(data) <= 1 or "role_prompt" not in data:
        if name in _REGISTRY and version in _REGISTRY[name]:
            return _REGISTRY[name][version]
        raise ValueError(f"未找到 Agent 定义: {name} v{version}")

    spec = _parse_agent(data, Path(directory or AGENTS_DIR) / f"{name}.yaml")
    _REGISTRY.setdefault(name, {})[version] = spec
    print(f"✅ [Agent] 动态注册 {name} v{version}")
    return spec


def get_agent(name: str, version: int | None = None) -> AgentSpec:
    if name not in _REGISTRY:
        raise AgentError(f"未注册的 Agent: {name}（检查 agents/*.yaml 与节点里声明的 agent 名）")
    versions = _REGISTRY[name]
    if version is None:
        return max(versions.values(), key=lambda s: s.version)
    if version not in versions:
        raise AgentError(f"Agent {name} 没有版本 v{version}（已有: {sorted(versions)}）")
    return versions[version]


def list_agents() -> list[dict]:
    return [max(v.values(), key=lambda s: s.version).public() for v in _REGISTRY.values()]


def reload_agents() -> dict[str, AgentSpec]:
    return load_agents()


# 导入即装载；失败不阻塞 UI（缺定义会在真正执行到该节点时报明确错误）
try:
    load_agents()
except Exception as e:  # pragma: no cover - 防御式
    print(f"⚠️ [Agent] 初始装载失败: {e}")


# ---------------------------------------------------------------- 调用

def _client():
    global _CLIENT
    if not API_KEY:
        raise AgentError(
            "未配置 DEEPSEEK_API_KEY：请在项目根目录创建 .env 文件（参考 .env.example），"
            "或设置系统环境变量后重启应用。"
        )
    if _CLIENT is None:
        from openai import OpenAI  # 延迟导入：没装 openai / 没配 Key 也不影响 UI 启动
        _CLIENT = OpenAI(api_key=API_KEY, base_url=BASE_URL,
                         timeout=REQUEST_TIMEOUT, max_retries=0)
    return _CLIENT


def _retryable(exc: Exception) -> bool:
    """只对「值得重试」的错误退避重试：超时/断连/限流/5xx。"""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    if getattr(exc, "status_code", None) and int(exc.status_code) >= 500:
        return True
    return type(exc).__name__ in {
        "APITimeoutError", "APIConnectionError", "RateLimitError",
        "InternalServerError", "ServiceUnavailableError", "APIStatusError",
    }


# 工具调用上限：防模型反复试探把 token 烧光
MAX_TOOL_ROUNDS = int(os.environ.get("AEGIS_TOOL_ROUNDS", "4"))
MAX_TOOL_CALLS = int(os.environ.get("AEGIS_TOOL_CALLS", "4"))


def _request(client, kwargs):
    """单次 completion 请求 + 退避重试。

    只把「值得重试」的错误吃掉：超时 / 断连 / 限流 / 5xx / 模型返回空内容。
    其它错误（比如 401 鉴权失败）立刻上抛，由节点的 on_failure 处理。
    """
    last_err: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            print(f"🤖 [API] {kwargs.get('model')} 第 {attempt}/{MAX_ATTEMPTS} 次调用…")
            resp = client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            if (msg.content or "").strip() or getattr(msg, "tool_calls", None):
                print("✅ [API] 返回成功")
                return resp
            last_err = AgentError("模型返回了空内容")
            print("⚠️ [API] 返回空内容，重试…")
        except Exception as e:
            if not _retryable(e):
                raise AgentError(f"大模型调用失败（不可重试）: {e}") from e
            last_err = e
            print(f"⚠️ [API] {type(e).__name__}: {e}")
        if attempt < MAX_ATTEMPTS:
            backoff = min(2 ** attempt, 8)
            print(f"⏳ [API] {backoff}s 后重试…")
            time.sleep(backoff)
    raise AgentError(f"大模型连续 {MAX_ATTEMPTS} 次失败：{last_err}") from last_err


def _accumulate(usage: dict, resp) -> dict:
    """把本次响应的 token 用量累加进 usage（字段缺失就跳过，绝不因此失败）。"""
    u = getattr(resp, "usage", None)
    if u is None:
        return usage
    for key, attr in (("prompt", "prompt_tokens"),
                      ("completion", "completion_tokens"),
                      ("total", "total_tokens")):
        try:
            usage[key] = int(usage.get(key, 0)) + int(getattr(u, attr, 0) or 0)
        except (TypeError, ValueError):
            pass
    return usage


def ask_agent(role_prompt: str, user_prompt: str, model: str = "deepseek-chat",
              temperature: float | None = None, max_tokens: int | None = None,
              *, tools=None, tool_ctx=None, on_tool=None, on_usage=None,
              max_rounds: int | None = None) -> str:
    """调用大模型：带超时 + 指数退避重试，支持工具调用循环。

    tools + tool_ctx 同时给定时进入 Function Calling 循环：
        模型返回 tool_calls -> 引擎执行工具 -> 结果以 role=tool 回灌 -> 模型继续推理，
    直到它给出最终文本。轮数上限（默认 AEGIS_TOOL_ROUNDS=4）与单轮调用数上限
    （AEGIS_TOOL_CALLS=4）都是为了防模型反复试探把 token 烧光。
    """
    client = _client()
    base = {"model": model, "timeout": REQUEST_TIMEOUT}
    if temperature is not None:
        base["temperature"] = temperature
    if max_tokens:
        base["max_tokens"] = max_tokens

    messages = [
        {"role": "system", "content": role_prompt},
        {"role": "user", "content": user_prompt},
    ]
    usage = {"calls": 0, "tools": 0, "prompt": 0, "completion": 0, "total": 0}
    enabled = bool(tools) and tool_ctx is not None
    rounds = MAX_TOOL_ROUNDS if max_rounds is None else int(max_rounds)

    for round_no in range(rounds + 1):
        kwargs = dict(base)
        kwargs["messages"] = list(messages)
        if enabled:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        resp = _request(client, kwargs)
        usage["calls"] += 1
        _accumulate(usage, resp)
        msg = resp.choices[0].message
        calls = list(getattr(msg, "tool_calls", None) or [])

        if calls and enabled and round_no < rounds:
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.function.name,
                                  "arguments": c.function.arguments or "{}"}}
                    for c in calls
                ],
            })
            for call in calls[:MAX_TOOL_CALLS]:
                name = getattr(call.function, "name", "") or ""
                raw = getattr(call.function, "arguments", "") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    args = {}
                observation = call_tool(name, tool_ctx, args)
                usage["tools"] += 1
                if on_tool:
                    try:
                        on_tool(name, args, observation)
                    except Exception as e:      # 回调只做记录，不能拖垮调用
                        print(f"⚠️ [工具] 回调异常: {e}")
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": observation})
            continue

        content = (msg.content or "").strip()
        if on_usage:
            try:
                on_usage(usage)
            except Exception as e:
                print(f"⚠️ [用量] 回调异常: {e}")
        if not content:
            raise AgentError("模型在工具调用之后仍未给出最终内容")
        return content

    if on_usage:
        try:
            on_usage(usage)
        except Exception:
            pass
    raise AgentError(f"工具调用轮数超过上限（{rounds} 轮）仍未收敛")


def ask_agent_spec(spec: AgentSpec, user_prompt: str, *, ctx=None,
                   on_tool=None, on_usage=None) -> str:
    """按 Agent 定义调用（引擎走这个入口）。

    ctx 是 ToolContext；只有当 Agent 在 YAML 里声明了 tools 才会真的挂工具。
    """
    schemas = tool_schemas(spec.tools) if spec.tools else None
    return ask_agent(spec.role_prompt, user_prompt, model=spec.model,
                     temperature=spec.temperature, max_tokens=spec.max_tokens,
                     tools=schemas, tool_ctx=ctx, on_tool=on_tool, on_usage=on_usage)


def router_agent(user_input: str) -> str:  # 兼容旧调用点
    """分诊台（兼容旧接口；新代码请走 Agent 注册表 + 工作流条件分支）"""
    return ask_agent_spec(get_agent("router"), user_input)