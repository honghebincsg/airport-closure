#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KJFK 关闭对象的时间分布（甘特图）+ 按 2 小时切片的可用性清单"""
import json, re
from datetime import datetime, timedelta
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle

AI = "/home/user/Doubao/chats/38454828072541442/ai_parse.json"
RAW = "/home/user/kjfk_notam_raw.json"
OUT = "/home/user/Doubao/chats/38454828072541442/KJFK_关闭时间轴.png"

objs = json.load(open(AI))["objects"]
tt = {r["no"]: (r["start"], r["end"]) for r in json.load(open(RAW))["rows"]}

def parse(s):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2})(\d{2})", s or "")
    if not m: return None
    mo, da, yr, hh, mi = map(int, m.groups())
    return datetime(yr, mo, da, hh, mi) - timedelta(hours=5)   # → EST

# 只取本次能上图的 6 个对象（4 滑行道 + 2 跑道）
KEEP = {"TWY Q4", "TWY M", "TWY H", "TWY Y", "RWY 04R/22L", "RWY 04L/22R"}
rows = []
for o in objs:
    name = f"{o['kind']} {o['name']}" if o["kind"] == "RWY" else f"TWY {o['name']}"
    if name.replace("TWY ", "TWY ") not in KEEP: continue
    s, e = tt[o["no"]]
    rows.append(dict(label=name, no=o["no"], s=parse(s), e=parse(e)))
rows.sort(key=lambda r: r["s"])

plt.rcParams["font.family"] = ["Noto Sans CJK JP", "Noto Sans CJK SC", "DejaVu Sans"]
fig, ax = plt.subplots(figsize=(13, 6.2), dpi=200, facecolor="white")
ax.set_facecolor("white")

xmin = min(r["s"] for r in rows) - timedelta(hours=8)
xmax = max(r["e"] for r in rows) + timedelta(hours=8)

# 今天（快照日）高亮
today = datetime(2026, 11, 19)
ax.axvspan(today, today + timedelta(days=1), color="#fff3d6", zorder=0)


for i, r in enumerate(rows):
    ax.barh(i, (xmax - xmin).total_seconds() / 86400, left=xmin, height=0.42,
            color="#eef1f5", edgecolor="#dde3ea", lw=0.6, zorder=1)
    ax.barh(i, (r["e"] - r["s"]).total_seconds() / 86400, left=r["s"], height=0.42,
            color="#d8202f", edgecolor="#a51622", lw=0.8, zorder=2)
    ax.text(r["e"] + timedelta(hours=2), i, f"{r['no']}", fontsize=7.5,
            color="#8a939f", va="center")
    d = (r["e"] - r["s"]).total_seconds() / 3600
    ax.text(r["s"] + timedelta(hours=1), i, f"{d:.0f}h", fontsize=7,
            color="white", va="center", fontweight="bold")

ax.set_yticks(range(len(rows)))
ax.set_yticklabels([r["label"] for r in rows], fontsize=9)
ax.invert_yaxis()
ax.set_xlim(xmin, xmax)
ax.xaxis.set_major_locator(mdates.DayLocator())
ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18]))
ax.grid(axis="x", which="major", color="#d8dee6", lw=0.8, zorder=0)
ax.grid(axis="x", which="minor", color="#eef1f5", lw=0.5, zorder=0)
for s in ("top", "right", "left"):
    ax.spines[s].set_visible(False)
ax.spines["bottom"].set_color("#c8d0d9")

# 快照时刻
now = datetime(2026, 11, 19, 10, 1)
ax.axvline(now, color="#2f3b45", lw=1.4, ls=(0, (4, 3)), zorder=3)
ax.text(now, -0.45, " 快照 11-19 10:01 EST ", fontsize=8, color="#2f3b45", ha="center",
        bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="none"))
ax.set_ylim(5.55, -0.78)

# 当天跑道全关区间
a = datetime(2026, 11, 19, 7, 0); b = datetime(2026, 11, 19, 14, 0)
ax.annotate("", xy=(b, 2.9), xytext=(a, 2.9),
            arrowprops=dict(arrowstyle="<->", color="#c1212e", lw=1.2))
ax.text(b + timedelta(hours=1.2), 3.45, " 07:00–14:00 两条 04 跑道同时关闭 ",
        fontsize=8.0, color="#c1212e", va="center",
        bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="none", alpha=0.85))

fig.text(0.012, 0.965, "KJFK 关闭对象的持续时间（EST）", fontsize=17, fontweight="bold", color="#1c242e")
fig.text(0.012, 0.905,
         "同一张图里的 6 个关闭，跨度从 7 小时到 9 天不等 —— 不标时间就会读成“此刻全关”",
         fontsize=9.5, color="#6b7684")
fig.text(0.012, 0.03,
         "数据：FAA NOTAM Search 2026-11-19 15:01Z · 时间已换算为纽约 EST = UTC−5",
         fontsize=7.5, color="#9aa4af")
plt.subplots_adjust(left=0.13, right=0.985, top=0.875, bottom=0.13)
fig.savefig(OUT, facecolor="white")
print("saved", OUT)

# ---- 按 2 小时切片，看每个时段的关闭状态 ----
print("\n11-19 各时段（EST）仍在关闭的对象：")
for h in range(0, 24, 2):
    t = datetime(2026, 11, 19, h)
    act = [r["label"] for r in rows if r["s"] <= t < r["e"]]
    print(f"  {h:02d}:00  " + ("、".join(act) if act else "无"))
