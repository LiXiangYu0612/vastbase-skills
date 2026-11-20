# Linux 页缓存核心机制（address_space → XArray → folio → LRU → 回写，源码定论）

> 面向 DBA 的页缓存数据结构主线：页缓存怎么组织、怎么查找、怎么驱逐、怎么回写。
> 判读层（预读调参/dirty 阈值/双缓存/refault）在 `io-storage.md` 和 `memory.md`，
> 本文只讲**底层怎么长这样**——读完能回答"为什么 /proc/meminfo 的 Cached 是
> 这个数""为什么 swappiness 偏 file""为什么 direct=1 绕的就是这一层"。
> 基线 v7.1-rc4 行级精读（2026-11-20）。

## 总图（一张图把所有部件串起来）

```
进程 read()/write()
  → VFS（file_operations）
    → address_space（每个文件一个，挂 inode）
        ├── i_pages: XArray（页索引树，页偏移→folio*）
        │      ├── PAGECACHE_TAG_DIRTY     ← 脏页标记（XA_MARK_0）
        │      ├── PAGECACHE_TAG_WRITEBACK ← 回写中标记（XA_MARK_1）
        │      └── PAGECACHE_TAG_TOWRITE   ← 待写标记（XA_MARK_2）
        ├── a_ops: address_space_operations（文件系统各自的实现）
        │      ├── read_folio / readahead     ← 读路径
        │      ├── writepages / dirty_folio   ← 写路径
        │      ├── invalidate_folio           ← 截断/失效
        │      └── direct_IO                  ← O_DIRECT 旁路
        └── nrpages / writeback_index         ← 计数与回写起点
  → folio（一个或多个连续物理页的抽象）
    └── lru: list_head（挂到 per-node LRU 双链）
         ├── LRU_INACTIVE_FILE  ← 页缓存冷端
         └── LRU_ACTIVE_FILE    ← 页缓存热端
```

## §1 address_space：页缓存的挂载点（fs.h:473）

**每个文件（inode）一个 address_space**——这是页缓存的"命名空间"：
同一文件的所有缓存页都挂在它的 `i_pages` 树上，`nrpages` 是当前缓存页总数。

```c
struct address_space {                          // include/linux/fs.h:473
    struct inode    *host;          // 所属 inode（即"哪个文件"）
    struct xarray   i_pages;        // ★ 核心底座：页偏移(index) → folio*
    gfp_t           gfp_mask;       // 分配缓存页的 GFP 标志
    unsigned long   nrpages;        // 当前缓存页数（/proc/meminfo Cached 的分项来源）
    pgoff_t         writeback_index;// 回写循环的起点（循环写模式）
    const struct address_space_operations *a_ops;  // ★ 操作函数表
    errseq_t        wb_err;         // 回写错误序列号（fsync 报错的来源）
    ...
};
```

**DBA 关注点**：
- `nrpages` 就是这个文件占了多少页缓存——`find /proc/*/smaps` 里
  某个 mmap 文件段的 `FilePss` 本质来自这里
- `wb_err` 是 errseq 机制：回写出过错后后续 fsync 会报错——库日志里的
  "I/O error writing to file" 可能源于之前某次异步回写失败

## §2 XArray：页索引树（fs.h:495 + xarray.h:355）

`i_pages` 是 XArray（6.x 后从 radix tree 迁移），按**页偏移（index）**
索引 folio 指针：

```
XArray:
  index 0    → folio* (page 0-3, 若 large folio)
  index 1    → folio* (page 4-7)
  index 4    → folio* (page 16-19)
  ...（稀疏，只存有缓存的页）
```

**XArray 的三个 mark（位标签）是页缓存状态管理的核心**（fs.h:495-497）：
```c
#define PAGECACHE_TAG_DIRTY     XA_MARK_0   // 页脏了，等回写
#define PAGECACHE_TAG_WRITEBACK XA_MARK_1   // 页正在回写中
#define PAGECACHE_TAG_TOWRITE   XA_MARK_2   // 标记需要写（tag_pages_for_writeback）
```

`mapping_tagged(mapping, PAGECACHE_TAG_DIRTY)` 快速判断"这个文件有没有脏页"
——回写决策的第一步就是扫这些 mark（`lookup_tag_pages` 批量拉出脏页集）。

