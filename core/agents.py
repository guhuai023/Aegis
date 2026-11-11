import os
from pathlib import Path

from openai import OpenAI


def _load_dotenv():
    """极简 .env 加载器：读取项目根目录 .env（不覆盖已有环境变量），无第三方依赖"""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# 密钥一律来自环境变量 / .env，不再硬编码进源码
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()

# Key 缺失时不在导入期炸掉：延迟到真正调用时给出明确报错，UI 仍能正常启动
client = OpenAI(api_key=API_KEY or "not-configured", base_url=BASE_URL)


def ask_agent(role_prompt, user_prompt):
    if not API_KEY:
        raise RuntimeError(
            "未配置 DEEPSEEK_API_KEY：请在项目根目录创建 .env 文件（参考 .env.example），"
            "或设置系统环境变量后重启应用。"
        )
    for attempt in range(2):
        print("🤖 [API 请求] 正在呼叫大模型...")
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": role_prompt}, {"role": "user", "content": user_prompt}]
        )
        content = (response.choices[0].message.content or "").strip()
        if content:
            print("✅ [API 返回] 成功拿到结果")
            return content
        print(f"⚠️ [API] 第 {attempt + 1} 次返回了空内容，重试...")
    raise RuntimeError("大模型连续返回空内容（可能是模型服务异常），请稍后重试")


def router_agent(user_input):
    """分诊台：负责动态意图路由"""
    prompt = """
    你是智能调度系统的路由分发器。请判断用户的输入属于以下哪个场景：
    - 如果用户要求写代码、写程序、排错，请输出: [CODE]
    - 如果是其他任何问题（聊天、问好），请输出: [CHAT]
    只输出上述标签，不要输出任何其他文字。
    """
    return ask_agent(prompt, user_input)
