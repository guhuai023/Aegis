# -*- coding: utf-8 -*-
"""CDP 驱动（演示视频用）：连一个已开启远程调试的 Edge，控制页面。

复用 verify_ui.py 里验证过的做法：JS 求值 + 按文本点击 + 平滑滚动。
"""
import json
import time

import requests
import websocket

PORT = 9223
WS = None
MID = 0


def list_targets():
    return requests.get(f"http://127.0.0.1:{PORT}/json/list", timeout=5).json()


def connect(url_substr="127.0.0.1:8507"):
    """连到指定页面的 CDP target。"""
    global WS
    for _ in range(60):
        try:
            for t in list_targets():
                if t.get("type") == "page" and url_substr in t.get("url", ""):
                    WS = websocket.create_connection(
                        t["webSocketDebuggerUrl"], timeout=40, suppress_origin=True)
                    cmd("Page.enable")
                    cmd("Runtime.enable")
                    return t
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("没有找到可连接的页面 target")


def cmd(method, params=None):
    global MID
    MID += 1
    WS.send(json.dumps({"id": MID, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(WS.recv())
        if msg.get("id") == MID:
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return msg.get("result", {})


def js(expr):
    r = cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    if r.get("exceptionDetails"):
        return None
    return r.get("result", {}).get("value")


def wait_for(expr, timeout=45):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if js(expr):
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def goto(url, ready_expr=None, timeout=60, settle=1.2):
    cmd("Page.navigate", {"url": url})
    if ready_expr:
        wait_for(ready_expr, timeout)
    time.sleep(settle)


def click_text(text, timeout=12):
    """按可见文本点击（精确匹配优先，其次包含匹配）。"""
    expr = """(function(t){
        const all=[...document.querySelectorAll('button,label,p,span,a,div,li')];
        let el=all.find(e=>e.innerText&&e.innerText.trim()===t);
        if(!el) el=all.find(e=>e.innerText&&e.innerText.includes(t));
        if(!el) return false;
        el.scrollIntoView({block:'center'});
        el.click();
        return true;
    })(%s)""" % json.dumps(text)
    ok = js(expr)
    if ok:
        time.sleep(1.2)
    return ok


def click_selector(selector):
    expr = """(function(s){
        const el=document.querySelector(s);
        if(!el) return false;
        el.scrollIntoView({block:'center'});
        el.click();
        return true;
    })(%s)""" % json.dumps(selector)
    ok = js(expr)
    if ok:
        time.sleep(1.2)
    return ok


def select_option(label_text, option_text, timeout=10):
    """Streamlit 下拉：先点开控件，再点选项。"""
    opened = click_text(label_text, timeout=timeout)
    if not opened:
        return False
    time.sleep(0.8)
    picked = click_text(option_text, timeout=timeout)
    if picked:
        time.sleep(1.5)
    return picked


def _scroller_js(inner):
    return """(function(){
        function pick(){
            const cands=[document.querySelector('[data-testid=\"stMain\"]'),
                          document.scrollingElement, document.documentElement];
            for (const c of cands){
                if (c && c.scrollHeight > c.clientHeight + 20) return c;
            }
            return document.scrollingElement || document.documentElement;
        }
        const el=pick();
        %s
        return el.scrollTop;
    })()""" % inner


def scroll_to(px, smooth=False):
    inner = ("el.scrollTo({top:%d, behavior:'%s'});" % (px, "smooth" if smooth else "auto"))
    return js(_scroller_js(inner))


def scroll_by(dy, smooth=True):
    inner = ("el.scrollTo({top: el.scrollTop + %d, behavior:'%s'});"
             % (dy, "smooth" if smooth else "auto"))
    return js(_scroller_js(inner))


def smooth_scroll_through(points, per_step=0.9):
    """在录制过程中做"缓慢滚动"，让画面不死板。points 是若干目标位置。"""
    for p in points:
        scroll_to(p, smooth=True)
        time.sleep(per_step)


def screenshot(path):
    import base64
    r = cmd("Page.captureScreenshot", {"format": "png"})
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["data"]))
    return path

def click_at(x, y, button="left"):
    """真实鼠标事件（React 受控组件只认这个）。"""
    for etype in ("mousePressed", "mouseReleased"):
        cmd("Input.dispatchMouseEvent", {
            "type": etype, "x": float(x), "y": float(y), "button": button,
            "clickCount": 1, "buttons": 1 if etype == "mousePressed" else 0})
        time.sleep(0.06)


def elements(selector):
    """返回元素的可见文本与视口坐标。"""
    expr = """(function(sel){
        return [...document.querySelectorAll(sel)]
            .filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;})
            .map(e=>{const r=e.getBoundingClientRect();
                     return {text:(e.innerText||e.textContent||'').trim().slice(0,40),
                             x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2),
                             w:Math.round(r.width), h:Math.round(r.height)};});
    })(%s)""" % json.dumps(selector)
    return js(expr) or []


def _canvas_workflow():
    """读画布当前工作流（下拉控件里 "工作流文件\n\n<name>" 的最后一行）"""
    return js("""(function(){
        const boxes=[...document.querySelectorAll('[data-testid="stSelectbox"]')];
        const b=boxes.find(x=>x.innerText.trim().startsWith('工作流文件'));
        if(!b) return '';
        const lines=b.innerText.trim().split('\\n').filter(Boolean);
        return lines[lines.length-1].trim();
    })()""")


def _escape_key():
    for etype in ("keyDown", "keyUp"):
        cmd("Input.dispatchKeyEvent", {"type": etype, "key": "Escape", "code": "Escape",
                                       "windowsVirtualKeyCode": 27})


def pick_selectbox(label_text, option_text, tries=4):
    """Streamlit 下拉：点开 → 轮询选项 → 点选。

    关键：先 Page.bringToFront —— 浏览器窗口没有焦点时，baseweb 的弹层不会出现
    （点击事件仍会派发，所以现象是"点了没反应"，很难查）。
    """
    try:
        cmd("Page.bringToFront")
        time.sleep(0.3)
    except Exception:
        pass
    for attempt in range(tries):
        _escape_key()
        time.sleep(0.35)
        ctrl = js("""(function(label){
            const boxes=[...document.querySelectorAll('[data-testid="stSelectbox"]')];
            const box=boxes.find(b=>b.innerText.trim().startsWith(label));
            if(!box) return null;
            const r=box.getBoundingClientRect();
            // 实测：点整个控件框的偏上位置能稳定打开弹层
            return {x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height*0.72)};
        })(%s)""" % json.dumps(label_text))
        if not ctrl:
            time.sleep(0.7)
            continue
        click_at(ctrl["x"], ctrl["y"])
        # 轮询等弹层出现（最多 3 秒）
        for _ in range(12):
            time.sleep(0.25)
            opts = elements('li[role="option"], [data-baseweb="menu"] li, [role="listbox"] li')
            if opts:
                for o in opts:
                    if o["text"] == option_text or o["text"].startswith(option_text):
                        # 弹层可能比视口长：先把目标项滚到可见位置再取坐标
                        pos = js("""(function(t){
                            const items=[...document.querySelectorAll('li[role="option"], [role="listbox"] li')];
                            const el=items.find(e=>(e.innerText||'').trim()===t)
                                   || items.find(e=>(e.innerText||'').trim().startsWith(t));
                            if(!el) return null;
                            el.scrollIntoView({block:'center'});
                            const r=el.getBoundingClientRect();
                            return {x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2),
                                    visible: r.top>=0 && r.bottom<=window.innerHeight};
                        })(%s)""" % json.dumps(option_text))
                        if pos and pos.get("visible", True):
                            click_at(pos["x"], pos["y"])
                        else:
                            click_at(o["x"], o["y"])
                        time.sleep(1.5)
                        return True
                break        # 弹层开了但没有目标项
        print("    [retry] 下拉未就绪，重试 %d/%d" % (attempt + 1, tries))
        time.sleep(0.6)
    return False


def scroll_top():
    """回到页面顶部，并把画布容器的横向滚动归零（否则镜头会切掉左半张图）"""
    js("""(function(){
        const m=document.querySelector('[data-testid="stMain"]');
        if(m) m.scrollTo({top:0, left:0});
        window.scrollTo(0,0);
        document.querySelectorAll('.ag-canvas').forEach(function(e){e.scrollLeft=0;});
        return true;
    })()""")
    time.sleep(0.5)


def smooth_scroll_through(points, per_step=0.9):
    """在录制过程中做"缓慢滚动"，让画面不死板。points 是若干目标位置。"""
    for p in points:
        scroll_to(p, smooth=True)
        time.sleep(per_step)


def screenshot(path):
    import base64
    r = cmd("Page.captureScreenshot", {"format": "png"})
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["data"]))
    return path

