#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正则解析 KJFK 通告里的关闭关系（对照组）"""
import json, re

RAW = "/home/user/kjfk_notam_raw.json"
OUT = "/tmp/re_parsed.json"

rows = json.load(open(RAW))["rows"]

def strip_tail(t):
    """去掉报文尾部粘连的垃圾：RSTRCTN>0? TO| 之类"""
    t = re.sub(r"[A-Z]*RSTRCTN>\S*(?:\s+\S+)*?$", "", t)
    t = re.sub(r">\S*(?:\s+\S+)*?$", "", t)
    t = re.sub(r"\s*TO\|$", "", t)
    return t.strip(" ,;")

BND = r"(?:(TWY|RWY|TERMINAL|RAMP|APRON)\s*)?([A-Z0-9]+(?:\s*RAMP)?|[0-9]{2}[LCR]?/[0-9]{2}[LCR]?)"
PAIR = re.compile(rf"^{BND}\s+AND\s+{BND}$")

CLOSE = re.compile(r"(TWY|RWY)\s+([A-Z0-9/]+)\s*(?:BTN\s+([^,]*?)\s*)?CLSD")

out = []
for r in rows:
    t = strip_tail(r["text"])
    if "CLSD" not in t:
        continue
    for m in CLOSE.finditer(t):
        kind, name, between = m.group(1), m.group(2), m.group(3)
        rec = dict(no=r["no"], kind=kind, name=name, start=r["start"], end=r["end"],
                   b1=None, b2=None, b1_type=None, b2_type=None,
                   evidence=t[:120])
        if between:
            p = PAIR.match(between.strip())
            if p:
                rec.update(b1=p.group(2), b1_type=p.group(1), b2=p.group(4), b2_type=p.group(3))
        out.append(rec)

json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1)
print("正则解析出关闭对象:", len(out))
for o in out:
    print(f"  {o['no']} {o['kind']} {o['name']:6s} "
          f"BTN {o['b1_type'] or ''} {o['b1'] or '-'} AND {o['b2_type'] or ''} {o['b2'] or '-'}")