**DBA 关注点**：`/proc/meminfo` 的 `Dirty` ≈ 全部 address_space 中带
DIRTY mark 的 folio 总量；`Writeback` ≈ 带 WRITEBACK mark 的 folio。

**查找路径**（filemap.c:1941）：
```c
struct folio *__filemap_get_folio_mpol(mapping, index, fgp_flags, gfp, pol)
    → xa_load(&mapping->i_pages, index)       // 无锁快查
    → 未命中 + FGP_CREAT → filemap_alloc_folio → filemap_add_folio
```

## §3 folio：现代页抽象（mm_types.h:401）

**folio = 一个或多个连续物理页的管理单元**（4KB 单页或 2M large folio）。
引入 folio 是为了让页缓存操作不必逐个 4KB page 处理。

```c
struct folio {
    memdesc_flags_t flags;
    union {
        struct list_head lru;        // ★ 挂 LRU 链
        struct { ... anon 的反向页表指针 ... };
    };
    struct address_space *mapping;   // ★ 指回所属文件（anon 页此位为空+低位标记）
    ...
};
```

**DBA 关注点**：
- 6.x 起 large folio（2M 连续缓存页）默认开启——**/proc/meminfo 的
  `Cached` 数量单位仍是"页"（4KB），与 folio 大小无关**
- `mapping` 指针区分：指向 address_space = **文件页（页缓存）**；
  低位置 1 = **匿名页**——vmscan 和 swappiness 决策就靠看这个

## §4 address_space_operations：文件系统的差异化接口（fs.h:401）

```c
struct address_space_operations {
    int   (*read_folio)(file, folio);           // 单页同步读（mmap 缺页）
    int   (*writepages)(mapping, wbc);          // ★ 回写一批脏页（fsync/fwrite 触发）
    bool  (*dirty_folio)(mapping, folio);       // 标脏（set_page_dirty 的现代形态）
    void  (*readahead)(ractl);                  // 预读入口（ext4→ext4_readahead）
    int   (*write_begin)(kiocb, mapping, ...);  // 写前准备（ allocating/reservation）
    int   (*write_end)(kiocb, mapping, ...);    // 写后提交（标脏+解锁）
    void  (*invalidate_folio)(folio, off, len); // 截断/失效时回调（释放 fs 私有数据）
    bool  (*release_folio)(folio, gfp);         // 回收前询问"能释放吗"（fs 私有引用清了才能走）
    ssize_t (*direct_IO)(kiocb, iter);          // ★ O_DIRECT 旁路（绕页缓存直达块层）
    int   (*migrate_folio)(mapping, dst, src, mode); // 内存整理/Compaction 迁移
    ...
};
```

**DBA 关注点**：
- `direct_IO` 是 **O_DIRECT 的唯一入口**——设了这个回调的文件系统才支持
  O_DIRECT（ext4/xfs 有；NFS 部分有；tmpfs 无 → O_DIRECT 到 tmpfs 会退回 buffered）
- `writepages` 是回写的多页批量接口——ext4 的延迟分配/多块分配在这里做，
  性能差异（单块 vs 多块合并）直接影响 iostat 的 w_iops
- `release_folio` 返回 false = 这个页不能回收（fs 还有引用，如 journal 未提交）——
  **内存紧张但 Cached 降不下来的原因之一**

## §5 LRU 双链：页缓存的驱逐机制（mmzone.h:386 + swap.c:506）

每个 NUMA node 维护 **5 条 LRU 链**（两维度：anon vs file × cold vs hot + unevictable）：

```c
enum lru_list {                    // include/linux/mmzone.h:386
    LRU_INACTIVE_ANON,             // 匿名页冷端（swap 候选）
    LRU_ACTIVE_ANON,               // 匿名页热端
    LRU_INACTIVE_FILE,             // ★ 页缓存冷端（首选驱逐对象）
    LRU_ACTIVE_FILE,               // ★ 页缓存热端
    LRU_UNEVICTABLE,               // 不可驱逐（mlock/shmem 锁定）
};
```