def click_at(x, y, button="left"):
    """真实鼠标事件（React 受控组件只认这个）。"""
    for etype in ("mousePressed", "mouseReleased"):
        cmd("Input.dispatchMouseEvent", {
            "type": etype, "x": float(x), "y": float(y), "button": button,
            "clickCount": 1, "buttons": 1 if etype == "mousePressed" else 0})
        time.sleep(0.06)


def elements(selector):
    """返回元素的可见文本与视口坐标。"""
    expr = """(function(sel){
        return [...document.querySelectorAll(sel)]
            .filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;})
            .map(e=>{const r=e.getBoundingClientRect();
                     return {text:(e.innerText||e.textContent||'').trim().slice(0,40),
                             x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2),
                             w:Math.round(r.width), h:Math.round(r.height)};});
    })(%s)""" % json.dumps(selector)
    return js(expr) or []


def _canvas_workflow():
    """读画布当前工作流（下拉控件里 "工作流文件\n\n<name>" 的最后一行）"""
    return js("""(function(){
        const boxes=[...document.querySelectorAll('[data-testid="stSelectbox"]')];
        const b=boxes.find(x=>x.innerText.trim().startsWith('工作流文件'));
        if(!b) return '';
        const lines=b.innerText.trim().split('\\n').filter(Boolean);
        return lines[lines.length-1].trim();
    })()""")


