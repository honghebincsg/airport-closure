#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在 FAA 官方机场图（d-TPP AD PDF，矢量）上叠加 NOTAM 关闭标注
1) PDF 矢量提取：跑道多边形（直接按 PDF 坐标画，零误差）
2) 跑道端点 ↔ 真实经纬度 做相似变换（scale+rotation+translation）完成配准
3) X-Plane 滑行网络算出的关闭段 → 反算回 PDF 坐标 → 叠加
"""
import re, csv, json, math
import numpy as np
import pdfplumber, networkx as nx
from PIL import Image, ImageDraw
from matplotlib import font_manager

PDF = "/tmp/kjfk_ad.pdf"
APT = "/tmp/kjfk_pack/KJFK_Scenery_Pack/Earth nav data/apt.dat"
RWY_CSV = "/tmp/runways.csv"
NOTAM = "/home/user/kjfk_notam_raw.json"
RASTER = "/tmp/kjfk_ad-1.png"          # pdftocairo -r 300 输出
DPI = 300
OUT = "/home/user/Doubao/chats/38454828072541442/KJFK_官方航图_关闭标注.png"

# ---------- 1. PDF 里的跑道多边形 ----------
pg = pdfplumber.open(PDF).pages[0]
polys = []
for o in pg.curves:
    if o.get("fill") and o.get("non_stroking_color") == (0.0, 0.0, 0.0) and o["width"] * o["height"] > 3000:
        raw = np.array([[q[0], q[1]] for q in o["pts"]], dtype=float)
        pts = np.array([[q[0], -q[1]] for q in o["pts"]], dtype=float)
        c = pts.mean(0); cov = np.cov((pts - c).T); ev, evec = np.linalg.eigh(cov); v = evec[:, 1]
        proj = (pts - c) @ v
        L = proj.max() - proj.min()
        if L < 110:
            continue
        polys.append(dict(L=L, e1=c + v * proj.min(), e2=c + v * proj.max(), raw=raw))
polys.sort(key=lambda d: d["L"])

REAL = {14511: ((40.648399, -73.816704), (40.627899, -73.771599)),   # 13R/31L
        10000: ((40.657799, -73.790199), (40.6437, -73.7593)),       # 13L/31R
        12079: ((40.622002, -73.785599), (40.6488, -73.764702)),     # 04L/22R
        8400:  ((40.62540054, -73.77030182), (40.64519882, -73.75489807))}  # 04R/22L
NAME = {14511: "13R/31L", 10000: "13L/31R", 12079: "04L/22R", 8400: "04R/22L"}

LAT0, LON0 = 40.6397, -73.78
def geo(lat, lon):
    return np.array([(lon - LON0) * 111320 * math.cos(math.radians(LAT0)), (lat - LAT0) * 110540])

LEN_SORT = sorted(REAL)          # 8400,10000,12079,14511
src, dst = [], []
for i, p in enumerate(polys):    # polys 已按长度升序
    key = LEN_SORT[i]
    p["rwy"] = NAME[key]
    a, b = geo(*REAL[key][0]), geo(*REAL[key][1])
    # 北端对北端：PDF 的 y 与地理 northing 排序一致
    if (p["e1"][1] > p["e2"][1]) != (a[1] > b[1]):
        a, b = b, a
    src += [p["e1"], p["e2"]]; dst += [a, b]
src, dst = np.array(src), np.array(dst)
mx, my = src.mean(0), dst.mean(0)
Xc, Yc = src - mx, dst - my
U, D, Vt = np.linalg.svd(Yc.T @ Xc / len(src))
R = U @ Vt
if np.linalg.det(R) < 0:
    U[:, -1] *= -1
    R = U @ Vt
S = D.sum() / Xc.var(0).sum()
T = my - S * R @ mx
res = np.linalg.norm((S * R @ src.T).T + T - dst, axis=1)
print(f"配准: 1pt={S:.3f}m  残差 RMS={np.sqrt((res**2).mean()):.1f}m  max={res.max():.1f}m")

def geo2pt(g):
    p = R.T @ (g - T) / S
    return p[0], -p[1]      # 回到 PDF top-down 坐标

def pt2px(x, top):
    return x * DPI / 72.0, top * DPI / 72.0

# ---------- 2. NOTAM 关闭段（沿用滑行网络算法） ----------
nodes, edges = {}, []
for line in open(APT, encoding="utf-8", errors="ignore"):
    f = line.rstrip().split()
    if line.startswith("1201"):
        nodes[int(f[4])] = (float(f[1]), float(f[2]))
    elif line.startswith("1202"):
        edges.append((int(f[1]), int(f[2]), " ".join(f[5:])))

def xy(lat, lon):
    return ((lon - LON0) * math.cos(math.radians(LAT0)) * 111320.0, (lat - LAT0) * 110540.0)

rows = json.load(open(NOTAM))["rows"]
def clean(t):
    return re.sub(r"(TWY|RWY)\s*[A-Z0-9]+\s*RSTRCTN>.*$", "", t).strip()

twy_cl, rwy_cl = [], []
for r in rows:
    q, t = r["qcode"], clean(r["text"])
    if q.startswith("QMR") and "CLSD" in t:
        m = re.search(r"RWY\s*(\d{2}[LCR]?/\d{2}[LCR]?)", t)
        if m: rwy_cl.append(dict(no=r["no"], rwy=m.group(1), s=r["start"], e=r["end"]))
    elif q.startswith("QMX") and "CLSD" in t:
        for seg in re.finditer(r"TWY\s+([A-Z0-9]+)\s+BTN\s+(?:TWY|RWY)\s+([A-Z0-9/]+)\s+AND\s+(?:TWY|RWY)\s+([A-Z0-9/]+)", t):
            twy_cl.append(dict(no=r["no"], twy=seg.group(1), b1=seg.group(2), b2=seg.group(3), s=r["start"], e=r["end"]))
        m = re.fullmatch(r"TWY\s+([A-Z0-9]+)\s+CLSD", t)
        if m: twy_cl.append(dict(no=r["no"], twy=m.group(1), b1=None, b2=None, s=r["start"], e=r["end"]))

def nodes_of(n):
    return {a for a, b, nm in edges if nm == n} | {b for a, b, nm in edges if nm == n}

def seg_dist(p, s1, s2):
    dx, dy = s2[0] - s1[0], s2[1] - s1[1]
    L2 = dx * dx + dy * dy
    if L2 == 0: return math.hypot(p[0] - s1[0], p[1] - s1[1])
    t = max(0, min(1, ((p[0] - s1[0]) * dx + (p[1] - s1[1]) * dy) / L2))
    return math.hypot(p[0] - (s1[0] + t * dx), p[1] - (s1[1] + t * dy))

def bnodes(twy, bnd):
    if not bnd: return set()
    shared = nodes_of(twy) & nodes_of(bnd)
    if shared: return shared
    segs = [(xy(*nodes[a]), xy(*nodes[b])) for a, b, nm in edges if nm == bnd and a in nodes and b in nodes]
    return {n for n in nodes_of(twy) if any(seg_dist(xy(*nodes[n]), s1, s2) <= 150 for s1, s2 in segs)}

segs_out = []
for c in twy_cl:
    sub = nx.Graph([(a, b) for a, b, nm in edges if nm == c["twy"]])
    if not sub.number_of_nodes(): continue
    if c["b1"] is None:
        comp = max(nx.connected_components(sub), key=len)
        t = sub.subgraph(comp); ends = [n for n in t.nodes if t.degree[n] == 1]
        pts = nx.shortest_path(t, ends[0], ends[1]) if len(ends) > 1 else list(t.nodes)
        segs_out.append(dict(label=f"TWY {c['twy']}", pts=pts, no=c["no"], s=c["s"], e=c["e"]))
        continue
    S1, S2 = bnodes(c["twy"], c["b1"]), bnodes(c["twy"], c["b2"])
    path = None
    for a1 in S1:
        for a2 in S2:
            if a1 != a2:
                try:
                    path = nx.shortest_path(sub, a1, a2); break
                except Exception: pass
        if path: break
    if path:
        segs_out.append(dict(label=f"TWY {c['twy']}（{c['b1']}–{c['b2']}）", pts=path, no=c["no"], s=c["s"], e=c["e"]))

# ---------- 3. 合成（跑道关闭：直接复用 PDF 原始矢量，零配准误差） ----------
FT = {"04L/22R": 12079, "04R/22L": 8400, "13L/31R": 10000, "13R/31L": 14511}
PT_PER_FT = 0.02163          # 实测：313.7pt / 14511ft
img = Image.open(RASTER).convert("RGB")
dr = ImageDraw.Draw(img, "RGBA")
W, H = img.size
print("画布", W, H)

closed = {c["rwy"] for c in rwy_cl}
print("通告关闭跑道:", sorted(closed))
for p in polys:
    hit = [n for n in closed if abs(p["L"] - FT[n] * PT_PER_FT) < 6]
    if not hit:
        continue
    name = hit[0]
    poly = [pt2px(x, top) for x, top in p["raw"]]
    dr.polygon(poly, fill=(228, 40, 55, 95), outline=(186, 18, 30, 255))
    ys = [y for _, y in poly]; xs = [x for x, _ in poly]
    y0, y1 = min(ys), max(ys)
    k = int(min(xs)) - int(y1 - y0)
    while k < int(max(xs)) + int(y1 - y0):
        dr.line([(k, y1), (k + (y1 - y0), y0)], fill=(186, 18, 30, 55), width=2)
        k += 13
    # CLSD 标签
    cx, cy = sum(xs) / 4, sum(ys) / 4
    lab = f"CLSD {name}"
    tw = dr.textlength(lab)
    dr.rectangle([cx - tw / 2 - 8, cy - 13, cx + tw / 2 + 8, cy + 13], fill=(186, 18, 30, 240))
    dr.text((cx - tw / 2, cy - 8), lab, fill=(255, 255, 255))

img.save(OUT)
print("saved", OUT)
