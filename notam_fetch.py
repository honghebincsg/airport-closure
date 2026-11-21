#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FAA NOTAM Search 抓取示例（免密钥）
用法: python notam_fetch.py KJFK KLAX KORD

说明:
- 接口为 FAA NOTAM Search 页面内部使用的 POST /notamSearch/search
- 支持分页 offset/limit，返回 totalNotamCount 便于翻页
- 必须从能正常访问 notams.aim.faa.gov 的出口运行（部分云主机/代理会返回 502）
- 属"信息性"数据，不可作为放行或运行决策的唯一依据
"""

import json
import sys
import time

import requests

BASE = "https://notams.aim.faa.gov/notamSearch/search"
HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://notams.aim.faa.gov/notamSearch/nsapp.html",
}


def fetch_one(icao: str, page_size: int = 30, sleep: float = 0.4):
    session = requests.Session()
    rows, offset, total = [], 0, None
    while True:
        body = f"searchType=0&designatorsForLocation={icao}&offset={offset}&limit={page_size}"
        resp = session.post(BASE, data=body, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        total = data.get("totalNotamCount", 0)
        batch = data.get("notamList") or []
        rows.extend(batch)
        offset += page_size
        if offset >= total or not batch:
            break
        time.sleep(sleep)
    return total, rows


def parse(icao: str, raw: list):
    out = []
    for n in raw:
        msg = (n.get("icaoMessage") or "").replace("\r", "\n")
        qline = next((ln for ln in msg.split("\n") if "Q)" in ln), "")
        qcode = qline.split("/")[1].strip() if qline.count("/") > 1 else ""
        e_text = msg.split("E)", 1)[1].strip().replace("\n", " ") if "E)" in msg else ""
        out.append({
            "icao": icao,
            "notam_no": n.get("notamNumber"),
            "feature": n.get("featureName"),
            "source": n.get("source"),
            "qcode": qcode,
            "issued": n.get("issueDate"),
            "start": n.get("startDate"),
            "end": n.get("endDate"),
            "text": e_text[:400],
        })
    return out


def main():
    locs = sys.argv[1:] or ["KJFK"]
    all_rows = []
    for loc in locs:
        total, raw = fetch_one(loc.upper())
        print(f"{loc.upper()}: total={total} fetched={len(raw)}")
        all_rows.extend(parse(loc.upper(), raw))
    out_name = "notam_" + "_".join(l.upper() for l in locs) + ".json"
    with open(out_name, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=1)
    print("saved", out_name, len(all_rows), "rows")


if __name__ == "__main__":
    main()
