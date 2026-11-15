# 发布说明与核对单（公仓转移）

本文件面向**仓库维护者**：从内部环境转移到公开仓库前后的核对与说明。

## 发布前三件套（已完成，2026-10-25）

1. **`.claude-plugin/marketplace.json`** ✅——单插件模型（plugin `vastbase`，
   source `./`）；可注册为 marketplace 后 `/plugin install vastbase@vastbase-skills`
2. **真实样本脱敏** ✅——.gitignore 排除：`workspace/inbox/*`（现场原始报告，
   含实例名/主机/IP）、`workspace/*.xlsx`（内部标准化参数表）、
   `evals/fixtures/`（真实环境 AWR 样本）。**如需公开可跑的 evals，另做
   合成样本**（待办，非阻塞）
3. **本机路径泛化** ✅——正文中的源码树路径统一为占位符
   `<openGauss-6.0.0-src>` / `<linux-7.1-rc4-src>` / `<openGauss-CM-src>`；
   `scripts/verify_anchors.py` 支持环境变量
   `VASTBASE_OG_SRC` / `VASTBASE_LINUX_SRC` / `VASTBASE_CM_SRC` 自配三棵树

## 内容脱敏规约（持续执行）

- 内部环境一律代号化：`env-A`/`env-B`/…；案例库另有铁律见 `cases/README.md`
- 不出现：内网 IP、主机名、实例名、客户标识、凭证
- "客户端（client）"是技术术语不在此列；"客户样本"类表述一律写"现场样本"
- 上游公开源（gitee openGauss/CM、fanlv.fun 笔记、deepwiki）可正常引用

## 发布时的人工项（维护者拍板）

- [x] LICENSE——**2026-11-15 用户拍板：暂不加**（维持无 LICENSE 状态；将来要加再议）
- [x] **openGauss 字样口径——2026-11-15 用户拍板：仅源码基线引用可写**：
      正文描述用 VastBase/Postgres；"版本注记/来源"的基线声明处允许出现
      openGauss 字样（现状即此口径，维持不改）
- [x] 仓库名与 owner（已落位：github.com/LiXiangYu0612/vastbase-skills，
      2026-11-04 首发已推）
- [x] **`vastbase-sandbox` 是独立 skill 且含内网沙箱配置，不随本仓发布**；
      如要开源沙箱能力，单独剥离配置后另发
- [x] 首次 push 前复跑：`python3 scripts/check_graph.py`（六查全绿）+
      `python3 scripts/verify_anchors.py`（需要本地三棵源码树或环境变量）
- [x] README.md 门面核对（当前无内部信息）
- [x] 内部资料排除（crash-case-index gitignore / read-write-splitting
      出仓 2026-11-15 补真执行 / CM 工单号脱敏 / has-dcs 出处中性化——均闭合）

## 版本已知问题对照表（待办，非阻塞）

官方文档已知问题爬梳（docs.vastdata.com.cn，版本说明/已知问题页）未完成
——站点为 JS 渲染，需要带浏览器的方式抓取；ROADMAP 挂 ◻。
