"""Aegis 控制台的视觉层。

风格定位：**内部工程控制台**，不是产品官网/落地页。
刻意避免的东西（也就是所谓"AI 味"）：
  · 渐变发光页头、装饰性网格底纹、大标语式副标题
  · emoji 当图标、糖果色胶囊、彩色左侧竖条
  · 每块都套圆角大阴影卡片、满屏大留白
保留的东西：1px 描边、6~8px 圆角、等宽字体承载数字与 ID、
状态用「小圆点 + 文字」表达、表格走信息密度优先。

所有视觉都是本模块输出的原生 HTML/CSS，不依赖 Streamlit 内部 DOM 结构。
"""
import html as _html
import re

# ---------------------------------------------------------------- 色板（克制：一个主色 + 四个语义色）
INK = "#101828"        # 主文字
BODY = "#344054"       # 正文
MUTED = "#667085"      # 次要/标签
LINE = "#E4E7EC"       # 描边
LINE2 = "#F2F4F7"      # 行分割
CANVAS = "#F7F8FA"     # 画布
CARD = "#FFFFFF"       # 面板
BLUE = "#175CD3"       # 主色（链接/主按钮）
BLUE2 = "#175CD3"      # 运行中
BLUES = "#EFF4FF"      # 主色浅底
CYAN = "#0E7090"
GREEN = "#067647"      # 完成
AMBER = "#B54708"      # 待人工/告警
RED = "#B42318"        # 失败
PURPLE = "#5925DC"     # 人工指令
SLATE = "#98A2B3"      # 未开始

STATUS_STYLE = {
    "done":          ("完成", GREEN),
    "running":       ("运行中", BLUE2),
    "failed":        ("失败", RED),
    "waiting_human": ("待人工", AMBER),
    "pending":       ("未开始", SLATE),
    "joined":        ("已汇合", GREEN),
}
LEVEL_COLOR = {"INFO": "#475467", "RUNNING": BLUE2, "WARN": AMBER,
               "ERROR": RED, "HUMAN": PURPLE, "SUCCESS": GREEN, "DONE": GREEN,
               "TOOL": CYAN}
TYPE_LABEL = {"llm": "大模型", "tool": "工具", "join": "汇合", "human": "人工"}

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002190-\U000021FF\U00002600-\U000027BF"
    "\U000025A0-\U000025FF\U00002B00-\U00002BFF\U0000FE0F\U0000200D]"
)
_ARROWS = {"→": "->", "←": "<-", "⇒": "=>"}


def esc(v) -> str:
    return _html.escape(str(v if v is not None else ""))


def clean(text) -> str:
    """日志文本清洗：去掉 emoji / 变体选择符，箭头换成 ASCII，压缩空白。

    引擎日志里带 🎯✅📝♻️⚠️ 这类符号，在控制台里会显得很"玩具"；
    级别已经由独立列表达，消息体保持纯文本更好读。
    """
    s = str(text or "")
    for a, b in _ARROWS.items():
        s = s.replace(a, b)
    s = _EMOJI.sub("", s)
    return re.sub(r"\s{2,}", " ", s).strip()


