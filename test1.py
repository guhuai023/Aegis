from openai import OpenAI
import subprocess
import tempfile
import os
import time

#初始化实例（Key 从环境变量读取，参考 .env.example）
client=OpenAI(api_key=os.environ.get("DEEPSEEK_API_KEY",""),
              base_url=os.environ.get("DEEPSEEK_BASE_URL","https://api.deepseek.com"))
def ask_llm(system_prompt,user_prompt):
    response=client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role":"system","content":system_prompt},
            {"role":"user","content":user_prompt}
        ]
    )
    return response.choices[0].message.content.strip()

#
def run_code_sandbox(code):
    clean_code=code.replace("```python\n","").replace("```","").strip()
    with tempfile.NamedTemporaryFile(mode="w",delete=False,encoding='utf-8') as f:
        f.write(clean_code)
        filepath=f.name

    try:
        # 调用解释器
        result=subprocess.run(["python",filepath],capture_output=True,text=True,timeout=5)
        if result.returncode==0:
            return True,result.stdout
        else:
            return False,result.stderr
    except subprocess.TimeoutExpired:
        return False,"执行超时(存在死循环)"
    except Exception as e:
        return False,str(e)
    finally:
        os.remove(filepath)

def run_workflow(task):
    print(f"\n"+ "="*50)
    print(f"[系统]接受任务：{task}")
    print("="*50)

    #people 1 开发
    print("[开发Agent]正在思考并编写代码...")
    code=ask_llm(
        system_prompt="你是一个资深的python开发专家。请根据任务写代码。只输出python纯代码，不需要解释",
        user_prompt=task
    )