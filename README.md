# 机场关闭区域图（Airport Closure Chart）

把机场的**跑道关闭 / 滑行道分段关闭**通告（NOTAM）自动画到机场图上：
「TWY R BTN TWY H AND TWY G CLSD」这类通告，程序会定位到 R 滑行道 H 与 G 之间的**那一段**并标红，而不是整条道刷红。

输入 ICAO 代码，输出一张 PNG + 一个本地网页。

```
python3 airport_closure_app.py KJFK
```

## 特点

- **免密钥取数**：FAA NOTAM Search 公开接口，无需 API Key（需 `curl_cffi` 绕 TLS 指纹，见下文）
- **分段落段**：滑行道关闭按通告里的两个边界要素求网络最短路，只标被关的那一段
- **三级兜底**：X-Plane 滑行网络最短路 → FAA AM 官方滑行道面片 → 黄条提示
- **编号不遮挡**：编号、时间框、滑行道字母三层避让，编号与关闭段之间用细红线 + 起点小圆点连接
- **UTC / ETA 过滤**：目标时间填航班 ETA（UTC），只保留与 `[ETA-6h, ETA+6h]` 有交集的通告

## 快速开始

```bash
pip install -r requirements.txt
python3 airport_closure_app.py KJFK          # 命令行出图
python3 airport_closure_app.py               # 启动本地网页（127.0.0.1:8765，自动开浏览器）
```

Windows 下可直接双击 `启动-网页版.bat`；`打包exe.bat` 用 PyInstaller 打单文件 exe。

## 数据源

| 数据 | 来源 | 性质 |
|---|---|---|
| 通告 | `https://notams.aim.faa.gov/notamSearch/search`（POST，免密钥） | 官方，实时 |
| 滑行网络几何 | X-Plane Scenery Gateway `apt.dat`（1201 节点 / 1202 边） | 社区维护，非当日 |
| 滑行道面片 | FAA `AM_Taxiway` ArcGIS FeatureServer | 官方，季度更新 |
| 跑道 | OurAirports `runways.csv` | 公开聚合 |
| 航图参考 | FAA d-TPP `AD`（PDF） | 官方，28 天周期 |

**为什么必须用 `curl_cffi`**：FAA 前置 Akamai 按 TLS/JA3 指纹判定机器人，普通 `requests` 直连会稳定返回 403，即使浏览器能正常打开。代码里用 `curl_cffi.requests` 模拟 Chrome 指纹即可。

## 目录

```
airport_closure_app.py     主程序（取数 + 解析 + 落段 + 渲染 + 本地网页）
notam_fetch.py             通告取数
make_chart_v2.py           早期出图脚本
make_kjfk_closure_chart.py 早期 KJFK 专用脚本
make_timeline.py           时间轴图
re_parse.py                解析核对
make_official_chart.py     航图配准实验（未收敛，勿用于生产）
cache/                     机场几何缓存（apt.dat / AM 面片 / 跑道表）
docs/                      数据源调研、风险分析、完整技术方案
data/                      通告原始 JSON 样本
```

## 精度边界

- 滑行道几何来自社区场景包，**不是当日数据**，图上已标注「几何基准 YYYY-MM」和「非当日数据，仅作示意」
- 通告里点名但网络/面片都没有的滑行道，会进「未定位」列表并在标题行给出条数，不会硬编号
- 跑道某一段关闭（`RWY 04R/22L BTN TWY A AND TWY B CLSD`）按滑行道截段处理

## 免责声明

**此图只作参考，不作为飞行或放行依据。** 非官方航图，不可替代 AIP / d-TPP / 机组放行资料。
数据来自公开渠道，几何与通告均可能存在滞后，任何运行决策请以官方原始通告和现行航图为准。

## 许可

仅供内部研究与运行辅助参考，请勿用于实际飞行导航。