CSS = """
<style>
:root{
  --ink:#101828; --body:#344054; --muted:#667085;
  --line:#E4E7EC; --line2:#F2F4F7; --card:#FFFFFF;
  --blue:#175CD3; --blues:#EFF4FF;
  --green:#067647; --amber:#B54708; --red:#B42318; --purple:#5925DC; --slate:#98A2B3;
  --mono:"Cascadia Mono","JetBrains Mono",Consolas,"Courier New",monospace;
  --sans:-apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
}
html,body,[data-testid="stAppViewContainer"]{font-family:var(--sans);}
#MainMenu,footer,[data-testid="stToolbar"]{visibility:hidden;}
[data-testid="stHeader"]{background:transparent; height:0;}
[data-testid="stAppViewContainer"]>.main .block-container,
[data-testid="stMainBlockContainer"]{padding:.9rem 1.1rem 2rem; max-width:1520px;}
[data-testid="stVerticalBlock"]{gap:.55rem;}

/* ---------- 侧边栏 ---------- */
[data-testid="stSidebar"]{background:var(--card); border-right:1px solid var(--line);}
[data-testid="stSidebar"] .block-container{padding:.7rem .85rem 1.4rem;}
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p{
  font-size:11px; font-weight:600; color:var(--muted); letter-spacing:.04em;
}

/* ---------- 控件：只改观感，窄边框、无阴影 ---------- */
.stButton>button{
  border-radius:6px; border:1px solid var(--line); font-weight:500; font-size:13px;
  color:var(--body); background:var(--card); box-shadow:none; transition:background .12s,border-color .12s;
}
.stButton>button:hover{border-color:#D0D5DD; background:#F9FAFB; color:var(--ink);}
.stButton>button[kind="primary"],[data-testid="stBaseButton-primary"]{
  background:var(--blue); border:1px solid var(--blue); color:#fff !important; box-shadow:none;
}
.stButton>button[kind="primary"]:hover{background:#1249A8; border-color:#1249A8;}
[data-testid="stTextArea"] textarea,[data-testid="stSelectbox"] div[data-baseweb="select"]>div{
  border-radius:6px !important; border-color:var(--line) !important;
  background:var(--card) !important; font-size:13px !important;
}
[data-testid="stTextArea"] textarea:focus{border-color:var(--blue) !important; box-shadow:0 0 0 3px rgba(23,92,211,.12) !important;}
[data-testid="stVerticalBlockBorderWrapper"]{
  border-radius:8px !important; border-color:var(--line) !important;
  background:var(--card); box-shadow:none !important;
}
[data-testid="stExpander"]{border:1px solid var(--line) !important; border-radius:8px !important;}
[data-testid="stExpander"] summary{font-size:13px; font-weight:500; color:var(--body);}
[data-testid="stCode"]{border-radius:6px;}
[data-testid="stCode"] pre{font-size:12px !important;}

/* ---------- 顶栏（替代 hero） ---------- */
.ag-top{
  background:var(--card); border:1px solid var(--line); border-radius:8px;
  padding:11px 14px; display:flex; justify-content:space-between;
  align-items:center; gap:16px; flex-wrap:wrap;
}
.ag-top .l{display:flex; align-items:baseline; gap:10px; min-width:0;}
.ag-top .mark{
  font-family:var(--mono); font-size:13px; font-weight:700;
  letter-spacing:.16em; color:var(--ink);
}
.ag-top .sep{width:1px; height:14px; background:var(--line);}
.ag-top .prod{font-size:13px; color:var(--muted);}
.ag-top .r{display:flex; align-items:center; gap:0; flex-wrap:wrap;}
.ag-meta{display:flex; align-items:baseline; gap:6px; padding:0 12px; border-left:1px solid var(--line);}
.ag-meta:first-child{border-left:none; padding-left:0;}
.ag-meta .k{font-size:11px; color:var(--muted); letter-spacing:.02em;}
.ag-meta .v{font-family:var(--mono); font-size:12px; color:var(--ink);}
.ag-meta .v.s{font-family:var(--sans);}

/* ---------- 统计条（替代 KPI 卡片） ---------- */
.ag-stats{
  display:grid; grid-template-columns:repeat(auto-fit,minmax(112px,1fr));
  background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden;
}
.ag-stat{padding:9px 14px; border-left:1px solid var(--line2);}
.ag-stat:first-child{border-left:none;}
.ag-stat .n{font-family:var(--mono); font-size:19px; line-height:1.1; color:var(--ink);}
.ag-stat .n.hot{color:var(--c);}
.ag-stat .t{font-size:11px; color:var(--muted); margin-top:3px; letter-spacing:.02em;}

/* ---------- 小节标题 ---------- */
.ag-sec-head{
  display:flex; justify-content:space-between; align-items:baseline;
  margin:6px 0 6px; padding-bottom:5px; border-bottom:1px solid var(--line);
}
.ag-sec-head .t{font-size:11px; font-weight:600; letter-spacing:.08em; color:var(--muted); text-transform:uppercase;}
.ag-sec-head .m{font-family:var(--mono); font-size:11px; color:var(--slate);}

/* ---------- 状态：小圆点 + 文字 ---------- */
.ag-st{display:inline-flex; align-items:center; gap:5px; font-size:12px; color:var(--c); white-space:nowrap;}
.ag-st i{width:6px; height:6px; border-radius:50%; background:var(--c); display:inline-block;}

/* ---------- 表格 ---------- */
.ag-tbl{width:100%; border-collapse:collapse; font-size:12.5px;}
.ag-tbl th{
  text-align:left; font-size:11px; font-weight:600; letter-spacing:.04em; color:var(--muted);
  text-transform:uppercase; padding:6px 10px; border-bottom:1px solid var(--line);
  background:#FCFCFD;
}
.ag-tbl td{padding:7px 10px; border-bottom:1px solid var(--line2); color:var(--body); vertical-align:middle;}
.ag-tbl tr:last-child td{border-bottom:none;}
.ag-tbl .id{font-family:var(--mono); color:var(--ink); font-size:12px;}
.ag-tbl .num{font-family:var(--mono); color:var(--muted); text-align:right; width:64px;}
.ag-tbl .ty{color:var(--muted); font-size:11.5px;}
.ag-panel{background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden;}

/* ---------- 日志流 ---------- */
.ag-logbox{
  background:var(--card); border:1px solid var(--line); border-radius:8px;
  max-height:340px; overflow:auto;
}
.ag-logbox::-webkit-scrollbar{width:9px;}
.ag-logbox::-webkit-scrollbar-thumb{background:#D0D5DD; border-radius:9px;}
.ag-logrow{
  display:grid; grid-template-columns:58px 52px 1fr; gap:8px;
  padding:4px 12px; border-bottom:1px solid var(--line2);
  font-family:var(--mono); font-size:12px; line-height:1.55;
}
.ag-logrow:last-child{border-bottom:none;}
.ag-logrow:hover{background:#FCFCFD;}
.ag-logrow .tm{color:var(--slate);}
.ag-logrow .lv{color:var(--c); font-size:10.5px; letter-spacing:.04em; padding-top:2px;}
.ag-logrow .ms{color:var(--body); font-family:var(--sans); white-space:pre-wrap; word-break:break-word;}

/* ---------- 侧边栏元素 ---------- */
.ag-brand{padding:2px 0 10px; border-bottom:1px solid var(--line);}
.ag-brand .n{font-family:var(--mono); font-size:13px; font-weight:700; letter-spacing:.16em; color:var(--ink);}
.ag-brand .s{font-size:11.5px; color:var(--muted); margin-top:3px;}
.ag-brand .v{font-family:var(--mono); font-size:10.5px; color:var(--slate); margin-top:7px;}
.ag-label{
  font-size:11px; font-weight:600; letter-spacing:.08em; color:var(--muted);
  text-transform:uppercase; margin:14px 0 6px;
}
.ag-tags{display:flex; flex-wrap:wrap; gap:4px; margin:2px 0 4px;}
.ag-tags span{
  font-family:var(--mono); font-size:10.5px; color:var(--body);
  background:#F2F4F7; border:1px solid var(--line2); border-radius:4px; padding:1px 6px;
}
.ag-row{
  display:grid; grid-template-columns:1fr auto; gap:8px; align-items:center;
  padding:5px 2px; border-bottom:1px solid var(--line2); font-size:12px;
}
.ag-row:last-child{border-bottom:none;}
.ag-row .id{font-family:var(--mono); color:var(--ink);}
.ag-row .sub{font-family:var(--mono); font-size:10.5px; color:var(--slate); margin-left:6px;}
.ag-var{
  display:grid; grid-template-columns:74px 1fr; gap:10px; padding:6px 12px;
  border-bottom:1px solid var(--line2); font-family:var(--mono); font-size:11px;
}
.ag-var:last-child{border-bottom:none;}
.ag-var .k{color:var(--blue);}
.ag-var .v{color:var(--body); white-space:pre-wrap; word-break:break-word; font-family:var(--sans); font-size:12px;}
.ag-filerow{
  display:grid; grid-template-columns:1fr auto; gap:10px; padding:6px 12px;
  border-bottom:1px solid var(--line2); font-size:12px;
}
.ag-filerow:last-child{border-bottom:none;}
.ag-filerow .f{font-family:var(--mono); color:var(--ink);}
.ag-filerow .z{font-family:var(--mono); font-size:10.5px; color:var(--slate);}
.ag-note{font-size:12px; color:var(--muted); line-height:1.6; padding:2px 0 4px;}
.ag-empty-behind{background:var(--card); border:1px solid var(--line); border-radius:8px; padding:18px 20px;}
.ag-warn{
  background:#FFFAEB; border:1px solid #FEDF89; border-left:2px solid var(--amber);
  border-radius:6px; padding:8px 11px; font-size:12px; color:#93370D; line-height:1.55;
}
.ag-ex{padding:2px 0;}
.ag-ex b{display:block; font-size:12.5px; color:var(--ink); margin-bottom:1px;}
.ag-ex span{font-size:11.5px; color:var(--muted);}
.ag-foot{font-family:var(--mono); font-size:10.5px; color:var(--slate); padding:10px 0 2px;}
</style>
"""


