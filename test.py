from openai import OpenAI
import subprocess
import tempfile
import os
import time

# ==========================================
# 1. 配置大模型大脑 (Agent 推理核心)
# ==========================================
# 从环境变量读取 Key（可在项目根目录 .env 中配置，参考 .env.example）
client = OpenAI(api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))


def ask_llm(system_prompt, user_prompt):
    """封装基础的 LLM 调用接口"""
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
    )
    return response.choices[0].message.content.strip()


# ==========================================
# 2. 物理规则锚点 (确定性的验证工具)
# ==========================================
def run_code_sandbox(code):
    """真实执行环境：大模型会骗人，但编译器不会"""
    # 清理大模型可能带出的 markdown 标记
    clean_code = code.replace("```python\n", "").replace("```", "").strip()

    # 写入临时文件
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
        f.write(clean_code)
        filepath = f.name

    try:
        # 调用 Python 解释器执行，限制 5 秒超时（防止死循环）
        result = subprocess.run(["python", filepath], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return True, result.stdout
        else:
            return False, result.stderr
    except subprocess.TimeoutExpired:
        return False, "执行超时（可能存在死循环）"
    except Exception as e:
        return False, str(e)
    finally:
        os.remove(filepath)


# ==========================================
# 3. 多智能体协作编排引擎 (Workflow Engine)
# ==========================================
def run_workflow(task):
    print(f"\n" + "=" * 50)
    print(f"🚀 [系统] 接收任务: {task}")
    print("=" * 50)

    # 【角色 1：开发 Agent】生成初稿
    print("\n🤖 [开发 Agent] 正在思考并编写代码...")
    code = ask_llm(
        system_prompt="你是资深Python开发专家。请根据任务写代码。只输出纯Python代码，不要markdown标记，不要任何解释。",
        user_prompt=task
    )
    print(f"--- 初始生成代码 ---\n{code}\n")

    # 【记忆机制】初始化记忆池
    memory_pool = []
    max_retries = 2

    # 【自我循环 Self-Loop】
    for attempt in range(max_retries + 1):
        print(f"🔄 [系统] 正在进行第 {attempt + 1} 轮物理验证...")

        # 调用物理锚点验证
        is_success, output = run_code_sandbox(code)

        if is_success:
            print("✅ [规则锚点] 验证通过！")
            print(f"🎉 [任务完成] 运行结果:\n{output}")
            return

        # 验证失败，记录报错信息
        print(f"❌ [规则锚点] 验证失败！拦截到错误:\n{output}")
        memory_pool.append(f"第 {attempt + 1} 次失败报错: {output}")

        # 达到最大重试次数，触发人工介入
        if attempt == max_retries:
            print("\n🚨 [系统挂起] Agent 多次修复失败！触发 Human-in-the-loop 人工介入节点。")
            human_input = input("👉 请人类专家输入修复建议（例如：加上 try-except，或者检查类型转换）：")

            print("\n🤖 [修复 Agent] 收到人类专家指令，正在紧急修复...")
            fix_prompt = f"人类专家给出的修改建议是：{human_input}\n你之前写的坏代码如下：\n{code}\n请严格按照人类建议修复代码。只输出纯代码。"
            code = ask_llm(system_prompt="你是排错专家，只输出纯Python代码。", user_prompt=fix_prompt)

            # 最后一次验证
            final_success, final_output = run_code_sandbox(code)
            if final_success:
                print(f"🎉 [人工救援成功] 运行结果:\n{final_output}")
            else:
                print(f"💀 [系统崩溃] 人类介入后依然失败，报错：\n{final_output}")
            return

        # 未达到最大次数，触发【角色 2：修复 Agent】
        print("\n🤖 [修复 Agent] 正在根据报错历史修复代码...")
        memory_context = "\n".join(memory_pool)
        fix_prompt = f"原任务：{task}\n\n你的【历史错误记录】如下，绝对不能重蹈覆辙：\n{memory_context}\n\n你上一次写的代码：\n{code}\n\n请修复它。"

        code = ask_llm(
            system_prompt="你是排错专家。根据报错信息修复代码，只输出纯Python代码，不要解释。",
            user_prompt=fix_prompt
        )
        print(f"--- 修复后的代码 ---\n{code}\n")
        time.sleep(1)  # 稍微停顿，让演示更有节奏感


# ==========================================
# 4. 启动执行
# ==========================================
if __name__ == "__main__":
    # 故意提供一个带有陷阱的任务（除以零异常）
    # 这个任务旨在逼迫大模型犯错，从而展示系统的自我循环与人工介入能力
    target_task = "介绍一下你自己"
    run_workflow(target_task)