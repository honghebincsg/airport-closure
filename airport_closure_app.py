#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
机场关闭图生成器（本地网页版，单文件）
运行： python airport_closure_app.py        → 自动打开 http://127.0.0.1:8765
输入 ICAO → 抓/贴通告 → 解析关闭 → 套滑行网络 → 出图
"""
import io
import os
import re
import sys
import csv
import json
import math
import time
import zipfile
import tempfile
import textwrap
import threading
import webbrowser
import socketserver
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler

import requests
import networkx as nx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon, Rectangle, Circle
import matplotlib.patheffects as pe

BASE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
RES = getattr(sys, "_MEIPASS", BASE)
CACHE = os.path.join(BASE, "cache")
OUTDIR = os.path.join(BASE, "output")
os.makedirs(CACHE, exist_ok=True)
os.makedirs(OUTDIR, exist_ok=True)

PORT = 8765
RUNWAYS_CSV = os.path.join(RES, "runways.csv")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ---------------------------------------------------------------- 抓取
def make_session(proxy=None):
    s = requests.Session()
    s.trust_env = True
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    s.headers.update({"User-Agent": UA})
    return s

def post_json(url, data, headers, proxy=None, timeout=30):
    """优先用 curl_cffi 模拟 Chrome 的 TLS 指纹；
    FAA 的 Akamai 会按 TLS/JA3 指纹判定机器人，普通 requests 一律 403。"""
    try:
        from curl_cffi import requests as creq
        proxies = {"http": proxy, "https": proxy} if proxy else None
        r = creq.post(url, data=data, headers=headers, impersonate="chrome",
                      proxies=proxies, timeout=timeout)
        return r.status_code, r.text
    except ImportError:
        pass
    s = make_session(proxy)
    r = s.post(url, data=data, headers=dict(headers, **{"User-Agent": UA}), timeout=timeout)
    return r.status_code, r.text

def fetch_notams(icao, proxy=None, log=print):
    """FAA NOTAM Search 免密钥接口，分页取全量"""
    url = "https://notams.aim.faa.gov/notamSearch/search"
    hdr = {"Accept": "application/json, text/javascript, */*; q=0.01",
           "Origin": "https://notams.aim.faa.gov",
           "Referer": "https://notams.aim.faa.gov/notamSearch/nsapp.html",
           "X-Requested-With": "XMLHttpRequest",
           "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
    rows, offset, limit = [], 0, 50
    while True:
        code, body = post_json(url, {"searchType": "0", "designatorsForLocation": icao,
                                     "offset": str(offset), "limit": str(limit)}, hdr, proxy)
        if code != 200:
            raise RuntimeError(f"FAA 接口返回 {code}。若是 403：网络层拦截或 curl_cffi 未安装，"
                               f"可执行 pip install curl_cffi，或填代理，或改用“粘贴通告”模式）")
        j = json.loads(body)
        batch = j.get("notamList") or []
        for n in batch:
            txt = (n.get("icaoMessage") or "").strip() or \
                  (n.get("plainLanguageMessage") or "").strip() or \
                  (n.get("traditionalMessage") or "").strip()
            m = re.search(r"\bE\)\s*(.+)$", txt, re.S)      # FAA 常回整段报文，取 E) 正文
            if m:
                txt = m.group(1)
            rows.append({"no": n.get("notamNumber", ""), "text": txt,
                         "start": n.get("startDate", ""), "end": n.get("endDate", "")})
        offset += len(batch)
        log(f"  已取 {offset}/{j.get('totalNotamCount', '?')} 条")
        if not batch or offset >= (j.get("totalNotamCount") or 0):
            break
    return rows

def parse_pasted(text):
    """把粘贴的通告文本切成条目：形如 '11/292 ... ' 每段一条"""
    blocks = re.split(r"\n(?=\d{2}/\d{3}\b)", text.strip())
    out = []
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        m = re.match(r"(\d{2}/\d{3})\s*", b)
        no = m.group(1) if m else "手动"
        body = b[m.end():] if m else b
        sd = re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{4})", body)
        ed = re.findall(r"(\d{2}/\d{2}/\d{4}\s+\d{4})", body)
        out.append({"no": no, "text": body.strip(),
                    "start": sd.group(1) if sd else "",
                    "end": ed[1] if len(ed) > 1 else (ed[0] if ed else "")})
    return out

# ---------------------------------------------------------------- 解析
TAIL = re.compile(r"[A-Z]*RSTRCTN>.*$|>.*$|\s*TO\|\s*$")
BND = r"(?:(TWY|RWY|TERMINAL|RAMP|APRON)\s*)?([A-Z0-9]+(?:\s*RAMP)?|[0-9]{2}[A-Z]?/[0-9]{2}[A-Z]?)"
PAIR = re.compile(rf"^{BND}\s+AND\s+{BND}$")

def clean(t):
    t = re.sub(r"[A-Z]*RSTRCTN>\S*(?:\s+\S+)*?$", "", t)
    t = re.sub(r">\S*(?:\s+\S+)*?$", "", t)
    # 去掉尾部的时间段（粘贴/报文里常见 11/19/2026 1215 11/19/2026 2300）
    t = re.sub(r"\b\d{2}/\d{2}/\d{4}\s+\d{4}\b", " ", t)
    t = re.sub(r"\b\d{2}/\d{2}/\d{4}\s+\d{4}Z?\b", " ", t)
    t = re.sub(r"\s*EST\b|\s*EDT\b", " ", t)
    t = re.sub(r"(CLSD)[A-Za-z0-9/]+.*$", r"\1", t)   # CLSD 后粘连的残留文本直接截掉
    return re.sub(r"\s+", " ", t).strip(" ,;|")

def parse_closures(rows, log=print):
    objs = []
    for r in rows:
        t = clean(r["text"])
        if "CLSD" not in t.upper() and "CLOSED" not in t.upper():
            continue
        # 拆并列项：逗号分隔，无 CLSD 的片段若形如 "TWY X BTN ..." 则继承关闭
        frags = [f.strip() for f in re.split(r",|;", t) if f.strip()]
        for i, f in enumerate(frags):
            has_clsd = bool(re.search(r"CLSD|CLOSED", f, re.I))
            m = re.match(r"(TWY|RWY)\s+([A-Z0-9/]+)\s*(?:BTN\s+([^,]*?)\s*)?(CLSD|CLOSED)?$",
                         f.strip(" .|"), re.I)
            if not m:
                continue
            kind, name, between = m.group(1).upper(), m.group(2), m.group(3)
            if not has_clsd and not (between and any(
                    re.search(r"CLSD|CLOSED", x, re.I) for x in frags[i + 1:])):
                continue
            rec = dict(no=r["no"], kind=kind, name=name, b1=None, b2=None,
                       b1_type=None, b2_type=None,
                       start=r.get("start", ""), end=r.get("end", ""),
                       inherited=not has_clsd, evidence=f[:110], raw=t)
            if between:
                p = PAIR.match(between.strip())
                if p:
                    rec.update(b1_type=p.group(1), b1=p.group(2),
                               b2_type=p.group(3), b2=p.group(4))
            objs.append(rec)
    log(f"  解析出关闭对象 {len(objs)} 个")
    return objs

# ---------------------------------------------------------------- 几何
def _save_apt_meta(path, sid, sc):
    """记下所用场景包的批准日期，用于在图上标注几何基准"""
    date = ""
    for x in (sc or []):
        if str(x.get("sceneryId")) == str(sid):
            date = (x.get("dateApproved") or x.get("dateUploaded") or "")[:10]
            break
    try:
        json.dump({"sceneryId": sid, "date": date}, open(path, "w", encoding="utf-8"))
    except Exception:
        pass

def apt_meta(icao):
    p = os.path.join(CACHE, f"{icao}.apt_meta.json")
    try:
        return json.load(open(p, encoding="utf-8")).get("date", "")
    except Exception:
        return ""

def get_apt(icao, proxy=None, log=print):
    """从 X-Plane Scenery Gateway 取机场 apt.dat（含滑行网络），带缓存"""
    cache_file = os.path.join(CACHE, f"{icao}.apt.dat")
    meta_file = os.path.join(CACHE, f"{icao}.apt_meta.json")
    if os.path.exists(cache_file) and os.path.getsize(cache_file) > 1000:
        if not os.path.exists(meta_file):       # 有缓存但没日期，补一次很轻的元数据请求
            try:
                info = make_session(proxy).get(
                    f"https://gateway.x-plane.com/apiv1/airport/{icao}", timeout=20).json()
                info = info.get("airport", info)
                _save_apt_meta(meta_file, info.get("recommendedSceneryId"), info.get("scenery"))
            except Exception:
                pass
        return cache_file
    s = make_session(proxy)
    api = s.get(f"https://gateway.x-plane.com/apiv1/airport/{icao}", timeout=30)
    if api.status_code != 200:
        raise RuntimeError(f"该机场在 X-Plane 场景库中不存在（{api.status_code}），无法取得滑行网络")
    j = api.json()
    info = j.get("airport", j)
    sid = info.get("recommendedSceneryId")
    if not sid:                                  # 兜底：取最新已批准场景
        sc = info.get("scenery") or []
        sid = (sc[-1].get("sceneryId") if sc else None)
    if not sid:
        raise RuntimeError("该机场没有推荐场景包，无法取得滑行网络")
    _save_apt_meta(meta_file, sid, info.get("scenery"))
    z = s.get(f"https://gateway.x-plane.com/scenery/download/{sid}", timeout=120)
    if z.status_code != 200:
        raise RuntimeError(f"场景包下载失败（{z.status_code}）")
    tmp = tempfile.mkdtemp()
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        target = None
        for n in zf.namelist():
            if n.lower().endswith("apt.dat"):
                target = n
                break
        if not target:
            raise RuntimeError("场景包里没有 apt.dat")
        data = zf.read(target)
    with open(cache_file, "wb") as fh:
        fh.write(data)
    log(f"  场景包 {sid} → apt.dat {len(data) // 1024} KB（已缓存）")
    return cache_file

def load_network(apt_path, log=print):
    nodes, edges = {}, []
    for line in open(apt_path, encoding="utf-8", errors="ignore"):
        if line.startswith("1201"):
            f = line.split()
            try:
                nodes[int(f[4])] = (float(f[1]), float(f[2]))
            except Exception:
                pass
        elif line.startswith("1202"):
            f = line.split()
            try:
                edges.append((int(f[1]), int(f[2]), " ".join(f[5:]).strip()))
            except Exception:
                pass
    log(f"  滑行网络：{len(nodes)} 节点 / {len(edges)} 条边")
    return nodes, edges

AM_URL = ("https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/"
          "AM_Taxiway/FeatureServer/0/query")
AM_SERVICE = ("https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/"
              "AM_Taxiway/FeatureServer")

def am_effective(proxy=None):
    """FAA AM 数据集自述的生效周期（服务描述里带），缓存 7 天，用于图上标注几何基准"""
    mf = os.path.join(CACHE, "am_meta.json")
    try:
        j = json.load(open(mf, encoding="utf-8"))
        if time.time() - j.get("ts", 0) < 7 * 86400:
            return j.get("eff", "")
    except Exception:
        pass
    eff = ""
    try:
        d = make_session(proxy).get(AM_SERVICE, params={"f": "pjson"}, timeout=30).json()
        desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", d.get("description") or ""))
        m = re.search(r"Effective\s*Date:\s*\d{4}Z\s+(\d{1,2} [A-Za-z]{3} \d{4})"
                      r"\s+to\s*\d{4}Z\s+(\d{1,2} [A-Za-z]{3} \d{4})", desc, re.I)
        if m:
            a = datetime.strptime(m.group(1), "%d %b %Y")
            b = datetime.strptime(m.group(2), "%d %b %Y")
            eff = f"{a:%Y-%m} ~ {b:%Y-%m}"
    except Exception:
        pass
    try:
        json.dump({"eff": eff, "ts": time.time()}, open(mf, "w", encoding="utf-8"))
    except Exception:
        pass
    return eff

def load_am_taxiways(icao, proxy=None, log=print):
    """FAA Airport Mapping 滑行道面（含 A4/A14 这类编号分支），公开 REST，免密钥"""
    cache_file = os.path.join(CACHE, f"{icao}.am_taxiway.json")
    if os.path.exists(cache_file) and os.path.getsize(cache_file) > 100:
        try:
            return json.load(open(cache_file, encoding="utf-8"))
        except Exception:
            pass
    params = {"where": f"ICAO_ID='{icao}'", "outFields": "DESIGNATOR",
              "returnGeometry": "true", "outSR": "4326",
              "geometryPrecision": "6", "f": "geojson"}
    try:
        s = make_session(proxy)
        r = s.get(AM_URL, params=params, timeout=90)
        if r.status_code != 200:
            log(f"  AM 滑行道面接口 {r.status_code}，跳过面片兜底")
            return {}
        feats = (r.json().get("features") or [])
    except Exception as e:
        log(f"  AM 滑行道面获取失败（{type(e).__name__}），跳过面片兜底")
        return {}
    am = {}
    for f in feats:
        d = (f.get("properties") or {}).get("DESIGNATOR")
        g = f.get("geometry") or {}
        if not d or g.get("type") != "Polygon":
            continue
        am.setdefault(d, []).append(g["coordinates"][0])   # 只取外环
    json.dump(am, open(cache_file, "w", encoding="utf-8"))
    log(f"  FAA AM 滑行道面：{len(feats)} 片 / {len(am)} 个标识（已缓存）")
    return am

def _ring_bbox(ring):
    xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)

def _poly_dist(a, b):
    """两个面最小顶点距离（近似），单位度"""
    ba, bb = _ring_bbox(a), _ring_bbox(b)
    if (ba[0] > bb[2] + 0.001 or bb[0] > ba[2] + 0.001 or
            ba[1] > bb[3] + 0.001 or bb[1] > ba[3] + 0.001):
        return 9.9
    best = 9.9
    for pa in a[:200]:
        for pb in b[:200]:
            d = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
            if d < best:
                best = d
    return best

def _centroid(ring):
    return (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))

def pick_am_polys(name, b1, b2, am, tol_deg=0.0016):
    """在 FAA AM 面片里找出通告指向的那一块／那一段"""
    if not am:
        return []
    base = list(am.get(name) or [])
    if not base:
        return []
    if not (b1 and b2):
        return base                                   # 整条关闭

    def cross(sfx):
        if not sfx:
            return []
        key = f"{name}_{sfx}"
        if key in am:
            return am[key]
        sfx_c = sfx.replace(" ", "")
        for k, r in am.items():                       # B_A15 这类交叉段命名
            if k.startswith(name + "_") and k.split("_")[-1] == sfx_c:
                return r
        return []

    c1, c2 = cross(b1), cross(b2)

    def anchor(sfx, cr):
        """边界锚点：优先用交叉段面片，没有就退回该要素自身的面"""
        if cr:
            return _centroid(cr[0])
        if sfx in am:
            return _centroid(am[sfx][0])
        return None

    a1, a2 = anchor(b1, c1), anchor(b2, c2)
    if a1 and a2:
        p1, p2 = a1, a2
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        L2 = (dx * dx + dy * dy) or 1e-12
        picked = []
        for ring in base:
            cx, cy = _centroid(ring)
            t = ((cx - p1[0]) * dx + (cy - p1[1]) * dy) / L2
            qx, qy = p1[0] + t * dx, p1[1] + t * dy
            if -0.03 <= t <= 1.03 and math.hypot(cx - qx, cy - qy) < tol_deg:
                picked.append(ring)                   # 落在两边界之间的主道面
        if picked:
            return picked + c1 + c2
        return (c2 or c1 or base)
    if c2:
        return c2
    if c1:
        return c1
    refs = [am[b][0] for b in (b1, b2) if b in am]
    if len(refs) == 2:                                # 几何相邻兜底
        near = [r for rings in base for r in rings
                if all(_poly_dist(r, ref) < 0.00035 for ref in refs)]
        if near:
            return near
    return base

def load_runways(icao, log=print):
    path = RUNWAYS_CSV
    if not os.path.exists(path):                       # 源码运行时向仓库取一次
        path = os.path.join(CACHE, "runways.csv")
        if not os.path.exists(path):
            s = make_session()
            r = s.get("https://raw.githubusercontent.com/davidmegginson/"
                      "ourairports-data/main/runways.csv", timeout=180)
            open(path, "wb").write(r.content)
    out = []
    for r in csv.DictReader(open(path, encoding="utf-8")):
        if r["airport_ident"] != icao:
            continue
        try:
            out.append(dict(a=(r["le_ident"], float(r["le_latitude_deg"]), float(r["le_longitude_deg"])),
                            b=(r["he_ident"], float(r["he_latitude_deg"]), float(r["he_longitude_deg"])),
                            width_m=float(r["width_ft"] or 150) * 0.3048))
        except Exception:
            pass
    log(f"  跑道 {len(out)} 条")
    return out

# ---------------------------------------------------------------- 求解
def solve(objs, nodes, edges, log=print, am=None):
    LAT0 = sum(n[0] for n in nodes.values()) / len(nodes)
    LON0 = sum(n[1] for n in nodes.values()) / len(nodes)

    def xy(lat, lon):
        return ((lon - LON0) * math.cos(math.radians(LAT0)) * 111320.0,
                (lat - LAT0) * 110540.0)

    def nodes_of(n):
        return {a for a, b, nm in edges if nm == n} | {b for a, b, nm in edges if nm == n}

    def seg_dist(p, s1, s2):
        dx, dy = s2[0] - s1[0], s2[1] - s1[1]
        L2 = dx * dx + dy * dy
        if L2 == 0:
            return math.hypot(p[0] - s1[0], p[1] - s1[1])
        t = max(0, min(1, ((p[0] - s1[0]) * dx + (p[1] - s1[1]) * dy) / L2))
        return math.hypot(p[0] - (s1[0] + t * dx), p[1] - (s1[1] + t * dy))

    def bnodes(twy, bnd):
        if not bnd:
            return set()
        shared = nodes_of(twy) & nodes_of(bnd)
        if shared:
            return shared
        segs = [(xy(*nodes[a]), xy(*nodes[b])) for a, b, nm in edges
                if nm == bnd and a in nodes and b in nodes]
        if not segs:
            return set()
        return {n for n in nodes_of(twy)
                if any(seg_dist(xy(*nodes[n]), s1, s2) <= 150 for s1, s2 in segs)}

    segments, unmatched = [], []
    for c in objs:
        if c["kind"] != "TWY":
            continue
        tag = f"TWY {c['name']}" + (f"（{c['b1']}–{c['b2']}）" if c.get("b1") else "")
        if c["name"] not in {nm for _, _, nm in edges}:
            unmatched.append((tag, c, "滑行网络里无此滑行道"))
            continue
        sub = nx.Graph([(a, b) for a, b, nm in edges if nm == c["name"]])
        apron = [x for x in (c.get("b1_type"), c.get("b2_type"))
                 if x in ("TERMINAL", "RAMP", "APRON")]
        if apron:
            other = c["b2"] if c["b1_type"] in ("TERMINAL", "RAMP", "APRON") else c["b1"]
            S = bnodes(c["name"], other)
            if S:
                ends = [n for n in sub.nodes if sub.degree[n] == 1 and n not in S]
                path = None
                for a1 in S:
                    for a2 in ends:
                        try:
                            path = nx.shortest_path(sub, a1, a2); break
                        except Exception:
                            pass
                    if path:
                        break
                if path:
                    segments.append(dict(label=tag, no=c["no"], start=c["start"], end=c["end"],
                                         raw=c.get("raw", ""), pts=[xy(*nodes[n]) for n in path]))
                    continue
            unmatched.append((tag, c, "机坪侧无可用端点"))
            continue
        if c.get("b1") is None:
            comp = max(nx.connected_components(sub), key=len)
            tree = sub.subgraph(comp)
            ends = [n for n in tree.nodes if tree.degree[n] == 1]
            path = nx.shortest_path(tree, ends[0], ends[1]) if len(ends) > 1 else list(tree.nodes)
            segments.append(dict(label=tag, no=c["no"], start=c["start"], end=c["end"],
                                 raw=c.get("raw", ""), pts=[xy(*nodes[n]) for n in path]))
            continue
        S1, S2 = bnodes(c["name"], c["b1"]), bnodes(c["name"], c["b2"])
        path = None
        for a1 in S1:
            for a2 in S2:
                if a1 != a2:
                    try:
                        path = nx.shortest_path(sub, a1, a2); break
                    except Exception:
                        pass
            if path:
                break
        if path:
            segments.append(dict(label=tag, no=c["no"], start=c["start"], end=c["end"],
                                 raw=c.get("raw", ""), pts=[xy(*nodes[n]) for n in path]))
        else:
            why = "边界要素不在网络" if not (S1 and S2) else "两边界无连通路径"
            unmatched.append((tag, c, why))
    still = []
    for tag, c, why in unmatched:
        rings = pick_am_polys(c["name"], c.get("b1"), c.get("b2"), am) if am else []
        poly = []
        for r in rings:
            ring = [xy(pt[1], pt[0]) for pt in r]        # AM 是 [lon,lat]，xy 要 (lat,lon)
            if len(ring) >= 3:
                poly.append(ring)
        if poly:
            segments.append(dict(label=tag, no=c["no"], start=c["start"], end=c["end"],
                                 raw=c.get("raw", ""), pts=[], poly=poly, src="FAA AM 面片"))
            log(f"  AM 面片兜底：{tag}（{c['no']}）按官方滑行道面落段")
            continue
        still.append((tag, c["no"], why))
    unmatched = still
    log(f"  可落段 {len(segments)}，未定位 {len(unmatched)}")
    return segments, unmatched, xy, LAT0, LON0, (nodes, edges)

# ---------------------------------------------------------------- 渲染
def setup_font():
    for f in ("Microsoft YaHei", "SimHei", "PingFang SC", "Noto Sans CJK SC",
              "Noto Sans CJK JP", "WenQuanYi Zen Hei", "DejaVu Sans"):
        try:
            from matplotlib.font_manager import findfont, FontProperties
            p = findfont(FontProperties(family=f), fallback_to_default=False)
            if p and os.path.exists(p):
                plt.rcParams["font.family"] = [f, "DejaVu Sans"]
                return f
        except Exception:
            continue
    plt.rcParams["font.family"] = ["DejaVu Sans"]
    return "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False

def utc_str(s, with_year=False):
    """FAA 通告起止本就是 UTC，直接按 UTC 原样输出"""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2})(\d{2})", s or "")
    if not m:
        return s or "—"
    mo, da, yr, hh, mi = map(int, m.groups())
    core = f"{hh:02d}:{mi:02d}"
    return f"{yr}-{mo:02d}-{da:02d} {core}Z" if with_year else f"{mo:02d}-{da:02d} {core}Z"

def utc_span(st, en):
    """同日只写一次日期：11-20 04:00Z–11:00Z"""
    a, b = utc_str(st), utc_str(en)
    if len(a) > 6 and a[:5] == b[:5]:
        return f"{a}–{b[6:]}"
    return f"{a}–{b}"

def utc_short(st, en):
    """图上用的时段：当日 → 20 12:00Z–20:00Z；同月跨日 → 20 04:00Z–26 04:00Z；跨月 → 11-20 …"""
    try:
        a = datetime.strptime(st[:15], "%m/%d/%Y %H%M")
        b = datetime.strptime(en[:15], "%m/%d/%Y %H%M")
    except Exception:
        return utc_span(st, en)
    ha, hb = a.strftime("%H:%M"), b.strftime("%H:%M")
    if a.date() == b.date():
        return f"{a.day:02d} {ha}Z–{hb}Z"
    if (a.year, a.month) == (b.year, b.month):
        return f"{a.day:02d} {ha}Z–{b.day:02d} {hb}Z"
    return f"{a.month:02d}-{a.day:02d} {ha}Z–{b.month:02d}-{b.day:02d} {hb}Z"

def circled(n):
    """1 → ① … 20 → ⑳；超出范围退回括号数字"""
    return chr(0x2460 + n - 1) if 1 <= n <= 20 else f"({n})"


def span_days(st, en):
    try:
        a = datetime.strptime(st[:15], "%m/%d/%Y %H%M")
        b = datetime.strptime(en[:15], "%m/%d/%Y %H%M")
        return (b - a).total_seconds() / 86400
    except Exception:
        return 0

def render(icao, objs, segments, unmatched, runways, nodes, edges, xy, out_png, at_label=None,
           geo_note="", am=None):
    setup_font()
    fig = plt.figure(figsize=(16, 9), dpi=170, facecolor="#f7f8fa")
    ax = fig.add_axes([0.015, 0.04, 0.685, 0.845]); ax.set_facecolor("#f7f8fa")
    ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)

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

    # 关闭几何的采样点：只用于给跑道标签挑空地，避免标签压住关闭红线/红面
    def _densify(pts_, step=80):
        """按弧长把折线采样成密集点（每 80 m 一个），供避让判定用。
        之前按点数比例抽样，长线段中段没采样点，编号会落在线上。"""
        out = []
        for i in range(len(pts_) - 1):
            a_, b_ = pts_[i], pts_[i + 1]
            L_ = math.hypot(b_[0] - a_[0], b_[1] - a_[1])
            n_ = max(1, int(L_ / step))
            for k in range(n_ + 1):
                t_ = k / n_
                out.append((a_[0] + (b_[0] - a_[0]) * t_, a_[1] + (b_[1] - a_[1]) * t_))
        return out

    SEG_PTS = []
    for s_ in segments:
        if s_.get("poly"):
            for r_ in s_["poly"]:
                SEG_PTS.extend(_densify(r_))
        elif s_.get("pts"):
            SEG_PTS.extend(_densify(s_["pts"]))

    def seg_clash(x, y, r=200):
        return any(math.hypot(x - p_[0], y - p_[1]) < (r + 60) for p_ in SEG_PTS)

    PLACED = []                                   # (x, y, r) 已占用区域
    def reserve(x, y, r):
        PLACED.append((x, y, r))
    def clash(x, y, r):
        return any(math.hypot(x - p[0], y - p[1]) < (r + p[2]) for p in PLACED)
    for rw in runways:
        poly, p1, p2 = rw_poly(rw)
        ax.add_patch(Polygon(poly, closed=True, facecolor="#39424c", edgecolor="#232a32", lw=0.8, zorder=3))
        ax.plot([p1[0], p2[0]], [p1[1], p2[1]], color="#f2f4f7", lw=0.7, ls=(0, (7, 7)), zorder=4, alpha=0.7)
        for desig, pt, other in ((rw["a"][0], p1, p2), (rw["b"][0], p2, p1)):
            vx, vy = pt[0] - other[0], pt[1] - other[1]
            dd = math.hypot(vx, vy) or 1
            lx, ly = pt[0] - vx / dd * 90, pt[1] - vy / dd * 90
            ax.text(lx, ly, desig, color="#f7f8fa", fontsize=8,
                    fontweight="bold", ha="center", va="center", zorder=6,
                    path_effects=[pe.withStroke(linewidth=1.6, foreground="#2b323a")])
            reserve(lx, ly, 95)                      # 跑道端号不可压

    PXY = [xy(*v) for v in nodes.values()] if nodes else [(0.0, 0.0)]
    BBOX = (min(p[0] for p in PXY), max(p[0] for p in PXY),
            min(p[1] for p in PXY), max(p[1] for p in PXY))

    BADGES = []                               # 调试用：记录每个编号实际落点
    LEAD = [0, 0]                             # [引线数, 贴住未画线数]
    STAT = {"ring": 0, "near": 0, "grid": 0, "hard": 0}

    def badge(x0, y0, idx, R=122, fc="#b3202c", lc="#b3202c", src=None):
        # 引线不能穿过已经摆好的编号圈/标签框——否则线从别人的圆上碾过去，
        # 看起来像连错了对象。这里用近似锚点先判，选位置时就把这种候选剔掉。
        def lead_clash(a_, b_):
            for t in range(1, 15):
                t_ = t / 15.0
                x_ = a_[0] + (b_[0] - a_[0]) * t_
                y_ = a_[1] + (b_[1] - a_[1]) * t_
                if clash(x_, y_, 26):
                    return True
            return False

        def ok_at(c, need_lead=True):
            return (not clash(c[0], c[1], R)
                    and not seg_clash(c[0], c[1], 80)
                    and (not need_lead or not lead_clash((x0, y0), c)))

        # 第一层：以锚点为圆心的极坐标细扫，半径 175→800 m 每 40 m、角度每 15°。
        # 细扫而不是固定几档半径，是为了"就近"——先找 175 m 的缝，而不是一步跳到 790 m，
        # 否则挤的地方编号会被甩到半张图外，引线拉得又长又穿字母。
        # 下限 175 m 是有意的：96~175 m 这一档里，圆圈会半盖住关闭段、引线只剩几十米，
        # 画出来像一根没头没尾的小毛刺，宁可让编号站远一点、线清清楚楚。
        best, how = None, "hard"
        for need_lead in (True, False):
            for r in range(175, 820, 40):
                for k in range(24):
                    ang = 2 * math.pi * k / 24
                    c = (x0 + r * math.cos(ang), y0 + r * math.sin(ang) * 0.82)
                    if ok_at(c, need_lead):
                        best, how = c, ("ring" if need_lead else "near"); break
                if best:
                    break
            if best:
                break
        # 第二层：实在没地方时允许贴到 100 m——此时圆圈会盖住关闭段端头，不再画线
        # （引线为负长度，画反而更乱），换来的是"至少编号在图上"。
        if best is None:
            for k in range(24):
                ang = 2 * math.pi * k / 24
                c = (x0 + 100 * math.cos(ang), y0 + 100 * math.sin(ang) * 0.82)
                if ok_at(c, False):
                    best, how = c, "near"; break
        # 第三层：整幅网格兜底（±1800 m、步长 45 m，先挑引线不穿人的，再退一步放宽）
        if best is None:
            gx0, gx1 = BBOX[0] - 1800, BBOX[1] + 1800
            gy0, gy1 = BBOX[2] - 1800, BBOX[3] + 1800
            for need_lead in (True, False):
                best, bestd = None, 1e18
                gx = gx0
                while gx <= gx1:
                    gy = gy0
                    while gy <= gy1:
                        if ok_at((gx, gy), need_lead):
                            d = math.hypot(gx - x0, gy - y0)
                            if d < bestd:
                                bestd, best = d, (gx, gy)
                        gy += 45.0
                    gx += 45.0
                if best is not None:
                    break
            if best is not None:
                how = "grid"
        if best is None:                          # 实在无解：退回贴住关闭几何，至少不压别人
            best, how = (x0, y0), "hard"
        STAT[how] += 1
        reserve(best[0], best[1], R)
        # 引线：锚点取"关闭几何上离编号最近的一点"，线永远垂直插向被关的那一段，
        # 不再用段中点，避免长段上编号偏在一头时线从远处斜穿过来。
        anc = None
        if src:
            anc = min(src, key=lambda p_: math.hypot(p_[0] - best[0], p_[1] - best[1]))
        if anc is None:
            anc = (x0, y0)
        d_line = math.hypot(best[0] - anc[0], best[1] - anc[1])
        if d_line > 96:            # 锚点已明显落在圆圈外才画，否则线只有几个像素，像噪点
            dx_, dy_ = best[0] - anc[0], best[1] - anc[1]
            dd_ = d_line or 1
            ax.plot([anc[0], best[0] - dx_ / dd_ * 74], [anc[1], best[1] - dy_ / dd_ * 74],
                    color="#e63946", lw=1.1, alpha=0.95, zorder=8.6,
                    solid_capstyle="round")        # 8.6：在关闭段(8)之上、编号圈(9)之下，
                                            # 这样关闭段的粗红线不会把这根细线吃掉
            ax.add_patch(Circle(anc, 3.4, facecolor="#e63946", edgecolor="#ffffff",
                                lw=0.7, zorder=8.65))   # 起点小圆点：一眼看出从哪段引出来的
            LEAD[0] += 1
        else:
            LEAD[1] += 1
        ax.add_patch(Circle(best, 72, facecolor=fc, edgecolor="white", lw=2.0, zorder=9))
        ax.text(best[0], best[1], str(idx), color="white", fontsize=11, fontweight="bold",
                ha="center", va="center", zorder=10)
        BADGES.append([int(idx), round(best[0]), round(best[1]), fc])

    numbered, cnt = [], 0
    # 跑道关闭标签不许压在任何一条跑道面上（KSEA 那种窄间距机场，标签会被相邻跑道吃掉）
    # 把每条跑道的中心线按 60 m 采样成点，带各自半宽，供标签判定用
    RWY_SOLID = []
    for rw_ in runways:
        _p, pa_, pb_ = rw_poly(rw_)
        hw_ = rw_["width_m"] / 2
        for q_ in _densify([pa_, pb_], 60):
            RWY_SOLID.append((q_[0], q_[1], hw_))

    def rwy_clash(x, y, r):
        return any(math.hypot(x - p[0], y - p[1]) < (r + p[2] + 18) for p in RWY_SOLID)

    for c in [o for o in objs if o["kind"] == "RWY"]:
        for rw in runways:
            if rw["a"][0] in c["name"].split("/") and rw["b"][0] in c["name"].split("/"):
                poly, p1, p2 = rw_poly(rw)
                ax.add_patch(Polygon(poly, closed=True, facecolor="#e63946", edgecolor="#c1212e",
                                     lw=2.0, alpha=0.28, hatch="////", zorder=5))
                ax.add_patch(Polygon(poly, closed=True, facecolor="none", edgecolor="#c1212e", lw=2.2, zorder=5))
                q = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
                rx_, ry_ = -(p2[1] - p1[1]), (p2[0] - p1[0])          # 跑道法向，用于往旁边贴字
                RL = math.hypot(rx_, ry_) or 1
                span_txt = utc_short(c["start"], c["end"]) if (c.get("start") and c.get("end")) else ""
                cnt += 1
                # 编号保持原来的圆圈样式，贴在时间框左侧、与之同一水平线；
                # 框用左对齐，锚点 spot_t 就是框左边缘，圆放在锚点左侧固定距离。
                label_y = (span_txt + "\n" if span_txt else "") + f"{c['name']} CLSD"
                d0 = rw["width_m"] / 2 + 345
                # 沿跑道轴向试多个位置（含跑道两端再往外伸）× 3 档外移 × 两侧，
                # 取第一个不冲突的（就近优先）。窄间距机场靠"端外"位置找空地。
                cands = []
                for t_ in (0.5, 0.34, 0.66, 0.20, 0.80, 1.14, -0.14, 1.32, -0.32):
                    cx_ = p1[0] + (p2[0] - p1[0]) * t_
                    cy_ = p1[1] + (p2[1] - p1[1]) * t_
                    for mul in (1.0, 1.6, 2.4):
                        for sgn in (1, -1):
                            cands.append((cx_ + rx_ / RL * d0 * mul * sgn,
                                          cy_ + ry_ / RL * d0 * mul * sgn))
                spot_t = None
                for c_ in cands:
                    if (not clash(c_[0] + 240, c_[1], 245)
                            and not seg_clash(c_[0] + 240, c_[1], 195)
                            and not clash(c_[0] - 142, c_[1], 122)
                            and not rwy_clash(c_[0] + 240, c_[1], 245)):
                        spot_t = c_; break
                if spot_t is None:
                    spot_t = cands[0]
                ax.text(spot_t[0], spot_t[1], label_y, fontsize=8.2, fontweight="bold",
                        color="#b3202c", ha="left", va="center", multialignment="left",
                        linespacing=1.45, zorder=12,     # 必须压在关闭红线之上，否则红线会穿过文字
                        bbox=dict(boxstyle="round,pad=0.42", fc="#ffffff", ec="#b3202c",
                                  lw=1.1, alpha=0.97))
                bx_, by_ = spot_t[0] - 142, spot_t[1]
                ax.add_patch(Circle((bx_, by_), 72, facecolor="#b3202c", edgecolor="white",
                                    lw=2.0, zorder=12))
                ax.text(bx_, by_, str(cnt), color="white", fontsize=11, fontweight="bold",
                        ha="center", va="center", zorder=13)
                BADGES.append([int(cnt), round(bx_), round(by_), "#b3202c"])
                reserve(spot_t[0] + 240, spot_t[1], 245)     # 框整体占位
                reserve(bx_, by_, 122)                       # 编号圈占位
                numbered.append((cnt, f"RWY {c['name']}", c["no"], c["start"], c["end"],
                                 c.get("raw", "")))

    for s in segments:
        if s.get("poly"):
            for ring in s["poly"]:
                ax.add_patch(Polygon(ring, closed=True, facecolor="#e63946", alpha=0.42,
                                     edgecolor="#b3202c", lw=1.7, zorder=8))
                ax.plot([p[0] for p in ring], [p[1] for p in ring], color="white", lw=1.2,
                        alpha=0.55, zorder=9)
            cx = sum(p[0] for r in s["poly"] for p in r) / sum(len(r) for r in s["poly"])
            cy = sum(p[1] for r in s["poly"] for p in r) / sum(len(r) for r in s["poly"])
            xs_ = [p[0] for r in s["poly"] for p in r]; ys_ = [p[1] for r in s["poly"] for p in r]
            if (max(ys_) - min(ys_)) >= (max(xs_) - min(xs_)):
                vx, vy = 1.0, 0.0                 # 面片竖长 → 往左右让
            else:
                vx, vy = 0.0, 1.0                 # 面片横长 → 往上下让
            cnt += 1
            numbered.append((cnt, s["label"], s["no"], s["start"], s["end"], s.get("raw", "")))
            # 淡红编号 = 这一条是按 FAA 面片落的段（与线段的深红区分）
            src_poly = [q for r_ in s["poly"] for q in _densify(r_ + [r_[0]])]
            badge(cx + vx * 205, cy + vy * 205, cnt, fc="#dd5158", lc="#dd5158", src=src_poly)
            continue
        xs = [p[0] for p in s["pts"]]; ys = [p[1] for p in s["pts"]]
        ax.plot(xs, ys, color="white", lw=9.5, solid_capstyle="round", zorder=7)
        ax.plot(xs, ys, color="#e63946", lw=5.4, solid_capstyle="round", zorder=8)
        cnt += 1
        numbered.append((cnt, s["label"], s["no"], s["start"], s["end"], s.get("raw", "")))
        ax_, ay_ = xs[len(xs) // 2], ys[len(ys) // 2]
        nx_, ny_ = (ys[-1] - ys[0]), -(xs[-1] - xs[0])
        L = math.hypot(nx_, ny_) or 1
        badge(ax_ + nx_ / L * 130, ay_ + ny_ / L * 130, cnt, src=_densify(s["pts"]))

    # 滑行道标识最后画：编号圈与跑道端号已全部占位，字母让开它们；
    # 每个连通分量标一次（长道被跑道切断时两段各标），位置取最长边的中点，
    # 冲突时沿法向平移，实在挤不下就不标——宁可少一个字母，也不让字母被编号压住。
    grp = {}
    for a, b, nm in edges:
        if nm and a in nodes and b in nodes and not re.search(r"/", nm):
            grp.setdefault(nm, []).append((a, b))
    items = []
    for nm, es in grp.items():
        sub = nx.Graph(es)
        for comp in nx.connected_components(sub):
            best_len, best = -1, None
            for a, b in es:
                if a in comp and b in comp:
                    x1, y1 = xy(*nodes[a]); x2, y2 = xy(*nodes[b])
                    L = math.hypot(x2 - x1, y2 - y1) or 1
                    if L > best_len:
                        best_len = L
                        best = ((x1 + x2) / 2, (y1 + y2) / 2, (-(y2 - y1) / L, (x2 - x1) / L))
            if best and best_len >= 50:            # 太短的残段不标，避免文字碎片
                items.append((nm, best, best_len, "net"))
    for s in segments:                             # AM 面片落段也要有字母（网络里没有这条道）
        if not s.get("poly"):
            continue
        nm = re.split(r"[（(]", s["label"])[0].replace("TWY", "").strip()
        if not nm or nm in grp:
            continue
        xs_ = [p[0] for r in s["poly"] for p in r]
        ys_ = [p[1] for r in s["poly"] for p in r]
        cxx, cyy = (min(xs_) + max(xs_)) / 2, (min(ys_) + max(ys_)) / 2
        L = max(max(xs_) - min(xs_), max(ys_) - min(ys_))
        if (max(ys_) - min(ys_)) >= (max(xs_) - min(xs_)):
            items.append((nm, (cxx, cyy, (1.0, 0.0)), max(L, 50), "am"))
        else:
            items.append((nm, (cxx, cyy, (0.0, 1.0)), max(L, 50), "am"))
    # 通告里点名的边界要素（A14 / A15 / A12 / KD 这类），网络里没有、但 AM 有面片，
    # 也要标出来——它们是关闭段的端点，不标出来就看不懂"哪一段关了"。
    seen = {x[0] for x in items}
    for c in objs:
        for b in (c.get("b1"), c.get("b2")):
            if not b or "/" in b:
                continue
            nm = re.sub(r"^\s*(TWY|RWY|TERMINAL|RAMP|APRON)\s*", "", b)
            nm = nm.replace("RAMP", "").strip()
            if not nm or nm in seen:
                continue
            ring = (am or {}).get(nm)
            if not ring:
                continue
            r0 = ring[0]
            xs_ = [q[0] for q in r0]; ys_ = [q[1] for q in r0]
            px_, py_ = xy((min(ys_) + max(ys_)) / 2, (min(xs_) + max(xs_)) / 2)
            items.append((nm, (px_, py_, (1.0, 0.0)), 55, "bnd"))
            seen.add(nm)
    items.sort(key=lambda t: -t[2])                # 长道优先占位，短道让路
    LBL_DBG = []
    n_lbl = 0
    for nm, (x, y, nv), L, kind in items:
        rr = 22 + 10 * min(len(nm), 3)
        cand = [(x, y)]
        for d in (72, 132, 200, 270):
            cand.append((x + nv[0] * d, y + nv[1] * d))
            cand.append((x - nv[0] * d, y - nv[1] * d))
        for r in (85, 145, 215, 295, 380):     # 还挤不开时环绕搜索
            for k in range(8):
                ang = k * math.pi / 4
                cand.append((x + r * math.cos(ang), y + r * math.sin(ang)))
        cand.sort(key=lambda c: math.hypot(c[0] - x, c[1] - y))
        spot = None
        for v in cand:
            if not clash(v[0], v[1], rr):
                spot = v; break
        if spot is None:
            LBL_DBG.append([nm, kind, 0, round(x), round(y)])
            continue
        LBL_DBG.append([nm, kind, 1, round(spot[0]), round(spot[1])])
        reserve(spot[0], spot[1], rr)
        if kind == "bnd":                      # 边界要素（AM 面片上的分支道）用淡红小斜体
            ax.text(spot[0], spot[1], nm, fontsize=5.5, color="#b0525b", style="italic",
                    ha="center", va="center", zorder=11,
                    path_effects=[pe.withStroke(linewidth=2.2, foreground="#f7f8fa")])
        elif kind == "am":                     # AM 面片落段（淡红编号对应的那道）
            ax.text(spot[0], spot[1], nm, fontsize=5.8, color="#b0525b",
                    ha="center", va="center", zorder=11,
                    path_effects=[pe.withStroke(linewidth=2.2, foreground="#f7f8fa")])
        else:
            ax.text(spot[0], spot[1], nm, fontsize=6.5 if len(nm) <= 2 else 5.8, color="#3f4a56",
                    ha="center", va="center", zorder=11,
                    path_effects=[pe.withStroke(linewidth=2.4, foreground="#f7f8fa")])
        n_lbl += 1
        _ = n_lbl

    ax.margins(0.05)                          # 留边距，避免边缘的字母/标注被裁掉
    ax.autoscale_view()
    x0, x1 = ax.get_xlim(); y0, y1 = ax.get_ylim()
    span = (x1 - x0) or 1000
    sx, sy, bar = x0 + span * 0.03, y0 + span * 0.03, 1000
    ax.plot([sx, sx + bar], [sy, sy], color="#2f3b45", lw=3, zorder=12)
    for bx in (sx, sx + bar):
        ax.plot([bx, bx], [sy - 15, sy + 15], color="#2f3b45", lw=1.6, zorder=12)
    ax.text(sx + bar / 2, sy + 26, "1 km", fontsize=8.5, color="#2f3b45", ha="center")

    fig.text(0.015, 0.968, f"{icao} 机场关闭区域图", fontsize=20, fontweight="bold",
             color="#1c242e", va="top")
    sub = (at_label + "　·　" if at_label else "") + \
          f"通告 {len(objs)} 个关闭 → 上图 {len(numbered)} 处，未定位 {len(unmatched)} 条"
    fig.text(0.015, 0.930, sub, fontsize=10, color="#6b7684", va="top")
    # 醒目声明：右上角标题区，白底红框（放在图内会遮道面，放在标题行右侧最干净）
    fig.text(0.985, 0.946, "此图只作参考，不作为飞行或放行依据",
             fontsize=11.5, fontweight="bold", color="#b3202c", ha="right", va="center",
             bbox=dict(boxstyle="round,pad=0.52", fc="#ffffff", ec="#b3202c", lw=1.5))
    if geo_note:
        fig.text(0.015, 0.908, geo_note, fontsize=8.6, color="#8994a1", va="top")

    axp = fig.add_axes([0.715, 0.04, 0.27, 0.845]); axp.axis("off")
    axp.add_patch(Rectangle((0, 0), 1, 1, transform=axp.transAxes, facecolor="white",
                            edgecolor="#e3e8ee", lw=1, zorder=0))
    axp.text(0.06, 0.945, "已上图", fontsize=13, fontweight="bold", color="#1c242e")
    y = 0.888
    n_items = max(len(numbered), 1)
    tight = n_items > 8                     # 条目多时压缩行高与字号，避免叠字
    step = (0.054 if tight else 0.092)
    step = min(step, (0.68 if tight else 0.60) / n_items)
    for idx, name, no, st, en, raw in numbered:
        axp.add_patch(Circle((0.085, y), 0.017 if tight else 0.019, transform=axp.transAxes,
                             facecolor="#b3202c", edgecolor="none"))
        axp.text(0.085, y, str(idx), transform=axp.transAxes, color="white",
                 fontsize=7.4 if tight else 8.5, fontweight="bold", ha="center", va="center")
        ty = y + (0.010 if tight else 0.012)
        axp.text(0.132, ty, f"{name} 关闭", transform=axp.transAxes,
                 fontsize=8.6 if tight else 9.4, color="#1c242e")
        axp.text(0.905, ty, f"{icao} {no}", transform=axp.transAxes, fontsize=6.6,
                 color="#8b96a2", ha="right")
        line2 = (f"{icao} {no} · {utc_str(st, True)} – {utc_str(en, True)}" if span_days(st, en) > 3
                 else utc_span(st, en))
        axp.text(0.132, y - (0.004 if tight else 0.006), line2, transform=axp.transAxes,
                 fontsize=6.7 if tight else 7.2, color="#77828f")
        # 灰色小字：通告原文（最多两行）
        width = 76 if tight else 46
        wrapped = textwrap.wrap(raw or "（无原文）", width)
        lines = wrapped[:2]
        if len(wrapped) > 2:
            lines[-1] = lines[-1][:width - 3] + "…"
        for k, ln in enumerate(lines):
            axp.text(0.132, y - (0.016 if tight else 0.024) - k * (0.0118 if tight else 0.0145),
                     ln, transform=axp.transAxes, fontsize=6.2 if tight else 6.6, color="#a6b0bb")
        y -= step

    if unmatched:
        y -= 0.012
        axp.plot([0.06, 0.94], [y, y], transform=axp.transAxes, color="#e3e8ee", lw=1)
        y -= 0.030
        axp.text(0.06, y, f"未上图（{len(unmatched)} 条，要素缺失）", fontsize=9.6,
                 fontweight="bold", color="#8a6d1f")
        y -= 0.028
        for tag, no, why in unmatched:
            axp.add_patch(Rectangle((0.062, y - 0.007), 0.013, 0.013, transform=axp.transAxes,
                                    facecolor="#e0b23a", edgecolor="none"))
            axp.text(0.098, y + 0.002, tag, transform=axp.transAxes, fontsize=8.4, color="#6a5a2e")
            axp.text(0.098, y - 0.018, f"{no} · {why}", transform=axp.transAxes, fontsize=7.0, color="#9a916f")
            y -= 0.040

    fig.text(0.015, 0.012,
             "数据：FAA NOTAM Search · X-Plane 滑行网络 · FAA AM 滑行道面 · OurAirports 跑道　|　"
             "非官方航图，此图只作参考，不作为飞行或放行依据", fontsize=7.2, color="#9aa4af")
    now_cst = datetime.utcnow() + timedelta(hours=8)
    fig.text(0.985, 0.012, f"生成于 {now_cst.strftime('%Y-%m-%d %H:%M')} 北京时（UTC+8）",
             fontsize=7.2, color="#9aa4af", ha="right")

    if os.environ.get("CHART_DEBUG"):        # 需要核对排版时：CHART_DEBUG=1 跑，落点写 json
        try:
            json.dump({"badges": BADGES, "labels": LBL_DBG, "xlim": list(ax.get_xlim()),
                       "ylim": list(ax.get_ylim()), "pos": list(ax.get_position().bounds),
                       "fig": [fig.get_size_inches()[0], fig.get_size_inches()[1]], "dpi": fig.dpi,
                       "lead": LEAD},
                      open("/tmp/chart_badges.json", "w"), ensure_ascii=False)
        except Exception:
            pass
    print(f"  引线 {LEAD[0]} 根 / 编号贴住关闭段未画线 {LEAD[1]} 个（共 {len(BADGES)} 个编号）"
          f"　落点：外环 {STAT['ring']} · 近贴 {STAT['near']} · 网格 {STAT['grid']} · 硬放 {STAT['hard']}")
    fig.savefig(out_png, facecolor=fig.get_facecolor())
    plt.close(fig)
    return numbered

# ---------------------------------------------------------------- 任务
def run_task(icao, mode="fetch", text="", proxy=None, at=None, log=print):
    icao = (icao or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{4}", icao):
        raise RuntimeError("请输入 4 位 ICAO 代码，例如 KJFK、KLAX、PANC")
    log(f"== {icao} ==")
    if mode == "paste":
        rows = parse_pasted(text)
    else:
        rows = fetch_notams(icao, proxy, log)
    log(f"  通告 {len(rows)} 条")
    objs = parse_closures(rows, log)
    if at:
        # at 即航班预计到达时间，按 UTC 解释；只保留与 [ETA-6h, ETA+6h] 有交集的通告
        t = datetime.strptime(at, "%Y-%m-%d %H:%M")
        lo, hi = t - timedelta(hours=6), t + timedelta(hours=6)
        keep = []
        for o in objs:
            try:
                s0 = datetime.strptime(o["start"][:15], "%m/%d/%Y %H%M")
                e0 = datetime.strptime(o["end"][:15], "%m/%d/%Y %H%M")
            except Exception:
                keep.append(o); continue
            if s0 <= hi and e0 >= lo:
                keep.append(o)
        objs = keep
        log(f"  ETA {at}Z 前后 6 小时窗口内生效的通告：{len(objs)} 个")
    apt = get_apt(icao, proxy, log)
    nodes, edges = load_network(apt, log)
    if not nodes or not edges:
        raise RuntimeError("该机场的 apt.dat 里没有滑行网络，无法定位关闭段")
    runways = load_runways(icao, log)
    am = load_am_taxiways(icao, proxy, log)
    segments, unmatched, xy, *_ = solve(objs, nodes, edges, log, am=am)
    # 几何基准：两套底图数据各自的版本日期，直接印在图上，避免被误读为当日状态
    gparts = []
    d_apt = apt_meta(icao)
    if d_apt:
        gparts.append(f"滑行网络 {d_apt[:7]}（X-Plane 场景包）")
    if am:
        d_am = am_effective(proxy)
        if d_am:
            gparts.append(f"滑行道面 {d_am}（FAA AM）")
    geo_note = ("几何基准 · " + " ｜ ".join(gparts) + "　·　非当日数据，仅作示意") if gparts else ""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(OUTDIR, f"{icao}_{stamp}.png")
    numbered = render(icao, objs, segments, unmatched, runways, nodes, edges, xy, out,
                      at_label=(f"目标 ETA {at}Z，取 ±6 小时" if at else None), geo_note=geo_note,
                      am=am)
    return dict(image=os.path.basename(out), items=[
        dict(idx=i, name=n, no=no, start=utc_str(s, span_days(s, e) > 3),
             end=utc_str(e, span_days(s, e) > 3),
             span=("长期" if span_days(s, e) > 3 else "短时"))
        for i, n, no, s, e, _raw in numbered], unmatched=[dict(tag=t, no=n, why=w) for t, n, w in unmatched],
        total=len(objs))

# ---------------------------------------------------------------- Web
PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>机场关闭图生成器</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#f4f6f8;color:#1c242e}
header{background:#1e2733;color:#fff;padding:18px 28px}
header h1{margin:0;font-size:19px;font-weight:600;letter-spacing:.5px}
header p{margin:5px 0 0;font-size:12.5px;color:#9aa7b6}
.wrap{max-width:1180px;margin:22px auto;padding:0 20px}
.card{background:#fff;border:1px solid #e3e8ee;border-radius:10px;padding:20px 22px;margin-bottom:18px}
label{font-size:13px;color:#4c5866;display:block;margin-bottom:6px;font-weight:600}
input,textarea,select{width:100%;border:1px solid #d5dce4;border-radius:7px;padding:9px 11px;font-size:14px;font-family:inherit}
input:focus,textarea:focus{outline:none;border-color:#b3202c}
.row{display:flex;gap:14px;flex-wrap:wrap}
.row>div{flex:1;min-width:170px}
textarea{height:110px;resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.btn{background:#b3202c;color:#fff;border:none;border-radius:8px;padding:11px 26px;font-size:14.5px;
     font-weight:600;cursor:pointer;margin-top:16px}
.btn:hover{background:#941a25}.btn:disabled{background:#c8ccd2;cursor:wait}
.tabs{display:flex;gap:8px;margin-bottom:14px}
.tab{padding:7px 16px;border:1px solid #d5dce4;border-radius:7px;font-size:13px;cursor:pointer;background:#f8fafc}
.tab.on{background:#1e2733;color:#fff;border-color:#1e2733}
#log{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#5c6875;white-space:pre-wrap;
     background:#f8fafc;border:1px solid #eef1f5;border-radius:7px;padding:12px;max-height:190px;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #eef1f5}
th{color:#6b7684;font-weight:600;font-size:12px}
td .n{display:inline-block;width:20px;height:20px;line-height:20px;text-align:center;background:#b3202c;
      color:#fff;border-radius:50%;font-size:11px;font-weight:700}
img{width:100%;border-radius:8px;border:1px solid #e3e8ee;margin-top:12px;cursor:zoom-in}
.warn{color:#8a6d1f;font-size:12.5px;background:#fdf6e3;border:1px solid #f0e2bd;padding:9px 12px;border-radius:7px;margin-top:10px}
.hint{font-size:12px;color:#8a939f;margin-top:7px}
</style></head><body>
<header><h1>机场关闭图生成器</h1>
<p>输入 ICAO → 读通告 → 在机场图上精确标出被关闭的跑道与滑行道段</p></header>
<div class="wrap">
<div class="card">
  <div class="tabs"><div class="tab on" data-m="fetch">自动抓取通告</div><div class="tab" data-m="paste">粘贴通告文本</div></div>
  <div class="row">
    <div><label>机场 ICAO</label><input id="icao" value="KJFK" placeholder="KJFK / KLAX / PANC" maxlength="4"></div>
    <div><label>目标时间（UTC，航班预计到达）</label><input id="at" placeholder="2026-11-19 14:00"></div>
    <div><label>HTTP 代理（可选）</label><input id="proxy" placeholder="http://127.0.0.1:7890"></div>
  </div>
  <div id="pastebox" style="display:none;margin-top:14px">
    <label>通告文本（每条以编号开头，如 11/292）</label>
    <textarea id="text" placeholder="11/292 TWY Y BTN TWY G AND TWY H CLSD 11/19/2026 1215 11/19/2026 2300"></textarea>
  </div>
  <button class="btn" id="go">生成标注图</button>
  <div class="hint">目标时间按 UTC 填写，一般是航班的 ETA；只保留 ETA 前后 6 小时内生效的关闭通告。留空则不限时间。首次运行会下载该机场滑行网络（约 0.3–1 MB），之后走本地缓存。</div>
  <div id="log" style="margin-top:14px">待运行…</div>
</div>
<div class="card" id="result" style="display:none">
  <label>标注结果</label>
  <table id="tbl"><thead><tr><th>#</th><th>要素</th><th>通告</th><th>起（UTC）</th><th>止（UTC）</th><th>性质</th></tr></thead><tbody></tbody></table>
  <div id="unm"></div>
  <img id="img" onclick="window.open(this.src)">
</div>
</div>
<script>
let mode="fetch";
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("on"));t.classList.add("on");
  mode=t.dataset.m;document.getElementById("pastebox").style.display=mode==="paste"?"block":"none";
});
document.getElementById("go").onclick=async()=>{
  const b=document.getElementById("go");b.disabled=true;b.textContent="处理中…";
  const logEl=document.getElementById("log");logEl.textContent="开始…";
  try{
    const r=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({icao:document.getElementById("icao").value,mode:mode,
        text:document.getElementById("text").value,proxy:document.getElementById("proxy").value,
        at:document.getElementById("at").value})});
    const j=await r.json();
    if(!j.ok){logEl.textContent=(j.log||"")+"\\n[失败] "+(j.error||"未知错误");return;}
    logEl.textContent=j.log;
    const tb=document.querySelector("#tbl tbody");tb.innerHTML="";
    j.items.forEach(it=>{tb.innerHTML+=`<tr><td><span class="n">${it.idx}</span></td><td>${it.name}</td>
      <td>${it.no}</td><td>${it.start}</td><td>${it.end}</td><td>${it.span}</td></tr>`});
    const un=j.unmatched.length? `<div class="warn">未上图 ${j.unmatched.length} 条：`+
      j.unmatched.map(u=>`${u.tag}（${u.no}，${u.why}）`).join("；")+`</div>`:"";
    document.getElementById("unm").innerHTML=un;
    document.getElementById("img").src="/output/"+j.image;
    document.getElementById("result").style.display="block";
  }catch(e){logEl.textContent="请求失败："+e;}
  finally{b.disabled=false;b.textContent="生成标注图";}
};
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/output/"):
            fn = os.path.basename(urllib.parse.unquote(self.path))
            p = os.path.join(OUTDIR, fn)
            if os.path.exists(p):
                self._send(200, open(p, "rb").read(), "image/png")
            else:
                self._send(404, b"not found", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/run":
            self._send(404, b"{}"); return
        ln = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(ln) or b"{}")
        lines = []
        try:
            res = run_task(req.get("icao", ""), req.get("mode", "fetch"),
                           req.get("text", ""), (req.get("proxy") or "").strip() or None,
                           (req.get("at") or "").strip() or None,
                           log=lambda m: lines.append(m))
            body = dict(ok=True, log="\n".join(lines), **res)
        except Exception as e:
            body = dict(ok=False, log="\n".join(lines), error=str(e))
        self._send(200, json.dumps(body, ensure_ascii=False).encode("utf-8"))

class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

def main():
    httpd = None
    for port in range(PORT, PORT + 12):          # 端口被占用就顺延，避免启动失败
        try:
            httpd = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        print("没有可用端口（8765-8776 全被占用）")
        sys.exit(1)
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"服务已启动：{url}")
    print("关闭窗口或按 Ctrl+C 结束")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    with httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass

if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        icao = sys.argv[1].upper()
        try:
            r = run_task(icao)
            print("完成：", os.path.join(OUTDIR, r["image"]))
        except Exception as e:
            print("失败：", e)
            sys.exit(1)
    else:
        main()