def pill(status: str) -> str:
    label, color = STATUS_STYLE.get(status, (status or "未知", SLATE))
    return f'<span class="ag-st" style="--c:{color}"><i></i>{esc(label)}</span>'


def hero(trace_id: str, wf_name: str, wf_version, node_count, engine_ok=True, tick=""):
    """顶栏：产品标识 + 一行元信息。没有渐变、没有标语。"""
    dot = GREEN if engine_ok else RED
    cells = [
        f'<div class="ag-meta"><span class="v s" style="color:{dot}">'
        f'{"引擎运行中" if engine_ok else "引擎未启动"}</span></div>',
        f'<div class="ag-meta"><span class="k">工作流</span>'
        f'<span class="v">{esc(wf_name)}{f" v{esc(wf_version)}" if wf_version else ""}</span></div>',
    ]
    if node_count:
        cells.append(f'<div class="ag-meta"><span class="k">节点</span>'
                     f'<span class="v">{esc(node_count)}</span></div>')
    if trace_id:
        cells.append(f'<div class="ag-meta"><span class="k">trace</span>'
                     f'<span class="v">{esc(trace_id)}</span></div>')
    if tick:
        cells.append(f'<div class="ag-meta"><span class="k">刷新</span>'
                     f'<span class="v">{esc(tick)}</span></div>')
    return f"""
<div class="ag-top">
  <div class="l"><span class="mark">AEGIS</span><span class="sep"></span>
    <span class="prod">执行控制台</span></div>
  <div class="r">{''.join(cells)}</div>
</div>
"""