def _escape_key():
    for etype in ("keyDown", "keyUp"):
        cmd("Input.dispatchKeyEvent", {"type": etype, "key": "Escape", "code": "Escape",
                                       "windowsVirtualKeyCode": 27})


def pick_selectbox(label_text, option_text, tries=4):
    """Streamlit 下拉：点开 → 轮询选项 → 点选。

    关键：先 Page.bringToFront —— 浏览器窗口没有焦点时，baseweb 的弹层不会出现
    （点击事件仍会派发，所以现象是"点了没反应"，很难查）。
    """
    try:
        cmd("Page.bringToFront")
        time.sleep(0.3)
    except Exception:
        pass
    for attempt in range(tries):
        _escape_key()
        time.sleep(0.35)
        ctrl = js("""(function(label){
            const boxes=[...document.querySelectorAll('[data-testid="stSelectbox"]')];
            const box=boxes.find(b=>b.innerText.trim().startsWith(label));
            if(!box) return null;
            const r=box.getBoundingClientRect();
            // 实测：点整个控件框的偏上位置能稳定打开弹层
            return {x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height*0.72)};
        })(%s)""" % json.dumps(label_text))
        if not ctrl:
            time.sleep(0.7)
            continue
        click_at(ctrl["x"], ctrl["y"])
        # 轮询等弹层出现（最多 3 秒）
        for _ in range(12):
            time.sleep(0.25)
            opts = elements('li[role="option"], [data-baseweb="menu"] li, [role="listbox"] li')
            if opts:
                for o in opts:
                    if o["text"] == option_text or o["text"].startswith(option_text):
                        # 弹层可能比视口长：先把目标项滚到可见位置再取坐标
                        pos = js("""(function(t){
                            const items=[...document.querySelectorAll('li[role="option"], [role="listbox"] li')];
                            const el=items.find(e=>(e.innerText||'').trim()===t)
                                   || items.find(e=>(e.innerText||'').trim().startsWith(t));
                            if(!el) return null;
                            el.scrollIntoView({block:'center'});
                            const r=el.getBoundingClientRect();
                            return {x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2),
                                    visible: r.top>=0 && r.bottom<=window.innerHeight};
                        })(%s)""" % json.dumps(option_text))
                        if pos and pos.get("visible", True):
                            click_at(pos["x"], pos["y"])
                        else:
                            click_at(o["x"], o["y"])
                        time.sleep(1.5)
                        return True
                break        # 弹层开了但没有目标项
        print("    [retry] 下拉未就绪，重试 %d/%d" % (attempt + 1, tries))
        time.sleep(0.6)
    return False


