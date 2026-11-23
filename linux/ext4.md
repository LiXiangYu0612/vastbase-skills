# ext4 文件系统机制与删除恢复判读

本文件帮你做两件事：①在 ext4 上跑 VastBase/Postgres 时理解组结构、位图与删除语义
（误删/drop 后判断恢复路径与窗口）；②判读 dumpe2fs/debugfs/jbd2 输出不被单位与
字节序坑。vbdul 侧实现（ext4_free.dul/扫描器）以本文机制为准。

## 结构速查

- **block group 中心**：每组 = superblock 副本（首组 SB 在 **byte 1024**）+ GDT +
  block bitmap + inode bitmap + inode table + 数据块
- **单位 = 文件系统块（常用 4096）**，全链路无扇区混淆：block_size=
  `1024<<s_log_block_size`（位移不是乘法）；物理偏移=`block*block_size`；
  唯一 512B 单位字段是 inode 的 i_blocks（恢复不用）
- **extent（ext4_extent/i_block 里的 extent tree）** 是现代默认；旧 indirect block
  文件仍在老盘存在，解析需分派
- **全小端**——与 XFS 元数据（大端）相反，与 VastBase/PG 页一致

## 空闲管理（free.dul 的机制基础）

- 空闲 = block bitmap **bit=0**；组描述符 bg_free_blocks_count 汇总
- **INODE_UNINIT 占位组**（从没建过 inode 的组）：磁盘位图是全 1 占位符，kernel
  在 sb 里算作全 free——这就是「free.dul 求和(6.35M) ≠ sb.free_blocks(25.2M) 的
  18.8M 缺口」的来源；这些组**从未存过文件，无数据可恢复，不需要扫**（实测 739 组）
- **完整性验证法**：位图 bit=0 求和 vs ext4_free.dul 求和对账（实测 6,351,118 vs
  6,348,828，差=实时波动）→ 删除的数据块必然 bit=0 → **含数据的 free 块 100% 在
  free.dul，不漏**（2026-06-13 v0 实测）

## ★ 删除语义与恢复正道（与 XFS 相反的核心差异）

| | XFS | ext4 |
|---|---|---|
| 删除时 inode 的 extent 信息 | **BMX 字节残留**（iflush 只在 if_bytes>0 写） | **extent 条目整个清零**（实测 truncate 后 i_block 全 0） |
| inode→extent 恢复路 | 可行（三阶段，见 `xfs.md`） | **死路**（无残留可读） |
| journal 路 | EFI 补充线索 | **活动 fs 上不可靠**：inode 复用（hot group 最新副本是别人的文件）+ extent 清零无 fallback；`debugfs lsdel` 也返回 0 |
| **恢复正道** | 空闲结构扫描+BMX 残留 | **free block 内容扫描**（页面签名雕刻），依赖 ext4_free.dul 完整 |

## journal（JBD2）判读要点

- **字节序双层**：jbd2 结构（header/superblock/tags/revoke）= **大端**；journal 数据
  块里的 ext4 inode 数据 = **小端**（磁盘实测 magic `c0 3b 39 98` 大端定案）
- 常量必须对内核 jbd2.h：`JBD2_FEATURE_INCOMPAT_64BIT=0x2`（**0x4 是
  ASYNC_COMMIT，写错会把 16B tag 当 8B 解析**）；tag flags：SAME_UUID=2（无此位
  则 tag 后跟 16B UUID 要跳过）、LAST_TAG=8
- tag：64bit=16B（blocknr/blocknr_high/flags/checksum），数据块位置=
  descriptor_jpos+1+tag_idx（与 UUID 无关）
- 权威对照：`dumpe2fs -h`（sb/bg）+ `echo 'logdump -a' | debugfs /dev/vdX`
  （事务 descriptor→fs block 映射）

## 扫描架构与实测（vbdul 战役定案）

- **读粒度 1MB batch + 4K 自适应步进**（镜像 XFS）：命中页 `off+=BLCKSZ`（跳 8K
  消费整页），未中 `off+=block_size`（滑 4K 试下一对齐）；batch 间重叠 1 块防跨界漏
- **★ odd-align 教训**：VB/PG 页是 8K **文件对齐**，relfilenode 起始 fs 块为奇数时
  页落在奇数设备偏移——只在偶数 8K 边界读会**漏掉约一半页**（实测 block 1460537
  修前不在结果、修后在）
- **端到端 300/300**（2026-06-14，v0 /dev/vda1）：建表插 300 行 DROP →
  free 扫描 → extract 全恢复；链路共修 4 处（命令解析 strcat 溢出 / extent+batch
  架构 / odd-align / pg_type_map 缺 SQL 别名致真 tuple 校验被删）
- **性能天花板 = I/O-bound**：单 HDD 裸读 113MB/s、扫描有效 77MB/s；并行与 batch
  都不提速（瓶颈=盘带宽+万级碎片 extent 的 seek），要快换 SSD/RAID
- **铁律：输出绝不能写被扫描的设备**（st_dev 同设备告警；实测输出污染曾造成
  7000+ 页假差异）

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 拿 journal/ext4_deleted_ext.dul 当恢复主路 | inode 复用+extent 清零，活动 fs 上不可靠 | free 块内容扫描是正道 |
| 扫描只按偶数 8K 边界 | 8K 文件对齐遇奇起始 fs 块全漏 | 4K 自适应步进 |
| 只跑 parallel 模式 | 单线程路径曾漏 pg_drop 分类（已修） | 两路同权验证 |
| 把 free.dul 求和≠sb.free 当漏扫 | 缺口=INODE_UNINIT 占位组（无数据） | 位图 bit=0 对账法核完整性 |

## 版本注记

extent 文件为主（2.6.23+）；老盘 indirect 文件分派处理；源码基线 linux fs/ext4 +
fs/jbd2（本机 /home/xiang/linux-master 镜像与 xfsprogs 同期分析）。

## 来源

- vbdul 战役（2026-06，v0=204 /dev/vda1 真机）：ext4 语义/位图完整性/odd-align/
  300 行端到端/JBD2 tag 修复全实测；细节存 memory `ext4_*`、`jbd2_*` 系列
- 内核 fs/jbd2/recovery.c:210-216,577-579（tag 解析）、include/linux/jbd2.h（常量）
- dumpe2fs/debugfs logdump 权威对照法（2026-06-13 验证 sb/bg 100% 匹配）
