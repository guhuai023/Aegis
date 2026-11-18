# -*- coding: utf-8 -*-
"""前端交互验证（CDP 真点击）。

注意：Streamlit 的「示例需求」块只在**空状态**（没有 trace_id）渲染，
所以验证必须分两趟：
  Pass A 空状态（无 trace）：示例需求 + 「填入」
  Pass B 有 trace：KPI / 拓扑 / 日志 / 交付物 / 深链 / 打开最近一条
  Pass C 编排画布：渲染 / 编辑表单 / 新增节点 / 保存 / 校验失败提示
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import requests
import websocket

PORT = 9223
PROBE = REPO / "workflows" / "_ui_probe.yaml"
SHOT = Path(tempfile.gettempdir()) / "aegis_ui_verify"
WS = None
MID = 0
RESULTS = []

HELPERS = """
window.__btn = (t) => [...document.querySelectorAll('button')]
        .find(b => (b.innerText||'').trim() === t);
window.__btns = (t) => [...document.querySelectorAll('button')]
        .filter(b => (b.innerText||'').trim() === t);
window.__setVal = (el, v) => {
  const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
};
window.__text = () => document.body.innerText || '';
"""


def cmd(method, params=None):
    global MID
    MID += 1
    WS.send(json.dumps({"id": MID, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(WS.recv())
        if msg.get("id") == MID:
            if "error" in msg:
                raise RuntimeError("%s: %s" % (method, msg["error"]))
            return msg.get("result", {})


def js(expr):
    r = cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    if r.get("exceptionDetails"):
        return None
    return r.get("result", {}).get("value")


def inject():
    js(HELPERS)


def wait_for(expr, timeout=45):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if js(expr):
                return True
        except Exception:
            pass
        time.sleep(0.35)
    return False


def snap(name):
    SHOT.mkdir(parents=True, exist_ok=True)
    res = cmd("Page.captureScreenshot", {"format": "png"})
    (SHOT / f"{name}.png").write_bytes(base64.b64decode(res["data"]))


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def goto(url, ready, timeout=70):
    cmd("Page.navigate", {"url": url})
    wait_for("document.readyState === 'complete'", 40)
    ok = wait_for(ready, timeout)
    time.sleep(1.5)
    inject()
    return ok


def main():
    global WS
    trace_id = sys.argv[1] if len(sys.argv) > 1 else ""
    base = os.environ.get("AEGIS_UI_URL", "http://127.0.0.1:8507/")

    target = None
    for _ in range(80):
        try:
            pages = [t for t in requests.get("http://127.0.0.1:%d/json" % PORT, timeout=1).json()
                     if t.get("type") == "page"]
            if pages:
                target = pages[0]
                break
        except Exception:
            pass
        time.sleep(0.4)
    if not target:
        print("找不到 CDP page")
        return 1

    WS = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=60,
                                     suppress_origin=True)
    cmd("Page.enable")
    cmd("Runtime.enable")
    cmd("Emulation.setDeviceMetricsOverride",
        {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
    cmd("Page.addScriptToEvaluateOnNewDocument", {"source":
        "window.__errs=[];"
        "window.addEventListener('error',e=>window.__errs.push(String(e.message)));"
        "window.addEventListener('unhandledrejection',e=>window.__errs.push(String(e.reason)));"})

    # ======================= Pass A：空状态 =======================
    print("\n=== Pass A 空状态（无 trace）===", flush=True)
    goto(base, "!!window.__btn && !!document.querySelector('textarea')")
    body = js("document.body.innerText") or ""
    check("A1 空状态有引导文案", "示例需求" in body, body[:100])
    fills = js("document.querySelectorAll('button').length") or 0
    check("A2 示例需求块渲染", ("生成销售汇总 CSV" in body) and ("爬取书籍信息" in body), body[:160])

    before = js("(document.querySelector('textarea')||{}).value || ''") or ""
    clicked = js("(() => { const b = window.__btns('填入')[0]; if(!b) return false; b.click(); return true; })()")
    time.sleep(3)
    after = js("(document.querySelector('textarea')||{}).value || ''") or ""
    errs = js("window.__errs || []") or []
    check("A3 「填入」把示例写进需求框", bool(clicked) and after != before,
          f"clicked={clicked} before={before[:24]!r} after={after[:24]!r}")
    check("A3 「填入」无 StreamlitAPIException",
          not any("StreamlitAPIException" in str(e) for e in errs), str(errs)[:160])
    snap("empty_state")

    # ======================= Pass B：有 trace =======================
    print("\n=== Pass B 有 trace ===", flush=True)
    if trace_id:
        ok = goto(f"{base}?trace={trace_id}", "!!document.querySelector('.ag-canvas, .ag-stats, .ag-panel')")
        page = js("window.__text()") or ""
        check("B1 ?trace= 深链生效", trace_id in page, page[:100])
        for block in ("执行拓扑", "节点明细", "执行日志", "运行环境", "调度器", "任务交付物"):
            check(f"B2 控制台区块：{block}", block in page)
        check("B3 顶栏显示引擎状态", ("引擎运行中" in page) or ("引擎已停止" in page))
        check("B4 拓扑 SVG 已渲染", js("!!document.querySelector('svg')"))
        kpi = js("(document.querySelector('.ag-stats')||{}).innerText || ''") or ""
        check("B5 KPI 有数字", any(ch.isdigit() for ch in kpi), kpi[:80])
        snap("trace_view")

        if js("!!window.__btn('打开最近一条')"):
            js("window.__btn('打开最近一条').click()")
            time.sleep(3)
            check("B6 「打开最近一条」可用", "执行拓扑" in (js("window.__text()") or ""))
        else:
            check("B6 「打开最近一条」按钮存在", False, "按钮缺失")
    else:
        print("  （跳过 Pass B：未提供 trace id）", flush=True)

    # ======================= Pass C：编排画布 =======================
    print("\n=== Pass C 编排画布 ===", flush=True)
    clicked = js("""(() => {
        const labels = [...document.querySelectorAll('[data-testid="stRadio"] label')];
        const hit = labels.find(l => (l.textContent||'').trim() === '编排画布');
        if (hit) hit.click();
        return !!hit;
    })()""")
    ok = wait_for("!!document.querySelector('.ag-canvas svg')", 70)
    inject()
    check("C1 视图切到编排画布并渲染 SVG", bool(clicked) and ok)
    time.sleep(1)

    body = js("window.__text()") or ""
    for field in ("选择节点", "类型", "显示名", "上下文模板", "max_retries", "新增节点"):
        check(f"C2 编辑区字段：{field}", field in body)
    yaml_text = js("""(() => {
        const blocks = [...document.querySelectorAll('pre, code')].map(e => e.innerText || '');
        return blocks.find(t => t.includes('name:') && t.includes('entry:')) || '';
    })()""") or ""
    check("C3 YAML 预览含 entry", "entry:" in yaml_text, yaml_text[:80])
    snap("canvas")

    # 新增节点：用 placeholder 定位输入框（label 结构不稳定）
    # 说明：Streamlit 的 text_input 是受控组件，CDP 合成的键盘事件喂不进 React 状态
    # （真实键盘没问题）。所以这里验证它**能证明的部分**：
    #   ① 按钮存在且点击有响应（空 id 时后端校验给出"节点 id 不能为空"）
    #   ② 保存真的重写文件、且文件仍可被引擎加载
    # 新增节点/校验逻辑的等价覆盖在 tests/test_canvas.py 与 tests/test_workflow_editor.py 里。
    before_file = PROBE.read_text(encoding="utf-8") if PROBE.exists() else ""
    js("(window.__btn('添加节点')||{click:()=>false}).click()")
    warn = wait_for("window.__text().includes('不能为空') || window.__text().includes('已存在')", 20)
    check("C4 新增节点按钮有响应（空 id 触发后端校验）", warn)

    js("(window.__btn('保存到 workflows/')||{click:()=>false}).click()")
    time.sleep(4)
    after_file = PROBE.read_text(encoding="utf-8") if PROBE.exists() else ""
    check("C5 保存真的重写了文件", after_file and after_file != before_file,
          f"len {len(before_file)}->{len(after_file)}")
    check("C5 保存保留文件头注释", after_file.lstrip().startswith("#"), after_file[:40])
    check("C6 保存后给出成功提示", "已写入" in (js("window.__text()") or ""))

    # 保存后的文件必须仍能被定义层加载（否则引擎会热加载到坏文件）
    try:
        from core.workflow import load_workflow
        spec = load_workflow("_ui_probe", use_cache=False)
        check("C5 保存后的文件仍可被引擎加载", len(spec.nodes) == 2, str(list(spec.nodes)))
    except Exception as exc:
        check("C5 保存后的文件仍可被引擎加载", False, str(exc))

    snap("canvas_saved")

    # 删除节点 -> 校验失败提示
    js("(window.__btn('删除该节点')||{click:()=>false}).click()")
    err_shown = wait_for("window.__text().includes('校验未通过') || window.__text().includes('不可达') || window.__text().includes('entry') "
                         "|| window.__text().includes('不存在')", 30)
    check("C7 删节点后出现校验错误提示", err_shown, (js("window.__text()") or "")[:160])
    snap("canvas_error")

    errs = js("window.__errs || []") or []
    check("C8 无致命前端异常", not any("StreamlitAPIException" in str(e) for e in errs), str(errs)[:160])

    print("\n" + "=" * 60, flush=True)
    bad = [r for r in RESULTS if not r[1]]
    print(f"前端交互验证：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过", flush=True)
    for name, ok, detail in bad:
        print("  ❌", name, "|", detail, flush=True)
    print("截图目录:", SHOT, flush=True)
    WS.close()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

