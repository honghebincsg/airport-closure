#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KJFK 关闭区域图 v2
输入：AI 结构化结果 ai_parse.json（与正则交叉核对后的权威版本）
      几何：X-Plane apt.dat 滑行网络 + OurAirports 跑道端点
输出：PNG + 控制台匹配状态表
"""
import os, re, csv, json, math
from datetime import datetime, timedelta
import networkx as nx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon, Rectangle, Circle
import matplotlib.patheffects as pe

APT = "/tmp/kjfk_pack/KJFK_Scenery_Pack/Earth nav data/apt.dat"
RWY_CSV = "/tmp/runways.csv"
AI = "/home/user/Doubao/chats/38454828072541442/ai_parse.json"
OUT = "/home/user/Doubao/chats/38454828072541442/KJFK_关闭区域示意图.png"

# ---------- 1. 几何 ----------
nodes, edges = {}, []
for line in open(APT, encoding="utf-8", errors="ignore"):
    f = line.rstrip().split()
    if line.startswith("1201"):
        nodes[int(f[4])] = (float(f[1]), float(f[2]))
    elif line.startswith("1202"):
        edges.append((int(f[1]), int(f[2]), " ".join(f[5:])))

runways = []
for r in csv.DictReader(open(RWY_CSV, encoding="utf-8")):
    if r["airport_ident"] == "KJFK":
        runways.append(dict(a=(r["le_ident"], float(r["le_latitude_deg"]), float(r["le_longitude_deg"])),
                            b=(r["he_ident"], float(r["he_latitude_deg"]), float(r["he_longitude_deg"])),
                            width_m=float(r["width_ft"]) * 0.3048))

LAT0 = sum(n[0] for n in nodes.values()) / len(nodes)
LON0 = sum(n[1] for n in nodes.values()) / len(nodes)
def xy(lat, lon):
    return ((lon - LON0) * math.cos(math.radians(LAT0)) * 111320.0, (lat - LAT0) * 110540.0)

NET_NAMES = {nm for a, b, nm in edges}

# ---------- 2. AI 结构化结果 ----------
objs = json.load(open(AI))["objects"]
AT = os.environ.get("AT")
OUT = os.environ.get("OUT", OUT)
def _dt(s):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2})(\d{2})", s or "")
    if not m: return None
    mo, da, yr, hh, mi = map(int, m.groups())
    return datetime(yr, mo, da, hh, mi)          # UTC
KEEP_AT = None
if AT:
    KEEP_AT = datetime.strptime(AT, "%Y-%m-%d %H:%M") + timedelta(hours=5)  # EST→UTC
# 时间窗从原始通告补齐
tt = {r["no"]: (r["start"], r["end"]) for r in json.load(open("/home/user/kjfk_notam_raw.json"))["rows"]}
for o in objs:
    o["start"], o["end"] = tt.get(o["no"], ("", ""))
if KEEP_AT:
    objs = [o for o in objs
            if _dt(o["start"]) and _dt(o["end"])
            and _dt(o["start"]) <= KEEP_AT < _dt(o["end"])]
rwy_cl = [o for o in objs if o["kind"] == "RWY"]
twy_cl = [o for o in objs if o["kind"] == "TWY"]

def nodes_of(n):
    return {a for a, b, nm in edges if nm == n} | {b for a, b, nm in edges if nm == n}

def seg_dist(p, s1, s2):
    dx, dy = s2[0] - s1[0], s2[1] - s1[1]
    L2 = dx * dx + dy * dy
    if L2 == 0: return math.hypot(p[0] - s1[0], p[1] - s1[1])
    t = max(0, min(1, ((p[0] - s1[0]) * dx + (p[1] - s1[1]) * dy) / L2))
    return math.hypot(p[0] - (s1[0] + t * dx), p[1] - (s1[1] + t * dy))

def boundary_nodes(twy, bnd):
    if not bnd: return set()
    shared = nodes_of(twy) & nodes_of(bnd)
    if shared: return shared
    segs = [(xy(*nodes[a]), xy(*nodes[b])) for a, b, nm in edges if nm == bnd and a in nodes and b in nodes]
    if not segs: return set()
    return {n for n in nodes_of(twy) if any(seg_dist(xy(*nodes[n]), s1, s2) <= 150 for s1, s2 in segs)}

segments, unmatched = [], []
for c in twy_cl:
    tag = f"TWY {c['name']}" + (f"（{c['b1']}–{c['b2']}）" if c["b1"] else "")
    # 要素缺失判定
    if c["name"] not in NET_NAMES:
        unmatched.append((tag, c["no"], "滑行网络里无此滑行道"))
        continue
    apron = [t for t in (c.get("b1_type"), c.get("b2_type")) if t in ("TERMINAL", "RAMP", "APRON")]
    if True:
        sub = nx.Graph([(a, b) for a, b, nm in edges if nm == c["name"]])
        if apron:
            # 机坪端截取：从另一侧边界走到该滑行道伸出机坪的端点
            other = c["b2"] if c["b1_type"] in ("TERMINAL", "RAMP", "APRON") else c["b1"]
            S = boundary_nodes(c["name"], other)
            if S:
                ends = [n for n in sub.nodes if sub.degree[n] == 1 and n not in S]
                path = None
                for a1 in S:
                    for a2 in ends:
                        try:
                            path = nx.shortest_path(sub, a1, a2)
                            break
                        except Exception: pass
                    if path: break
                if path:
                    segments.append(dict(label=tag, pts=[xy(*nodes[n]) for n in path],
                                         no=c["no"], start=c["start"], end=c["end"], apron=True))
                    print(f"  [{c['no']}] {tag} 机坪端截取 -> OK {len(path)} 节点")
                    continue
            unmatched.append((tag, c["no"], "机坪侧无可用端点"))
            print(f"  [{c['no']}] {tag} 机坪端截取 -> 未匹配")
            continue
        if c["b1"] is None:
            comp = max(nx.connected_components(sub), key=len)
            tree = sub.subgraph(comp)
            ends = [n for n in tree.nodes if tree.degree[n] == 1]
            path = nx.shortest_path(tree, ends[0], ends[1]) if len(ends) > 1 else list(tree.nodes)
            segments.append(dict(label=tag, pts=[xy(*nodes[n]) for n in path], no=c["no"], start=c["start"], end=c["end"]))
            print(f"  [{c['no']}] {tag} 整条 -> OK {len(path)} 节点")
            continue
        S1, S2 = boundary_nodes(c["name"], c["b1"]), boundary_nodes(c["name"], c["b2"])
        path = None
        for a1 in S1:
            for a2 in S2:
                if a1 != a2:
                    try:
                        path = nx.shortest_path(sub, a1, a2); break
                    except Exception: pass
            if path: break
        if path:
            segments.append(dict(label=tag, pts=[xy(*nodes[n]) for n in path], no=c["no"], start=c["start"], end=c["end"]))
            print(f"  [{c['no']}] {tag} 边界 {len(S1)}/{len(S2)} -> OK")
        else:
            why = "边界要素不在网络" if not (S1 and S2) else "两边界无连通路径"
            unmatched.append((tag, c["no"], why))
            print(f"  [{c['no']}] {tag} 边界 {len(S1)}/{len(S2)} -> 未匹配（{why}）")

print(f"\n上图 {len(segments)} 段滑行道 + {len(rwy_cl)} 条跑道；未上图 {len(unmatched)} 条")

# ---------- 3. 绘图 ----------
plt.rcParams["font.family"] = ["Noto Sans CJK JP", "Noto Sans CJK SC", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

fig = plt.figure(figsize=(16, 9), dpi=200, facecolor="#f7f8fa")
ax = fig.add_axes([0.015, 0.04, 0.685, 0.845]); ax.set_facecolor("#f7f8fa")
ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
for s in ax.spines.values(): s.set_visible(False)

for a, b, nm in edges:
    if a in nodes and b in nodes:
        x1, y1 = xy(*nodes[a]); x2, y2 = xy(*nodes[b])
        ax.plot([x1, x2], [y1, y2], color="#ccd3db", lw=5.2, solid_capstyle="round", zorder=1)
        ax.plot([x1, x2], [y1, y2], color="#e9eef3", lw=2.6, solid_capstyle="round", zorder=2)

def rw_poly(rw):
    p1 = xy(rw["a"][1], rw["a"][2]); p2 = xy(rw["b"][1], rw["b"][2])
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    d = math.hypot(dx, dy) or 1
    px, py = -dy / d, dx / d
    hw = rw["width_m"] / 2
    return ([(p1[0] + px * hw, p1[1] + py * hw), (p2[0] + px * hw, p2[1] + py * hw),
             (p2[0] - px * hw, p2[1] - py * hw), (p1[0] - px * hw, p1[1] - py * hw)], p1, p2)

for rw in runways:
    poly, p1, p2 = rw_poly(rw)
    ax.add_patch(Polygon(poly, closed=True, facecolor="#39424c", edgecolor="#232a32", lw=0.8, zorder=3))
    ax.plot([p1[0], p2[0]], [p1[1], p2[1]], color="#f2f4f7", lw=0.7, ls=(0, (7, 7)), zorder=4, alpha=0.7)
    for desig, pt, other in ((rw["a"][0], p1, p2), (rw["b"][0], p2, p1)):
        vx, vy = pt[0] - other[0], pt[1] - other[1]
        dd = math.hypot(vx, vy) or 1
        ax.text(pt[0] - vx / dd * 90, pt[1] - vy / dd * 90, desig, color="#f7f8fa", fontsize=8,
                fontweight="bold", ha="center", va="center", zorder=6,
                path_effects=[pe.withStroke(linewidth=1.6, foreground="#2b323a")])

PLACED = []
def place_badge(x0, y0, idx, min_d=175):
    cand = [(x0, y0)]
    for r in (min_d, min_d * 1.55, min_d * 2.1):
        for k in range(12):
            ang = math.radians(k * 30)
            cand.append((x0 + r * math.cos(ang), y0 + r * math.sin(ang) * 0.75))
    best = cand[0]
    for c in cand:
        if all(math.hypot(c[0] - p[0], c[1] - p[1]) >= min_d for p in PLACED):
            best = c; break
    PLACED.append(best)
    if math.hypot(best[0] - x0, best[1] - y0) > 20:
        ax.plot([x0 * 0.5 + best[0] * 0.5, best[0]], [y0 * 0.5 + best[1] * 0.5, best[1]],
                color="#b3202c", lw=0.8, alpha=0.55, zorder=8)
    ax.add_patch(Circle(best, 72, facecolor="#b3202c", edgecolor="white", lw=2.0, zorder=9))
    ax.text(best[0], best[1], str(idx), color="white", fontsize=11, fontweight="bold",
            ha="center", va="center", zorder=10)

numbered = []
cnt = 0
for c in rwy_cl:
    for rw in runways:
        if rw["a"][0] in c["name"].split("/") and rw["b"][0] in c["name"].split("/"):
            poly, p1, p2 = rw_poly(rw)
            ax.add_patch(Polygon(poly, closed=True, facecolor="#e63946", edgecolor="#c1212e",
                                 lw=2.0, alpha=0.28, hatch="////", zorder=5))
            ax.add_patch(Polygon(poly, closed=True, facecolor="none", edgecolor="#c1212e", lw=2.2, zorder=5))
            mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
            q = (p1[0] * 0.72 + p2[0] * 0.28, p1[1] * 0.72 + p2[1] * 0.28)
            ax.text(q[0], q[1], f"CLSD {c['name']}", color="#ffffff", fontsize=8.5, fontweight="bold",
                    ha="center", va="center", zorder=6,
                    rotation=math.degrees(math.atan2(p2[1] - p1[1], p2[0] - p1[0])),
                    bbox=dict(boxstyle="round,pad=0.28", fc="#b3202c", ec="none", alpha=0.95))
            cnt += 1
            numbered.append((cnt, f"RWY {c['name']}", c["no"], c["start"], c["end"]))
            nx_, ny_ = -(p2[1] - p1[1]), (p2[0] - p1[0])
            L = math.hypot(nx_, ny_) or 1
            place_badge(mid[0] + nx_ / L * 210, mid[1] + ny_ / L * 210, cnt)

for s in segments:
    xs = [p[0] for p in s["pts"]]; ys = [p[1] for p in s["pts"]]
    ax.plot(xs, ys, color="white", lw=9.5, solid_capstyle="round", zorder=7)
    ax.plot(xs, ys, color="#e63946", lw=5.4, solid_capstyle="round", zorder=8)
    cnt += 1
    numbered.append((cnt, s["label"], s["no"], s["start"], s["end"]))
    seglen = math.hypot(xs[-1] - xs[0], ys[-1] - ys[0])
    ax_, ay_ = xs[len(xs) // 2], ys[len(ys) // 2]
    if True:
        nx_, ny_ = (ys[-1] - ys[0]), -(xs[-1] - xs[0])
        L = math.hypot(nx_, ny_) or 1
        ax_, ay_ = ax_ + nx_ / L * 130, ay_ + ny_ / L * 130
    place_badge(ax_, ay_, cnt)

LABEL_OK = set("ABCDEFGHJKLMNPQRSTUVWYZ")
done = set()
for a, b, nm in edges:
    if nm in LABEL_OK and nm not in done and a in nodes and b in nodes:
        x1, y1 = xy(*nodes[a]); x2, y2 = xy(*nodes[b])
        ax.text((x1 + x2) / 2, (y1 + y2) / 2, nm, fontsize=7.6, color="#48535f", ha="center",
                va="center", zorder=11,
                path_effects=[pe.withStroke(linewidth=2.2, foreground="#f7f8fa")])
        done.add(nm)

x0, x1 = ax.get_xlim(); y0, y1 = ax.get_ylim()
span = x1 - x0; bar = 1000
sx, sy = x0 + span * 0.03, y0 + span * 0.03
ax.plot([sx, sx + bar], [sy, sy], color="#2f3b45", lw=3, zorder=12)
ax.plot([sx, sx], [sy - 15, sy + 15], color="#2f3b45", lw=1.6, zorder=12)
ax.plot([sx + bar, sx + bar], [sy - 15, sy + 15], color="#2f3b45", lw=1.6, zorder=12)
ax.plot([sx + bar / 2, sx + bar / 2], [sy - 9, sy + 9], color="#2f3b45", lw=1.4, zorder=12)
ax.text(sx + bar / 2, sy + 24, "1 km", fontsize=8.5, color="#2f3b45", ha="center")

fig.text(0.015, 0.962, "KJFK 约翰·肯尼迪国际机场  关闭区域图",
         fontsize=20, fontweight="bold", color="#1c242e", va="top")
fig.text(0.015, 0.918,
         ("时刻快照 " + AT + " EST　·　" if AT else "通告快照 2026-11-19 15:01Z　·　") +
         "纽约 EST = UTC−5　·　"
         f"AI 解析 10 条关闭 → 上图 {len(numbered)} 处，未定位 {len(unmatched)} 条",
         fontsize=10, color="#6b7684", va="top")

# ---------- 右侧清单 ----------
axp = fig.add_axes([0.715, 0.04, 0.27, 0.845]); axp.axis("off")
axp.add_patch(Rectangle((0, 0), 1, 1, transform=axp.transAxes, facecolor="white",
                        edgecolor="#e3e8ee", lw=1, zorder=0))

def est(s, with_year=False):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4}) (\d{2})(\d{2})", s or "")
    if not m: return ""
    mo, da, yr, hh, mi = map(int, m.groups())
    tot = hh * 60 + mi - 300; day = da
    if tot < 0: tot += 1440; day -= 1
    core = f"{tot // 60:02d}:{tot % 60:02d}"
    return f"{yr}-{mo:02d}-{day:02d} {core}" if with_year else f"{mo:02d}-{day:02d} {core}"

def span_days(st, en):
    a = re.match(r"(\d{2})/(\d{2})/(\d{4}) (\d{2})(\d{2})", st or "")
    b = re.match(r"(\d{2})/(\d{2})/(\d{4}) (\d{2})(\d{2})", en or "")
    if not (a and b): return 0
    ta = datetime(*[int(x) for x in (a.group(3), a.group(1), a.group(2), a.group(4), a.group(5))])
    tb = datetime(*[int(x) for x in (b.group(3), b.group(1), b.group(2), b.group(4), b.group(5))])
    return (tb - ta).total_seconds() / 86400

axp.text(0.06, 0.945, "已上图", fontsize=13, fontweight="bold", color="#1c242e")
y = 0.885
for idx, name, no, st, en in numbered:
    axp.add_patch(Circle((0.085, y), 0.020, transform=axp.transAxes, facecolor="#b3202c", edgecolor="none"))
    axp.text(0.085, y, str(idx), transform=axp.transAxes, color="white", fontsize=9,
             fontweight="bold", ha="center", va="center")
    axp.text(0.135, y + 0.007, f"{name} 关闭", transform=axp.transAxes, fontsize=9.6, color="#1c242e")
    if span_days(st, en) > 3:
        line2 = f"{no} · {est(st, True)} – {est(en, True)}"
    else:
        line2 = f"{no} · EST {est(st)}–{est(en)}"
    axp.text(0.135, y - 0.021, line2, transform=axp.transAxes,
             fontsize=7.2, color="#77828f")
    y -= 0.066

# 未上图区块
y -= 0.012
axp.plot([0.06, 0.94], [y, y], transform=axp.transAxes, color="#e3e8ee", lw=1)
y -= 0.032
axp.text(0.06, y, f"未上图（{len(unmatched)} 条，要素缺失）", fontsize=10,
         fontweight="bold", color="#8a6d1f")
y -= 0.030
for tag, no, why in unmatched:
    axp.add_patch(Rectangle((0.062, y - 0.008), 0.014, 0.014, transform=axp.transAxes,
                            facecolor="#e0b23a", edgecolor="none"))
    axp.text(0.100, y, tag, transform=axp.transAxes, fontsize=8.6, color="#6a5a2e")
    axp.text(0.100, y - 0.021, f"{no} · {why}", transform=axp.transAxes, fontsize=7.2, color="#9a916f")
    y -= 0.048

axp.text(0.06, max(y - 0.045, 0.16),
         "标注口径\n"
         "· 红色实线 = 滑行道关闭段（按 BTN X AND Y 截取）\n"
         "· 红色斜线 = 整条跑道关闭\n"
         "· 黄色项 = 要素在图上/网络里不存在，不硬标\n"
         "· 圆圈数字对应上方清单",
         transform=axp.transAxes, fontsize=7.8, color="#5c6875", va="top", linespacing=1.9)


fig.text(0.015, 0.012,
         "数据：FAA NOTAM Search 2026-11-19 15:01Z · X-Plane Scenery Gateway · OurAirports　|　"
         "解析：AI 结构化 + 正则交叉核对（正则 9 / AI 10，差异为并列句 TWY B）",
         fontsize=7.2, color="#9aa4af")

fig.savefig(OUT, facecolor=fig.get_facecolor())
print("\nsaved", OUT)