def scroll_top():
    """回到页面顶部，并把画布容器的横向滚动归零（否则镜头会切掉左半张图）"""
    js("""(function(){
        const m=document.querySelector('[data-testid="stMain"]');
        if(m) m.scrollTo({top:0, left:0});
        window.scrollTo(0,0);
        document.querySelectorAll('.ag-canvas').forEach(function(e){e.scrollLeft=0;});
        return true;
    })()""")
    time.sleep(0.5)


def smooth_scroll_through(points, per_step=0.9):
    """在录制过程中做"缓慢滚动"，让画面不死板。points 是若干目标位置。"""
    for p in points:
        scroll_to(p, smooth=True)
        time.sleep(per_step)


def screenshot(path):
    import base64
    r = cmd("Page.captureScreenshot", {"format": "png"})
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["data"]))
    return path

def click_at(x, y, button="left"):
    """真实鼠标事件（React 受控组件只认这个）。"""
    for etype in ("mousePressed", "mouseReleased"):
        cmd("Input.dispatchMouseEvent", {
            "type": etype, "x": float(x), "y": float(y), "button": button,
            "clickCount": 1, "buttons": 1 if etype == "mousePressed" else 0})
        time.sleep(0.06)


def elements(selector):
    """返回元素的可见文本与视口坐标。"""
    expr = """(function(sel){
        return [...document.querySelectorAll(sel)]
            .filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;})
            .map(e=>{const r=e.getBoundingClientRect();
                     return {text:(e.innerText||e.textContent||'').trim().slice(0,40),
                             x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2),
                             w:Math.round(r.width), h:Math.round(r.height)};});
    })(%s)""" % json.dumps(selector)
    return js(expr) or []


def _canvas_workflow():
    """读画布当前工作流（下拉控件里 "工作流文件\n\n<name>" 的最后一行）"""
    return js("""(function(){
        const boxes=[...document.querySelectorAll('[data-testid="stSelectbox"]')];
        const b=boxes.find(x=>x.innerText.trim().startsWith('工作流文件'));
        if(!b) return '';
        const lines=b.innerText.trim().split('\\n').filter(Boolean);
        return lines[lines.length-1].trim();
    })()""")


def _escape_key():
    for etype in ("keyDown", "keyUp"):
        cmd("Input.dispatchKeyEvent", {"type": etype, "key": "Escape", "code": "Escape",
                                       "windowsVirtualKeyCode": 27})