**页缓存页进入 LRU 的路径**（mm/swap.c:506）：
```
首次缓存命中（filemap_add_folio）
  → folio_add_lru(folio)              // swap.c:506
    → 加入 per-CPU folio_batch（延迟批量操作，减少锁争用）
      → 批量满 → lru_add_drain → 挂入 LRU_INACTIVE_FILE
```

**LRU 晋升/降级**（由 vmscan 的引用检测驱动）：
```
第二次被访问 → mark_page_accessed → 从 inactive 晋升 active
长时间未访问 → active 链老化扫描降回 inactive → 下轮可被驱逐
```

**vmscan 驱逐页缓存**（mm/vmscan.c，`shrink_folio_list`）：
```
scan LRU_INACTIVE_FILE
  → folio 有 DIRTY mark？→ 先回写（阻塞/非阻塞取决于 GFP）
  → folio 有 WRITEBACK mark？→ 跳过（正在写）
  → a_ops->release_folio 返回 true？→ 从 XArray 移除 → 释放页框
                                              ↑ 返回 false = 不能回收
```

**DBA 关注点**：
- **swappiness 的作用点**就在这里：vmscan 决定扫 anon 链还是 file 链的比例——
  `swappiness=60` 意味着驱逐 file 页（可重生）的倾向高于 anon 页
  （需换出）——`memory.md` swappiness 节的完整模型
- **页缓存"免费"但不无限**：file 页可随时丢弃（下次读再拉回来），
  所以 OS 优先驱逐 file 页——这就是 MemAvailable 远大于 MemFree 的原因
- `workingset_refault`（memory.md 已锚 workingset.c:546）= 驱逐后又被读回
  = **页缓存容量 < 工作集**的硬判据

## §6 读路径：从 read() 到预读（filemap.c + readahead.c）

```
read() → generic_file_read_iter
  → filemap_get_folio(mapping, index)          // filemap.c:1941
    → XArray xa_load 命中？→ 拷给用户 → 返回（"Cached"读，无 IO）
    → 未命中？
        → page_cache_sync_ra()                 // readahead.c:557
          → ra_pages 窗口判定（readahead.c:144,546 已锚 io-storage.md）
          → do_page_cache_ra()                 // readahead.c:315
            → filemap_alloc_folio + a_ops->read_folio 或 a_ops->readahead
            → folio_add_lru → 进入 LRU_INACTIVE_FILE
            → 唤醒等待进程
```

**io-storage.md 已锚的预读窗口机制**（ra_pages/`read_ahead_kb` 旋钮）不再重复。

## §7 写路径：标脏到回写（filemap.c + page-writeback.c）

```
write() → generic_file_write_iter
  → a_ops->write_begin(mapping, pos, len, &folio, ...)
      → 分配/获取 folio → 加入 XArray（filemap_add_folio）
  → 拷用户数据到 folio
  → a_ops->write_end(mapping, pos, len, copied, folio, ...)
      → filemap_dirty_folio(mapping, folio)    // page-writeback.c:2710
        → __folio_mark_dirty                   // :2670
          → XArray 设置 PAGECACHE_TAG_DIRTY    // ★ 脏标记在此刻打上
          → accounting（nr_dirty++ / zone stat）
  → 返回用户（★ 不等 IO！——这就是"写快"的原因）
```

**回写触发点**（io-storage.md 已铺三闸门，本文补数据结构侧）：
```
per-BDI flusher 线程唤醒
  → bdi_writeback_workfn
    → write_cache_pages(mapping, wbc)
      → lookup_tag_pages(PAGECACHE_TAG_DIRTY)  // ★ 从 XArray 按标记批量拉脏页
      → 逐 folio：设置 PAGECACHE_TAG_WRITEBACK
      → a_ops->writepages(mapping, wbc)         // ext4_writepages → 多块分配 → 块层
      → 完成后清除 DIRTY + WRITEBACK 标记
```

**fsync 与 errseq**（fs.h:506 `wb_err`）：
- fsync 等 address_space 的全部脏页写完（`filemap_write_and_wait_range`）
- 如果某次异步回写出过错，`wb_err` errseq 会记住——后续 fsync 返回
  该错误，**不会静默丢数据**

## §8 截断与失效：truncate/invalidatepath