def kpis(items):
    """统计条。只有失败/待人工非零时才上色，其余保持墨色。"""
    cells = []
    for label, value, color in items:
        hot = ""
        if color in (RED, AMBER) and str(value) not in ("0", ""):
            hot = f' class="n hot" style="--c:{color}"'
        cells.append(f'<div class="ag-stat"><div class="n"{hot}>{esc(value)}</div>'
                     f'<div class="t">{esc(label)}</div></div>')
    return f'<div class="ag-stats">{"".join(cells)}</div>'


def panel_head(title, meta="", icon=""):
    meta_html = f'<div class="m">{esc(meta)}</div>' if meta else ""
    return (f'<div class="ag-sec-head"><div class="t">{esc(title)}</div>'
            f'{meta_html}</div>')


def node_table(spec, states, stats):
    rows = []
    for node in spec.nodes.values():
        st_ = states.get(node.id, "pending")
        mx, cnt = stats.get(node.id, (0, 0))
        rows.append(
            "<tr>"
            f'<td class="id">{esc(node.id)}</td>'
            f"<td>{esc(node.title)}</td>"
            f'<td class="ty">{esc(TYPE_LABEL.get(node.type, node.type))}</td>'
            f"<td>{pill(st_)}</td>"
            f'<td class="num">{mx if mx else "—"}</td>'
            f'<td class="num">{cnt}</td>'
            "</tr>"
        )
    return ('<div class="ag-panel"><table class="ag-tbl">'
            "<thead><tr><th>节点</th><th>名称</th><th>类型</th><th>状态</th>"
            "<th style='text-align:right'>重试</th><th style='text-align:right'>执行</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def log_list(rows):
    if not rows:
        return ('<div class="ag-logbox"><div class="ag-note" style="padding:12px">'
                "暂无日志</div></div>")
    items = []
    for ts, level, msg in rows:
        color = LEVEL_COLOR.get(level, MUTED)
        items.append(
            f'<div class="ag-logrow"><span class="tm">{esc(ts)}</span>'
            f'<span class="lv" style="--c:{color}">{esc(level)}</span>'
            f'<span class="ms">{esc(clean(msg))}</span></div>'
        )
    return f'<div class="ag-logbox">{"".join(items)}</div>'


def brand():
    return ('<div class="ag-brand"><div class="n">AEGIS</div>'
            '<div class="s">多 Agent 协同与可信交付</div>'
            '<div class="v">blackboard-driven / yaml-defined</div></div>')


def kv(pairs):
    return '<div class="ag-tags">' + "".join(
        f"<span>{esc(k)} {esc(v)}</span>" for k, v in pairs) + "</div>"


def trace_row(trace_id, status, when):
    return (f'<div class="ag-row"><span><span class="id">{esc(trace_id)}</span>'
            f'<span class="sub">{esc(when)}</span></span>{pill(status)}</div>')


def empty_state():
    return """
<div class="ag-empty-behind">
  <div style="font-size:13px;color:#101828;font-weight:600;margin-bottom:4px">还没有正在执行的追踪</div>
  <div class="ag-note">在左侧选择工作流、输入需求并启动。引擎在任务黑板上推进这条 trace，本页每 3 秒刷新一次。</div>
  <table class="ag-tbl" style="margin-top:10px">
    <thead><tr><th style="width:64px">阶段</th><th>做什么</th><th>由谁决定</th></tr></thead>
    <tbody>
      <tr><td class="id">分诊</td><td>判断是代码任务还是闲聊，决定走哪条分支</td><td class="ty">router.yaml + routes</td></tr>
      <tr><td class="id">生成</td><td>产出依赖、断言与多文件代码规格</td><td class="ty">coder.yaml</td></tr>
      <tr><td class="id">验收</td><td>装依赖、真实执行、按物理断言裁决</td><td class="ty">tool: execute_spec</td></tr>
      <tr><td class="id">修复</td><td>按完整失败链全量重写，预算内重试</td><td class="ty">fixer.yaml + max_retries</td></tr>
      <tr><td class="id">熔断</td><td>预算耗尽转人工，建议下发后预算重置</td><td class="ty">human + on_resume</td></tr>
    </tbody>
  </table>
</div>
"""


def warn_card(text):
    return f'<div class="ag-warn">{esc(text)}</div>'


def footer(text):
    return f'<div class="ag-foot">{esc(text)}</div>'


def var_list(pairs):
    if not pairs:
        return ('<div class="ag-panel"><div class="ag-note" style="padding:10px 12px">'
                "该 trace 暂无工作记忆变量</div></div>")
    rows = []
    for k, v in pairs:
        v = str(v or "")
        short = v if len(v) <= 140 else v[:140] + " …"
        rows.append(f'<div class="ag-var"><span class="k">{esc(k)}</span>'
                    f'<span class="v">{esc(short)}</span></div>')
    return f'<div class="ag-panel">{"".join(rows)}</div>'


def worker_list(items):
    """调度器：Worker 画像。items: [(worker 名, 能力集, 认领数)]"""
    if not items:
        return ('<div class="ag-panel"><div class="ag-note" style="padding:10px 12px">'
                "引擎尚未启动 Worker</div></div>")
    rows = []
    for name, caps, claimed in items:
        cap_txt = "全能力" if list(caps) == ["*"] else " ".join(caps)
        rows.append(
            '<div class="ag-row"><span><span class="id">%s</span>'
            '<span class="sub">%s</span></span>'
            '<span style="font-size:11px;color:#667085">本进程认领 %s</span></div>'
            % (esc(name), esc(cap_txt), esc(claimed))
        )
    return '<div class="ag-panel">%s</div>' % "".join(rows)


def sched_table(buckets):
    """调度器：待执行任务按能力分类。buckets: [{capability, pending, top_priority, wait_s}]"""
    if not buckets:
        return ('<div class="ag-panel"><div class="ag-note" style="padding:10px 12px">'
                "当前没有待执行任务</div></div>")
    rows = []
    for b in buckets:
        rows.append(
            '<tr><td class="id">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td><td class="num">%ss</td></tr>'
            % (esc(b["capability"]), esc(b["pending"]),
               esc(b["top_priority"]), esc(b["wait_s"]))
        )
    return ('<div class="ag-panel"><table class="ag-tbl">'
            '<thead><tr><th>能力</th><th style="text-align:right">积压</th>'
            '<th style="text-align:right">优先级</th>'
            '<th style="text-align:right">最长等待</th></tr></thead>'
            '<tbody>%s</tbody></table></div>' % "".join(rows))


def file_rows(files, sizes):
    if not files:
        return ('<div class="ag-panel"><div class="ag-note" style="padding:10px 12px">'
                "工作区暂无产出文件</div></div>")
    rows = []
    for name in files:
        rows.append(f'<div class="ag-filerow"><span class="f">{esc(name)}</span>'
                    f'<span class="z">{sizes.get(name, 0):,} B</span></div>')
    return f'<div class="ag-panel">{"".join(rows)}</div>'