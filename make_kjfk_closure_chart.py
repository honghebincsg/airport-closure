#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KJFK 关闭区域示意图
跑道几何：OurAirports（FAA 派生）runways.csv
滑行道几何：X-Plane Scenery Gateway apt.dat 滑行网络
关闭关系：FAA NOTAM Search 快照
"""

import re
import csv
import json
import math
import networkx as nx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon, Rectangle, Circle
import matplotlib.patheffects as pe

APT = "/tmp/kjfk_pack/KJFK_Scenery_Pack/Earth nav data/apt.dat"
RWY_CSV = "/tmp/runways.csv"
NOTAM = "/home/user/kjfk_notam_raw.json"
OUT = "/home/user/Doubao/chats/38454828072541442/KJFK_关闭区域示意图.png"

# ---------- 1. 几何 ----------
nodes, edges = {}, []
for line in open(APT, encoding="utf-8", errors="ignore"):
    line = line.rstrip("\r\n")
    if line.startswith("1201"):
        f = line.split()
        nodes[int(f[4])] = (float(f[1]), float(f[2]))
    elif line.startswith("1202"):
        f = line.split()
        edges.append((int(f[1]), int(f[2]), " ".join(f[5:]) if len(f) > 5 else ""))

runways = []
for r in csv.DictReader(open(RWY_CSV, encoding="utf-8")):
    if r["airport_ident"] != "KJFK":
        continue
    runways.append(dict(
        a=(r["le_ident"], float(r["le_latitude_deg"]), float(r["le_longitude_deg"])),
        b=(r["he_ident"], float(r["he_latitude_deg"]), float(r["he_longitude_deg"])),
        width_m=float(r["width_ft"]) * 0.3048))

LAT0 = sum(n[0] for n in nodes.values()) / len(nodes)
LON0 = sum(n[1] for n in nodes.values()) / len(nodes)

def xy(lat, lon):
    return ((lon - LON0) * math.cos(math.radians(LAT0)) * 111320.0,
            (lat - LAT0) * 110540.0)

# ---------- 2. NOTAM ----------
rows = json.load(open(NOTAM))["rows"]

def clean(t):
    return re.sub(r"(TWY|RWY)\s*[A-Z0-9]+\s*RSTRCTN>.*$", "", t).strip()

twy_cl, rwy_cl = [], []
for r in rows:
    q, t = r["qcode"], clean(r["text"])
    if q.startswith("QMR") and "CLSD" in t:
        m = re.search(r"RWY\s*(\d{2}[LCR]?/\d{2}[LCR]?)", t)
        if m:
            rwy_cl.append(dict(no=r["no"], rwy=m.group(1), start=r["start"], end=r["end"]))
    elif q.startswith("QMX") and "CLSD" in t:
        for seg in re.finditer(
                r"TWY\s+([A-Z0-9]+)\s+BTN\s+(?:TWY|RWY)\s+([A-Z0-9/]+)\s+AND\s+(?:TWY|RWY)\s+([A-Z0-9/]+)", t):
            twy_cl.append(dict(no=r["no"], twy=seg.group(1), b1=seg.group(2),
                               b2=seg.group(3), start=r["start"], end=r["end"]))
        m = re.fullmatch(r"TWY\s+([A-Z0-9]+)\s+CLSD", t)
        if m:
            twy_cl.append(dict(no=r["no"], twy=m.group(1), b1=None, b2=None,
                               start=r["start"], end=r["end"]))

# ---------- 3. 关闭段 ----------
def coords(path):
    return [xy(*nodes[n]) for n in path]

def nodes_of(name):
    return {n for a, b, nm in edges if nm == name for n in (a, b)}

def seg_dist(p, s1, s2):
    ax, ay = p; bx, by = s1; cx, cy = s2
    dx, dy = cx - bx, cy - by
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(ax - bx, ay - by)
    t = max(0, min(1, ((ax - bx) * dx + (ay - by) * dy) / L2))
    return math.hypot(ax - (bx + t * dx), ay - (by + t * dy))

def boundary_nodes(twy, bnd):
    if not bnd:
        return set()
    shared = nodes_of(twy) & nodes_of(bnd)
    if shared:
        return shared
    segs = [(xy(*nodes[a]), xy(*nodes[b])) for a, b, nm in edges
            if nm == bnd and a in nodes and b in nodes]
    out = set()
    for n in nodes_of(twy):
        p = xy(*nodes[n])
        if any(seg_dist(p, s1, s2) <= 150 for s1, s2 in segs):
            out.add(n)
    return out

segments = []
for c in twy_cl:
    sub = nx.Graph([(a, b) for a, b, nm in edges if nm == c["twy"]])
    if sub.number_of_nodes() == 0:
        print(f"[{c['no']}] TWY {c['twy']}: 网络里无此滑行道，跳过")
        continue
    if c["b1"] is None:
        comp = max(nx.connected_components(sub), key=len)
        tree = sub.subgraph(comp)
        ends = [n for n in tree.nodes if tree.degree[n] == 1]
        path = nx.shortest_path(tree, ends[0], ends[1]) if len(ends) >= 2 else list(tree.nodes)
        segments.append(dict(label=f"TWY {c['twy']}", pts=coords(path), no=c["no"],
                             start=c["start"], end=c["end"]))
        print(f"[{c['no']}] TWY {c['twy']} 整条关闭 -> OK {len(path)} 节点")
        continue
    S1, S2 = boundary_nodes(c["twy"], c["b1"]), boundary_nodes(c["twy"], c["b2"])
    path = None
    for s1 in S1:
        for s2 in S2:
            if s1 != s2:
                try:
                    path = nx.shortest_path(sub, s1, s2); break
                except Exception:
                    pass
        if path:
            break
    print(f"[{c['no']}] TWY {c['twy']} {c['b1']}–{c['b2']}: 边界 {len(S1)}/{len(S2)} "
          f"-> {'OK' if path else '未匹配'}")
    if path:
        segments.append(dict(label=f"TWY {c['twy']}（{c['b1']}–{c['b2']}）",
                             pts=coords(path), no=c["no"], start=c["start"], end=c["end"]))

PLACED = []
def place_badge(ax, ax_, ay_, idx, min_d=175):
    """带避让的编号徽标：围绕锚点找空位"""
    import math as _m
    cand = [(ax_, ay_)]
    for r in (min_d, min_d * 1.55, min_d * 2.1):
        for k in range(12):
            ang = _m.radians(k * 30)
            cand.append((ax_ + r * _m.cos(ang), ay_ + r * _m.sin(ang) * 0.75))
    best = cand[0]
    for c in cand:
        if all(_m.hypot(c[0] - p[0], c[1] - p[1]) >= min_d for p in PLACED):
            best = c; break
    PLACED.append(best)
    if _m.hypot(best[0] - ax_, best[1] - ay_) > 20:
        ax.plot([ax_ * 0.5 + best[0] * 0.5, best[0]], [ay_ * 0.5 + best[1] * 0.5, best[1]],
                color="#b3202c", lw=0.8, alpha=0.55, zorder=8)
    ax.add_patch(Circle(best, 72, facecolor="#b3202c", edgecolor="white", lw=2.0, zorder=9))
    ax.text(best[0], best[1], str(idx), color="white", fontsize=11, fontweight="bold",
            ha="center", va="center", zorder=10)

# ---------- 4. 绘图 ----------
plt.rcParams["font.family"] = ["Noto Sans CJK JP", "Noto Sans CJK SC", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

fig = plt.figure(figsize=(16, 9), dpi=200, facecolor="#f7f8fa")
ax = fig.add_axes([0.015, 0.04, 0.70, 0.845]); ax.set_facecolor("#f7f8fa")
ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
for s in ax.spines.values():
    s.set_visible(False)

for a, b, nm in edges:
    if a in nodes and b in nodes:
        x1, y1 = xy(*nodes[a]); x2, y2 = xy(*nodes[b])
        ax.plot([x1, x2], [y1, y2], color="#ccd3db", lw=5.2, solid_capstyle="round", zorder=1)
        ax.plot([x1, x2], [y1, y2], color="#e9eef3", lw=2.6, solid_capstyle="round", zorder=2)

def rw_poly(rw, pad=0):
    p1 = xy(rw["a"][1], rw["a"][2]); p2 = xy(rw["b"][1], rw["b"][2])
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    d = math.hypot(dx, dy)
    ux, uy = dx / d, dy / d; px, py = -uy, ux
    hw = rw["width_m"] / 2 + pad
    return ([(p1[0] + px * hw, p1[1] + py * hw), (p2[0] + px * hw, p2[1] + py * hw),
             (p2[0] - px * hw, p2[1] - py * hw), (p1[0] - px * hw, p1[1] - py * hw)], p1, p2)

for rw in runways:
    poly, p1, p2 = rw_poly(rw)
    ax.add_patch(Polygon(poly, closed=True, facecolor="#39424c", edgecolor="#232a32",
                         lw=0.8, zorder=3))
    ax.plot([p1[0], p2[0]], [p1[1], p2[1]], color="#f2f4f7", lw=0.7, ls=(0, (7, 7)),
            zorder=4, alpha=0.7)
    for desig, pt, other in ((rw["a"][0], p1, p2), (rw["b"][0], p2, p1)):
        vx, vy = pt[0] - other[0], pt[1] - other[1]
        dd = math.hypot(vx, vy)
        ax.text(pt[0] - vx / dd * 90, pt[1] - vy / dd * 90, desig, color="#f7f8fa",
                fontsize=8, fontweight="bold", ha="center", va="center", zorder=6,
                path_effects=[pe.withStroke(linewidth=1.6, foreground="#2b323a")])

numbered = []   # (idx, 名称, 编号, 开始, 结束, 类别)
cnt = 0
for c in rwy_cl:
    for rw in runways:
        if rw["a"][0] in c["rwy"].split("/") and rw["b"][0] in c["rwy"].split("/"):
            poly, p1, p2 = rw_poly(rw)
            ax.add_patch(Polygon(poly, closed=True, facecolor="#e63946", edgecolor="#c1212e",
                                 lw=2.0, alpha=0.28, hatch="////", zorder=5))
            ax.add_patch(Polygon(poly, closed=True, facecolor="none", edgecolor="#c1212e",
                                 lw=2.2, zorder=5))
            mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
            q = (p1[0] * 0.72 + p2[0] * 0.28, p1[1] * 0.72 + p2[1] * 0.28)
            ang = math.degrees(math.atan2(p2[1]-p1[1], p2[0]-p1[0]))
            ax.text(q[0], q[1], f"CLSD {c['rwy']}", color="#ffffff", fontsize=8.5,
                    fontweight="bold", ha="center", va="center", rotation=ang, zorder=6,
                    bbox=dict(boxstyle="round,pad=0.28", fc="#b3202c", ec="none", alpha=0.95))
            cnt += 1
            numbered.append((cnt, f"RWY {c['rwy']}", c["no"], c["start"], c["end"], "跑道"))
            nx_, ny_ = -(p2[1] - p1[1]), (p2[0] - p1[0])
            L = math.hypot(nx_, ny_) or 1
            place_badge(ax, mid[0] + nx_ / L * 210, mid[1] + ny_ / L * 210, cnt)

for s in segments:
    xs = [p[0] for p in s["pts"]]; ys = [p[1] for p in s["pts"]]
    ax.plot(xs, ys, color="white", lw=9.5, solid_capstyle="round", zorder=7)
    ax.plot(xs, ys, color="#e63946", lw=5.4, solid_capstyle="round", zorder=8)
    i = len(xs) // 2
    cnt += 1
    numbered.append((cnt, s["label"], s["no"], s["start"], s["end"], "滑行道"))
    # 标记放在线段外侧，避免压住红线
    place_badge(ax, xs[i], ys[i], cnt)

import string
LABEL_OK = set("ABCDEFGHJKLMNPQRSTUVWYZ") | {"Q4"}
done = set()
for a, b, nm in edges:
    if nm in LABEL_OK and nm not in done and a in nodes and b in nodes:
        x1, y1 = xy(*nodes[a]); x2, y2 = xy(*nodes[b])
        ax.text((x1 + x2) / 2, (y1 + y2) / 2, nm, fontsize=7.6, color="#48535f",
                ha="center", va="center", zorder=11,
                path_effects=[pe.withStroke(linewidth=2.2, foreground="#f7f8fa")])
        done.add(nm)

x0, x1 = ax.get_xlim(); y0, y1 = ax.get_ylim()
span = x1 - x0
bar = 1000
sx, sy = x0 + span * 0.03, y0 + span * 0.03
ax.plot([sx, sx + bar], [sy, sy], color="#2f3b45", lw=3, zorder=12)
ax.plot([sx, sx], [sy - 15, sy + 15], color="#2f3b45", lw=1.6, zorder=12)
ax.plot([sx + bar, sx + bar], [sy - 15, sy + 15], color="#2f3b45", lw=1.6, zorder=12)
ax.plot([sx + bar / 2, sx + bar / 2], [sy - 9, sy + 9], color="#2f3b45", lw=1.4, zorder=12)
ax.text(sx + bar / 2, sy + 24, "1 km", fontsize=8.5, color="#2f3b45", ha="center")

fig.text(0.015, 0.962, "KJFK 约翰·肯尼迪国际机场  关闭区域示意图",
         fontsize=20, fontweight="bold", color="#1c242e", va="top")
fig.text(0.015, 0.918, "NOTAM 快照 2026-11-19 15:01Z　·　纽约当地 EST = UTC−5",
         fontsize=10, color="#6b7684", va="top")

# ---------- 右侧清单 ----------
axp = fig.add_axes([0.735, 0.04, 0.25, 0.845]); axp.axis("off")
axp.add_patch(Rectangle((0, 0), 1, 1, transform=axp.transAxes, facecolor="white",
                        edgecolor="#e3e8ee", lw=1, zorder=0))

def est(s):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4}) (\d{2})(\d{2})", s or "")
    if not m:
        return ""
    mo, da, yr, hh, mi = map(int, m.groups())
    tot = hh * 60 + mi - 300; day = da
    if tot < 0:
        tot += 1440; day -= 1
    return f"{mo:02d}-{day:02d} {tot // 60:02d}:{tot % 60:02d}"

axp.text(0.07, 0.94, "关闭清单", fontsize=14, fontweight="bold", color="#1c242e")
y = 0.865
for idx, name, no, st, en, kind in numbered:
    axp.add_patch(Circle((0.095, y), 0.021, transform=axp.transAxes,
                         facecolor="#b3202c", edgecolor="none"))
    axp.text(0.095, y, str(idx), transform=axp.transAxes, color="white", fontsize=9,
             fontweight="bold", ha="center", va="center")
    axp.text(0.145, y + 0.008, f"{name} 关闭", transform=axp.transAxes,
             fontsize=9.8, color="#1c242e")
    axp.text(0.145, y - 0.022, f"{no} · EST {est(st)}–{est(en)}", transform=axp.transAxes,
             fontsize=7.6, color="#77828f")
    y -= 0.072

axp.text(0.07, 0.30,
         "怎么读这张图\n"
         "· 红色粗线 = 滑行道关闭段，按通告\n"
         "　“BETWEEN X AND Y”截取成具体区段\n"
         "· 红色斜线 = 整条跑道关闭\n"
         "· 灰色线 = 滑行道面，深色为跑道\n"
         "· 圆圈数字对应右侧清单",
         transform=axp.transAxes, fontsize=8.2, color="#5c6875", va="top", linespacing=2.0)
axp.text(0.07, 0.115,
         "底图：X-Plane apt.dat 滑行网络 + OurAirports 跑道坐标\n"
         "仅为 NOTAM 可视化示意图，非官方航图，不可用于运行决策",
         transform=axp.transAxes, fontsize=7.2, color="#98a2ae", va="top", linespacing=1.8)

fig.text(0.015, 0.012,
         "数据：FAA NOTAM Search（2026-11-19 15:01Z）· X-Plane Scenery Gateway · OurAirports",
         fontsize=7.4, color="#9aa4af")

fig.savefig(OUT, facecolor=fig.get_facecolor())
print("saved", OUT, "标记数", len(numbered))
