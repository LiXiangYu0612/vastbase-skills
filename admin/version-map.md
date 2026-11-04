# 版本谱系与官方文档站地图

## 概述

> 服务分类：admin（版本对表/文档检索）。两块知识：①G100 版本谱系（哪些是
> LTS、哪些是 DMR）②官方文档站的**程序化抓取配方**（API 直取，无需浏览器）。

## 版本谱系（2026-10-25 实抓官方接口 + 2026-10-26 用户校准）

| 类型 | 版本 |
|---|---|
| **LTS** | V2.2.10、**V2.2.15**、V3.0.8、**V3.0.9（最新 LTS）** |
| DMR | V2.2.11~14、V2.2.16~18 |

**差异记录（官方站元数据不可全信）**：docVersionTree 接口把 V2.2.15 的
versionType 标为 DMR——**与产品事实不符，2.2.15 实为 LTS**（2026-10-26
用户校准入库）。教训：站端元数据只当线索，版本定级以产品发布事实为准。

**谱系外 build（2026-11-02 复抓+实测）**：现网存在 **V2.2.19**
（`vb_version()` 串 "V2.2 (Build 19) Release compiled at 2026-01-19"，
env-W 现场机）——官方版本树**无此号**（树里 2.2 线止于 2.2.18 后接
V3.0.8）；编译日期晚于 V3.0.8 本尊（2025-12-07），= 2.2 老线在 3.0.8
发布后的平行延续补丁。**≠ 3.0.8**（行为佐证：gs_stack 在 3.0.8 全链
正常、在 2.2.19 空返回）。版本串规则：`V<x.y> (Build N)` 的 N 即补丁号
（V3.0 Build 8 ↔ V3.0.8 实锚）。报版本对不上的现场，一律 `vb_version()`
原文对账，别信转述。

- 每版本在文档站只有一份"用户手册"（docType=1，安装升级指南形态）；
  其余手册（开发/管理/兼容/参考）是产品级不分版本
- 判读联动：交付核对按 LTS 档（`g100-standard-params.md` 的 3.0.8/3.0.9
  标准值正对两个 LTS）；现场报 DMR 版本时注意与 LTS 的差异未记录在
  标准表——按环境事实处理

## 官方文档站抓取配方（API 直取，2026-10-25 实证）

站点 https://docs.vastdata.com.cn 是 umi 单页应用（curl 只有 JS 壳），
但后端 API 裸奔可访问：

```
# ① 产品版本×文档树（版本谱系就是它给的）
curl 'https://docs.vastdata.com.cn/api-docCenter/productVersionDoc/docVersionTree?langType=zh_CN&productCode=VastbaseG100'
# ② 某版本手册的章节树（5MB 级，含全章节 uuid）
curl 'https://docs.vastdata.com.cn/api-docCenter/homePage/getDocMap/zh_CN/VastbaseG100/V3.0.9/1'
# ③ 章节正文（uuid 来自②）
curl 'https://docs.vastdata.com.cn/api-docCenter/homePage/getDocContent/zh_CN/VastbaseG100/V3.0.9/1/<章节uuid>'
```

页面路由规律：`/zh_CN/VastbaseG100/<版本>/<docType>/<章节uuid>`。
带浏览器的替代配方（WSL 直调 Windows Chrome）：
`"/mnt/c/Users/<user>/AppData/Local/Google/Chrome/Application/chrome.exe"
--headless=new --user-data-dir='C:\Temp\hl' --dump-dom <url>`。

## 已知问题清单：公开源不存在（负向实锤）

- 文档知识中心**没有"版本说明/已知问题"页面**（全树检索无命中；首页数据
  仅"版本"选择器与"安全公告"）——已知问题随**发布包/支持渠道**走，无公开
  爬取源
- 获取路径只剩：①随包发布说明（交付侧资产）②开源 openGauss gitee issue
  tracker（**近似参考**，基线近似原则同 ha/；匿名 API 可用但限流）
- 本条结论本身即判读知识：客户问"这是不是已知 bug"，查公开文档无用，
  直接走源码复现 + 交付渠道发布说明

## 常见错误

| 错误 | 纠正 |
|---|---|
| 去 docs 站找已知问题 | 不存在该栏目（本文件负向实锤） |
| curl 文档站拿正文 | SPA 壳，用上面的 API 三件套 |
| DMR 当 LTS 判读 | 标准表只锚 LTS 档 |
| 全信站端 versionType | 2.2.15 被站标 DMR、实为 LTS（差异记录） |

## 版本注记 / 来源

- 实抓 2026-10-25（接口原样返回 code:200；docVersionTree 全量 9.2KB）；
  版本谱系随官方更新会变，复抓①即得新谱

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`；互喂边：版本对表→
g100-standard-params（LTS 档）、文档站配方→本域持续学习（官方文档按需
查阅的执行路径）。
