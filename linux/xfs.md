# XFS 文件系统机制与删除恢复判读

本文件帮你做两件事：①在 XFS 上跑 VastBase/Oracle 时理解磁盘结构、地址编码与删除语义
（误删/drop 后判断恢复窗口与路径）；②判读 xfs_db/xfs_io 输出不被单位与字节序坑。
实现级深度（结构体逐字段、恢复算法全码）在 skill `xfs-recovery`，本文收运维判读面。

## 结构速查（AG 中心）

- **AG = 独立管理单元**：每 AG 自带 SB(sector 0)/AGF(sector 1)/AGI(sector 2)/AGFL
  (sector 3)（`XFS_SB_DADDR..XFS_AGFL_DADDR`，xfs_format.h:466-689）；典型 4GB=1 AG
- **空闲空间双 btree**：BNO（按块号排序）+ CNT（按大小排序），root 在 AGF
  （`agf_bno_root/agf_cnt_root`）；记录 `ar_startblock/ar_blockcount`
- **AGFL = 环形队列**：`agf_flfirst→agf_fllast` 可 wrap 到 0；V5 跳过 36B 头读 be32
  数组，容量=(sectsize−36)/4≈119（V4 无头 128）
- **全部大端 `__be*`**——与 VastBase/PG 页（小端）相反，读 XFS 元数据必须
  `be*_to_cpu()`；CRC 字段例外是 `__le32`
- **V4/V5（CRC）差异**（读错全偏）：

| 项 | V4 | V5 |
|---|---|---|
| btree block header | 16B | **56B**（不是 48） |
| AGFL | 无头直接数组 | 36B 头（magic 'XAFL'） |
| btree magic | ABTB/ABTC | AB3B/AB3C 等 |

## 地址编码（最容易错的一层）

- **FSBNO = `agno << sb_agblklog | agbno`**——agblklog=ceil(log2(agblocks))；
  agblocks 非 2 的幂时（实测 2693632→agblklog=22）**除法/取模必错**，只能位移
- 字节偏移 = `(agno*agblocks+agbno) * blocksize`；AG 头按 **sector(512B)** 不按块
- `xfs_io fiemap/bmap` 的 BLOCK-RANGE 列是 **512B sector 号**（÷8 才是 4K 块），
  AG-OFFSET 列才是块号——两列单位不同

## 删除语义三阶段（恢复窗口的机制根源）

```
Phase 1 unlinked list:  di_mode=原值 di_nlink=0 di_nextents=原值 BMX 完整   ← 易
Phase 2 truncate+ifree: di_mode=原值(不清零!) di_nextents=0 BMX 字节残留   ← 中
Phase 3 reclaim:        di_mode=0 BMX 可能被覆盖                            ← 难
```

关键机制：`xfs_iflush_fork` 只在 `if_bytes>0` 时写数据 fork——truncate 后 if_bytes=0
**不写=磁盘旧 BMX 原样保留**；所以 `di_nextents=0 ≠ BMX 被清零`。扫描过滤器必须用
`nlink==0 && nextents==0`，**不能用 mode==0**（Phase 2 的 mode 保持原值，会漏掉恢复
概率最高的一批）。VastBase DROP TABLE=ftruncate(0)+unlink，正好落这条链。

## 恢复路径五级（按代价排序）与实测状态

1. AGFL 环形队列扫描（最近释放块，最快）
2. BNO/CNT btree 遍历（全部空闲 extent）
3. RMAP btree 反查（物理块→owner inode；XFS_RMAP_OWN_* 特殊 owner）
4. 日志 EFI 解析（extent free intent 记录即将释放的 extent）
5. 全盘碎片扫描（兜底）

实测锚：①②+inode 扫描 156 真机验证（2026-09-30，真实 rm 目录恢复闭环）；**free-scan
级从未真机实测（挂账）**；VastBase 页识别（40B 头/pd_lower≥40/version=6）见 skill
`vastbase-parse-page-tuple`。

## 判读工具

```bash
xfs_db -c "sb 0" -c p /dev/sdX          # superblock
xfs_db -c "agf 0" -c p /dev/sdX         # AGF（flfirst/fllast/freeblks）
xfs_db -c "agfl 0" -c p /dev/sdX        # AGFL
xfs_db -c "freesp -d" /dev/sdX          # 空闲空间分布
xfs_io -r -c 'fiemap' <datafile>        # 文件物理布局（BLOCK-RANGE=sector!）
```

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| btree 内部节点 ptr 偏移按 `numrecs*key_size` | 指针区固定在 `maxrecs*key_size` 之后（xfs_alloc_btree.h PTR_ADDR 宏） | 204 实测修复（2026-09-28） |
| leaf 里先跳 keys 再读 records | leaf 只有 records，internal 才有 keys+ptrs | rec 从 header 后直接开始 |
| V5 btree header 按 48B 算 | CRC 版 56B（blkno/lsn/uuid/owner/crc 全在内） | 按 magic 分支 V4=16/V5=56 |
| `fsbno * blocksize` 直接算偏移 | FSBNO 是 agblklog 位域编码 | `agno=fsbno>>agblklog; agbno=fsbno&mask` |
| fiemap 的 BLOCK-RANGE 当块号 | 那是 512B sector 号 | ÷8（4K 块）再算偏移 |

## 版本注记

V4/V5（CRC）两代并存，判读先看 `sb_versionnum`/magic 分支；源码基线 linux fs/xfs
+ xfsprogs（本机 /home/xiang/xfsprogs-dev/）；VastBase 侧页格式见 parse-page-tuple。

## 来源

- vbdul 战役（2026-05~10）：XFS 恢复全链设计与真机验证（156 rm 目录闭环
  2026-09-30、FSBNO 编码修复、btree ptr 偏移修复 204 实测、inode 三阶段语义
  2026-05-23 验证）；细节存 memory `xfs_*` 系列
- skill `xfs-recovery`（结构体逐字段 + 恢复五级算法全量，源码行号引用）
- 内核 fs/xfs/libxfs/xfs_format.h、xfs_alloc.c；xfsprogs db/freesp.c