def pick_selectbox(label_text, option_text, tries=4):
    """Streamlit 下拉：点开 → 轮询选项 → 点选。

    关键：先 Page.bringToFront —— 浏览器窗口没有焦点时，baseweb 的弹层不会出现
    （点击事件仍会派发，所以现象是"点了没反应"，很难查）。
    """
    try:
        cmd("Page.bringToFront")
        time.sleep(0.3)
    except Exception:
        pass
    for attempt in range(tries):
        _escape_key()
        time.sleep(0.35)
        ctrl = js("""(function(label){
            const boxes=[...document.querySelectorAll('[data-testid="stSelectbox"]')];
            const box=boxes.find(b=>b.innerText.trim().startsWith(label));
            if(!box) return null;
            const r=box.getBoundingClientRect();
            // 实测：点整个控件框的偏上位置能稳定打开弹层
            return {x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height*0.72)};
        })(%s)""" % json.dumps(label_text))
        if not ctrl:
            time.sleep(0.7)
            continue
        click_at(ctrl["x"], ctrl["y"])
        # 轮询等弹层出现（最多 3 秒）
        for _ in range(12):
            time.sleep(0.25)
            opts = elements('li[role="option"], [data-baseweb="menu"] li, [role="listbox"] li')
            if opts:
                for o in opts:
                    if o["text"] == option_text or o["text"].startswith(option_text):
                        # 弹层可能比视口长：先把目标项滚到可见位置再取坐标
                        pos = js("""(function(t){
                            const items=[...document.querySelectorAll('li[role="option"], [role="listbox"] li')];
                            const el=items.find(e=>(e.innerText||'').trim()===t)
                                   || items.find(e=>(e.innerText||'').trim().startsWith(t));
                            if(!el) return null;
                            el.scrollIntoView({block:'center'});
                            const r=el.getBoundingClientRect();
                            return {x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2),
                                    visible: r.top>=0 && r.bottom<=window.innerHeight};
                        })(%s)""" % json.dumps(option_text))
                        if pos and pos.get("visible", True):
                            click_at(pos["x"], pos["y"])
                        else:
                            click_at(o["x"], o["y"])
                        time.sleep(1.5)
                        return True
                break        # 弹层开了但没有目标项
        print("    [retry] 下拉未就绪，重试 %d/%d" % (attempt + 1, tries))
        time.sleep(0.6)
    return False


def scroll_top():
    """回到页面顶部，并把画布容器的横向滚动归零（否则镜头会切掉左半张图）"""
    js("""(function(){
        const m=document.querySelector('[data-testid="stMain"]');
        if(m) m.scrollTo({top:0, left:0});
        window.scrollTo(0,0);
        document.querySelectorAll('.ag-canvas').forEach(function(e){e.scrollLeft=0;});
        return true;
    })()""")
    time.sleep(0.5)


def smooth_scroll_through(points, per_step=0.9):
    for p in points:
        scroll_to(p, smooth=True)
        time.sleep(per_step)


def screenshot(path):
    import base64
    r = cmd("Page.captureScreenshot", {"format": "png"})
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["data"]))
    return path


def canvas_summary():
    """读画布下方那行汇总（"5 节点 · 7 条转移"），它来自真正渲染出的 SVG。"""
    return js("""(function(){
        const m=document.body.innerText.match(/(\\d+)\\s*节点\\s*·\\s*(\\d+)\\s*条转移/);
        return m? m[0] : '';
    })()""")


def go_canvas(workflow=None, expect=None, settle=2.0, tries=4):
    """进入编排画布并切到指定工作流。

    判据是"画布实际渲染出的汇总行"（expect），而不是下拉控件的显示值：
    控件值会先于 SVG 重绘更新，曾导致录到的画面还停在上一条流程。
    """
    click_text("编排画布")
    time.sleep(1.8)
    if workflow:
        for i in range(tries):
            if expect and expect in (canvas_summary() or ""):
                break
            pick_selectbox("工作流文件", workflow)
            time.sleep(1.5)
            if expect and expect in (canvas_summary() or ""):
                break
            print("    [retry] 画布还是 %r，期望 %r" % (canvas_summary(), expect))
            time.sleep(0.9)
        else:
            print("    [warn] 画布未切到", workflow)
    scroll_top()
    time.sleep(settle)