```
truncate(f) → inode_operations->setattr
  → truncate_inode_pages_range(mapping, start, end)
    → 逐 index: filemap_get_folio
      → 若 DIRTY → 先回写
      → a_ops->invalidate_folio(folio, offset, len)  // fs 释放私有数据
      → filemap_remove_folio → 从 XArray 删除 → 从 LRU 摘除 → 释放
```

**DBA 关注点**：`DROP TABLE` / `TRUNCATE TABLE` 大表时，这个路径会
批量清 address_space 的全部页——如果页缓存中有大量脏页（比如刚做完
大批量写入），**truncate 也会触发回写风暴**（和 checkpoint 叠加）。

## §9 与库（VastBase/Postgres）的双缓存互动

已在 `memory.md` §页缓存与数据库 铺过双缓存结构，此处补数据结构侧：
- 库的 shared_buffers 是**用户态**数组（slockhash 管理的 buffer descriptor pool），
  **不经过 address_space/XArray**——除非用 `data_page_writeback` 类 GUC
  让库写走 OS 缓存（VB G100 默认 direct IO 绕页缓存）
- 库的**数据文件**读写如果走 buffered IO（部分场景如临时文件），
  OS 页缓存会缓存一份——XArray 里多了一棵以库文件 inode 为 host 的子树
- `O_DIRECT` 库（如 G100 默认）→ `a_ops->direct_IO` → 完全绕过本文件
  全部机制（不进 XArray、不进 LRU、不标脏）——**/proc/meminfo 的 Cached
  不会包含库数据文件页**

## 判读速查

| 现象 | 查什么 | 机制 |
|---|---|---|
| Cached 巨大但 MemAvailable 还行 | `free -g` available 列 | file 页可随时驱逐（§5） |
| Dirty 居高不下 | `vm.dirty_ratio` / `vm.dirty_background_ratio` | XArray DIRTY 标记堆积，回写闸门未触发（§7 + io-storage.md） |
| 页缓存驱逐后又被读回（IO 暴增） | `/proc/vmstat` workingset_refault | 工作集 > 页缓存容量（memory.md 已锚） |
| DROP TABLE 后 IO 飙升 | iostat w_iops 同窗 | truncate 路径强制回写全部脏页（§8） |
| O_DIRECT 时 Cached 不涨 | `open(...,O_DIRECT)` | direct_IO 回调绕过 XArray（§4/§9） |
| fsync 报 I/O error 但之前没报 | `wb_err` errseq 机制 | 之前某次异步回写失败被记住（§7） |

## 版本注记

- 基线 7.1-rc4 行级精读（2026-11-20）：fs.h:473/:495/:401（address_space/
  XArray tags/a_ops）、mm_types.h:401（folio）、mmzone.h:386（LRU 枚举）、
  filemap.c:1941（__filemap_get_folio_mpol）、readahead.c:315/:557（do_page_cache_ra/
  page_cache_sync_ra）、swap.c:506（folio_add_lru）、page-writeback.c:2670/:2710
  （__folio_mark_dirty/filemap_dirty_folio）
- large folio（>4KB 页缓存单元）6.x 默认开启；3.10 无 folio（仍用 page +
  radix tree）——**机制语义跨版本一致，结构名变了**（page→folio、radix→XArray）
- XArray/radix tree 迁移：4.20 起渐进替换，6.x 完成

## 来源

- v7.1-rc4 源码行级精读（2026-11-20），文件级镜像
  /home/xiang/kernel-src/linux-7.1-rc4-mirror：
  include/linux/fs.h:401-530（a_ops/address_space/XArray marks）
  include/linux/mm_types.h:401-420（folio）
  include/linux/mmzone.h:386-405（LRU 枚举）
  include/linux/xarray.h:355（xa_load）
  mm/filemap.c:1941（__filemap_get_folio_mpol）、:1807（page_cache_next_miss）
  mm/readahead.c:315（do_page_cache_ra）、:557（page_cache_sync_ra）
  mm/swap.c:506（folio_add_lru）
  mm/page-writeback.c:2670（__folio_mark_dirty）、:2710（filemap_dirty_folio）
  mm/vmscan.c（shrink_folio_list 驱逐路径）
- 判读层关联：`io-storage.md`（回写三闸门/预读旋钮/O_DIRECT/hung task）、
  `memory.md`（swappiness 模型/双缓存/workingset refault）
