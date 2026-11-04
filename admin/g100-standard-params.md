# G100 标准化参数基线（3.0.8 / 3.0.9）

## 概述

> 服务分类：admin（参数基线核对）；performance（诊断对照：报告参数 vs 标准值）

公司标准化参数基线，用于交付前核对与 AWR 诊断时对照（现场报告里某参数偏离
标准值 = 配置类发现线索）。**版本适用**：3.0.8 PSU4 与 3.0.9 PSU1；"Has安装
默认值"列为安装器出厂值，"标准配置"为标准化要求值，两者差异即安装后必须
手工调整项。

原件：`../workspace/G100标准化参数_2026-06-25.xlsx`（含 750 行全参数列表、
参数更新记录 38 条非空、专项测试问题库 66 条、某银行现场参考 190 行——
**该文件与含内部链接/人名的表不入 Git**）。本文件由脚本从原件生成
（2026-10-25），保真不手抄。

**更新契约（2026-10-25 用户定）**：标准化参数表有新版本时由用户通知，
重新导出 xlsx 替换 workspace 原件后重跑生成脚本——不做自动刷新。

**用户令修订（2026-11-02，先于下一版 xlsx 生效）**：`max_files_per_process`
标准值由 100000 改为 **10000~20000**（闭区间；基线 JSON 与机械闸已支持区间
判定）。理由：现场 200000 出过多次问题（过大）；机制与取舍见
`../internals/fd-vfd-budget.md`——**分区表分区特别多、local index 也多时，
预算偏低会频繁 open/close fd 造成性能衰减，此类实例取区间上限并盯 fd 抖动**。

**用户令修订（2026-11-04，先于下一版 xlsx 生效）**：`num_internal_lock_partitions`
基线收录，对照口径只判 **FASTPATH_PART 子字段，标准 = 100 或 200（枚举，非区间，
中间值不算合规）**；其余子字段（CLOG_PART=256 / CSNLOG_PART=512 /
LOG2_LOCKTABLE_PART=4 / TWOPHASE_PART=1）沿用更新记录行出厂值不判。原更新记录行
（FASTPATH_PART=20 太小调到 100，加快轻量锁分配减少阻塞）为本令前身；比对器已支持
"子字段=枚举" 形制（extract.py `_hit`）。env-T 实测 FASTPATH_PART=100 合规。

## 高危参数速览（风险等级=高，308/309 表内标注）

按两版标准化表提取风险等级"高"的参数与标准值。

## 参数地雷清单（问题记录 + 更新记录中的宕库级修改）

以下条目来自"参数问题记录"全表与"参数更新记录"中修改原因含
宕库/crash/core/夯 的条目——**遇到这些参数必须核对版本与 PSU**。


### 高危参数（风险等级=高）

| 版本 | 参数 | 标准值 |
|---|---|---|
| 308 | session_timeout | '30min' |
| 308 | wal_level | hot_standby |
| 308 | most_available_sync | on |
| 308 | sync_config_strategy | none_node |
| 308 | synchronous_commit | on |
| 308 | synchronous_standby_names | '*' |
| 308 | enable_slot_log | on |
| 308 | log_statement | 'ddl' |
| 308 | vacuum_cost_limit | 10000 |
| 308 | track_stmt_stat_level | 'off,L1' |
| 308 | enable_opfusion | off |
| 308 | behavior_compat_options | 'display_leading_zero,skip_insert_gs_source' |
| 308 | enable_nestloop | off |
| 308 | enable_pbe_optimization | off |
| 308 | enable_ustore | off |
| 308 | enable_thread_pool | off |
| 308 | use_workload_manager | off |
| 308 | enable_oranumber_type | off |
| 308 | new_sort | off |
| 308 | query_dop | 1 |
| 308 | max_flashback_time | 0 |
| 308 | max_parallel_workers_per_gather | 0 |
| 308 | enable_global_result_cache | off |
| 308 | enable_recyclebin | off |
| 308 | b_compatibility_mode | off |
| 309 | vb_recover_memory_percent | 0 |
| 309 | vb_wait_backends_timeout | 10s |
| 309 | vb_enable_compile_view_after_rename_base | off |
| 309 | session_timeout | '30min' |
| 309 | wal_level | hot_standby |
| 309 | most_available_sync | on |
| 309 | sync_config_strategy | none_node |
| 309 | synchronous_commit | on |
| 309 | synchronous_standby_names | '*' |
| 309 | enable_slot_log | on |
| 309 | log_statement | 'ddl' |
| 309 | vacuum_cost_limit | 10000 |
| 309 | track_stmt_stat_level | 'off,L1' |
| 309 | enable_opfusion | off |
| 309 | enable_audit | off |
| 309 | behavior_compat_options | 'display_leading_zero,skip_insert_gs_source' |
| 309 | enable_nestloop | off |
| 309 | enable_pbe_optimization | off |
| 309 | enable_ustore | off |
| 309 | enable_thread_pool | off |
| 309 | use_workload_manager | off |
| 309 | enable_oranumber_type | off |
| 309 | new_sort | off |
| 309 | query_dop | 1 |
| 309 | max_flashback_time | 0 |
| 309 | max_parallel_workers_per_gather | 0 |
| 309 | enable_global_result_cache | off |
| 309 | enable_recyclebin | off |
| 309 | b_compatibility_mode | off |

## 3.0.8 PSU4 标准化参数（108 行）

| 参数类型 | 参数名称 | 风险等级 | 3.0.8 PSU4 标准配置 | Has安装默认值 | 研发建议 |
| SecureAccess | listen_addresses |  | '*' | localhost |
|  | password_force_alter | 中 | off | off |
|  | vastbase_login_info | 中 | False | False |
|  | session_timeout | 高 | '30min' | 10min |
|  | password_effect_time | 中 | 36500 | 90 |
| WAL | wal_level | 高 | hot_standby | hot_standby |
|  | full_page_writes |  | off | off |
|  | wal_log_hints | 中 | on | on |
|  | xloginsert_locks |  | 8 | 8 |
|  | advance_xlog_file_num |  | 10 | 0 |
|  | archive_mode |  | on | off |
|  | archive_dest |  | '/backup/archive_wals/' | '' |
| replicationAndSlots | wal_keep_segments |  | 128 | 16 |
|  | max_wal_senders |  | 8 | 16 |
|  | most_available_sync | 高 | on | off | 建议修改 |
|  | catchup2normal_wait_time |  | 0 | -1 |
|  | wal_receiver_timeout |  | 60s | 6s |
|  | sync_config_strategy | 高 | none_node | all_node |
|  | wal_sender_timeout |  | 10s | 6s |
|  | synchronous_commit | 高 | on | on |
|  | synchronous_standby_names | 高 | '*' | '*' |
|  | enable_slot_log | 高 | on | off |
| dbLog | log_duration | 中 | on | off |
|  | log_line_prefix |  | '%m %r %d %u [%x] [%p]' | '%m %c %d %p %a %x %n %e ' |
|  | log_checkpoints |  | on | off |
|  | log_statement | 高 | 'ddl' | 'none' |
|  | log_lock_waits | 中 | on | off |
|  | log_timezone |  | 'PRC' | 'PRC' |
| VACUUM | autovacuum_mode | 中 | mix | mix |
|  | autovacuum_max_workers |  | 5 | 3 |
|  | autovacuum_naptime |  | 20s | 20s |
|  | autovacuum_vacuum_cost_delay |  | 10 | 20 |
|  | autovacuum_vacuum_scale_factor |  | 0.05 | 0.2 |
|  | autovacuum_analyze_scale_factor |  | 0.02 | 0.1 |
|  | autovacuum_vacuum_threshold |  | 200 | 50 |
|  | autovacuum_analyze_threshold |  | 200 | 50 |
|  | vacuum_cost_limit | 高 | 10000 | 200 |
|  | autoanalyze_timeout |  | 900 | 300 |
| perfStats | enable_instr_cpu_timer | 中 | on | on |
|  | enable_stmt_track | 中 | on | on |
|  | enable_resource_track | 中 | on | on |
|  | enable_wdr_snapshot | 中 | on | off |
|  | enable_auto_clean_unique_sql | 中 | on | off |
|  | instr_unique_sql_count | 中 | 200000 | 100 |
|  | track_stmt_stat_level | 高 | 'off,L1' | 'off,L0' |
|  | log_min_duration_statement | 中 | 3s | 1800s |
|  | track_activities |  | on | on |
|  | track_activity_query_size |  | 4096 | 1024 |
|  | track_sql_count |  | on | on |
|  | enable_instr_rt_percentile |  | off | on |
|  | enable_opfusion | 高 | off | on |
| audit | audit_enabled | 超高 | off | on |
|  | audit_login_logout |  | 7 | 7 |
|  | audit_grant_revoke |  | 1 | 1 |
|  | audit_database_process |  | 0 | 1 |
|  | audit_user_locked |  | 0 | 1 |
|  | audit_user_violation |  | 0 | 0 |
|  | audit_system_object |  | 0 | 12295 |
|  | audit_set_parameter |  | 0 | 1 |
|  | audit_xid_info |  | 0 | 0 |
|  | audit_resource_policy |  | off | on |
|  | audit_file_remain_time |  | 7 | 90 |
|  | audit_space_limit |  | 5GB | 1GB |
| otherKeyParams | lower_case_table_names | 中 | 1 | 0 |
|  | enable_cbm_tracking | 中 | on | off |
|  | behavior_compat_options | 高 | 'display_leading_zero,skip_insert_gs_source' | reduce_tailing_zero,skip_insert_gs_source,select_into_return_null |
|  | cstore_buffers |  | 16MB | 512MB |
|  | standby_shared_buffers_fraction |  | 1 | 0.3 |
|  | checkpoint_segments |  | 128 | 64 |
|  | checkpoint_completion_target |  | 0.9 | 0.5 |
|  | max_files_per_process | 高 | 10000~20000 | 1000 |
|  | lc_messages |  | 'en_US.UTF-8' | 'en_US.UTF-8' |
|  | lc_monetary |  | 'en_US.UTF-8' | 'en_US.UTF-8' |
|  | lc_numeric |  | 'en_US.UTF-8' | 'en_US.UTF-8' |
|  | lc_time |  | 'en_US.UTF-8' | 'en_US.UTF-8' |
|  | timezone | 中 | 'PRC' | 'PRC' |
|  | enable_mergejoin | 中 | on | on | 待定 |
|  | enable_nestloop | 高 | off | off |
|  | enable_pbe_optimization | 高 | off | on | 建议修改 |
|  | enable_asp |  | on | on |
|  | recovery_max_workers |  | 4 | 4 |
|  | enable_alarm |  | off | on |
|  | enable_codegen |  | off | off |
|  | update_lockwait_timeout |  | 5min | 20min |
|  | lockwait_timeout |  | 5min | 20min |
|  | max_size_for_xlog_prune | 中 | '100GB' | 2TB |
|  | enable_ustore | 高 | off | on |
|  | random_page_cost | 中 | 1.1 | 4 |
|  | enable_thread_pool | 高 | off | off |
|  | temp_file_limit | 中 | 50GB | -1 |
|  | use_workload_manager | 高 | off | off |
|  | rewrite_rule |  | 'magicset,enable_any_sublink_pullup_enhanced' | 'magicset' |
|  | enable_bbox_dump |  | on | on |
|  | enable_partition_analyze | 中 | on | off |
|  | max_compile_functions | 中 | 2147483647 | 1000 |
|  | use_part_stat_to_get_cost | 中 | on | off |
|  | enable_oranumber_type | 高 | off | on | 建议修改 |
|  | resource_limit | 中 | off | on | 建议修改 |
|  | new_sort | 高 | off | adaptive_sort |
|  | vb_enable_remove_unused_outputs |  | on | off |
|  | b_format_behavior_compat_options |  | 'set_keyword_as_colname,b_format_escape,show_attalias_as_colname,pg_todate_format,enable_autoinc_adaptive_maxvalue' | 'set_keyword_as_colname,b_format_escape,show_attalias_as_colname,pg_todate_format' |
| 其他高危参数 | query_dop | 高 | 1 | 1 |
|  | max_flashback_time | 高 | 0 | 0 |
|  | max_parallel_workers_per_gather | 高 | 0 | 0 |
|  | enable_global_result_cache | 高 | off | off |
|  | enable_recyclebin | 高 | off | off |
|  | b_compatibility_mode | 高 | off | off |

## 3.0.9 PSU1 标准化参数（113 行）

| 参数类型 | 参数名称 | 3.0.9 PSU1 标准配置 | Has安装默认值 | 研发建议 | 是否调整 | 风险等级 |
| 309新增 | postgres_fdw_use_remote_timezone | off | off |
|  | vb_track_plsql_activities | off | off |
|  | vb_inlist_fusion_num | 0 | 0 |
|  | vb_memory_overload_threshold | 90 | 90 |
|  | vb_memory_snapshot_interval | 3 | 3 |
|  | vb_recover_memory_duration | 60 | 60 |
|  | vb_recover_memory_percent | 0 | 0 |  |  | 高 |
|  | vb_cplan_cost_factor | 1.1 | 1.1 |
|  | recovery_show_time_stats | on | off |
|  | recovery_optimization_mask | '0,0,0,0,0,0,0,0' | '0,0,0,0,0,0,0,0' |
|  | hot_standby_lock_redo_opt | off | off |
|  | vb_wait_backends_timeout | 10s | 10s |  |  | 高 |
|  | vb_auto_analyze_stmt_track | on | off |
|  | enable_expr_fusion | off | off |
|  | vb_enable_compile_view_after_rename_base | off | off |  |  | 高 |
| SecureAccess | listen_addresses | '*' | localhost |
|  | password_force_alter | off | off |  |  | 中 |
|  | vastbase_login_info | False | False |  |  | 中 |
|  | session_timeout | '30min' | 10min |  |  | 高 |
|  | password_effect_time | 36500 | 90 |  |  | 中 |
| WAL | wal_level | hot_standby | hot_standby |  |  | 高 |
|  | full_page_writes | off | off |
|  | wal_log_hints | on | on |  |  | 中 |
|  | xloginsert_locks | 8 | 8 |
|  | advance_xlog_file_num | 10 | 0 |
|  | archive_mode | on | off |
|  | archive_dest | '/backup/archive_wals/' | '' |
| replicationAndSlots | wal_keep_segments | 128 | 16 |
|  | max_wal_senders | 8 | 16 |
|  | most_available_sync | on | off | 建议修改 | 不调整，添加参数说明 | 高 |
|  | catchup2normal_wait_time | 0 | -1 |
|  | wal_receiver_timeout | 60s | 6s |
|  | sync_config_strategy | none_node | all_node |  |  | 高 |
|  | wal_sender_timeout | 10s | 6s |
|  | synchronous_commit | on | on |  |  | 高 |
|  | synchronous_standby_names | '*' | '*' |  |  | 高 |
|  | enable_slot_log | on | off |  |  | 高 |
| dbLog | log_duration | on | off |  |  | 中 |
|  | log_line_prefix | '%m %r %d %u [%x] [%p]' | '%m %c %d %p %a %x %n %e ' |
|  | log_checkpoints | on | off |
|  | log_statement | 'ddl' | 'none' |  |  | 高 |
|  | log_lock_waits | on | off |  |  | 中 |
|  | log_timezone | 'PRC' | 'PRC' |
| VACUUM | autovacuum_mode | mix | mix |  |  | 中 |
|  | autovacuum_max_workers | 5 | 3 |
|  | autovacuum_naptime | 20s | 20s |
|  | autovacuum_vacuum_cost_delay | 10 | 20 |
|  | autovacuum_vacuum_scale_factor | 0.05 | 0.2 |
|  | autovacuum_analyze_scale_factor | 0.02 | 0.1 |
|  | autovacuum_vacuum_threshold | 200 | 50 |
|  | autovacuum_analyze_threshold | 200 | 50 |
|  | vacuum_cost_limit | 10000 | 200 |  |  | 高 |
|  | autoanalyze_timeout | 900 | 300 |
| perfStats | enable_instr_cpu_timer | on | on |  |  | 中 |
|  | enable_stmt_track | on | on |  |  | 中 |
|  | enable_resource_track | on | on |  |  | 中 |
|  | enable_wdr_snapshot | on | off |  |  | 中 |
|  | enable_auto_clean_unique_sql | on | off |  |  | 中 |
|  | instr_unique_sql_count | 200000 | 100 |  |  | 中 |
|  | track_stmt_stat_level | 'off,L1' | 'off,L0' |  |  | 高 |
|  | log_min_duration_statement | 3s | 1800s |  |  | 中 |
|  | track_activities | on | on |
|  | track_activity_query_size | 4096 | 1024 |
|  | track_sql_count | on | on |
|  | enable_instr_rt_percentile | off | on |
|  | enable_opfusion | off | on |  |  | 高 |
| audit | enable_audit | off | off |  |  | 高 |
|  | audit_directory_size | 1GB | 128MB |
| otherKeyParams | lower_case_table_names | 1 | 0 |  |  | 中 |
|  | enable_cbm_tracking | on | off |  |  | 中 |
|  | behavior_compat_options | 'display_leading_zero,skip_insert_gs_source' | reduce_tailing_zero,skip_insert_gs_source,select_into_return_null |  |  | 高 |
|  | cstore_buffers | 16MB | 512MB |
|  | standby_shared_buffers_fraction | 1 | 0.3 |
|  | checkpoint_segments | 128 | 64 |
|  | checkpoint_completion_target | 0.9 | 0.5 |
|  | max_files_per_process | 10000~20000 | 1000 |
|  | lc_messages | 'en_US.UTF-8' | 'en_US.UTF-8' |
|  | lc_monetary | 'en_US.UTF-8' | 'en_US.UTF-8' |
|  | lc_numeric | 'en_US.UTF-8' | 'en_US.UTF-8' |
|  | lc_time | 'en_US.UTF-8' | 'en_US.UTF-8' |
|  | timezone | 'PRC' | 'PRC' |  |  | 中 |
|  | enable_mergejoin | on | on | 待定 | 评估中 | 中 |
|  | enable_nestloop | off | off |  |  | 高 |
|  | enable_pbe_optimization | off | on | 建议修改 | 已调整 | 高 |
|  | enable_asp | on | on |
|  | recovery_max_workers | 1 | 4 |
|  | enable_alarm | off | on |
|  | enable_codegen | off | off |
|  | update_lockwait_timeout | 5min | 20min |
|  | lockwait_timeout | 5min | 20min |
|  | max_size_for_xlog_prune | '100GB' | 2TB |  |  | 中 |
|  | enable_ustore | off | on |  |  | 高 |
|  | random_page_cost | 1.1 | 4 |  |  | 中 |
|  | enable_thread_pool | off | off |  |  | 高 |
|  | temp_file_limit | 50GB | -1 |  |  | 中 |
|  | use_workload_manager | off | off |  |  | 高 |
|  | rewrite_rule | magicset,pullup_empty_from_list_subquery,not_in_null_elimination,enable_any_sublink_pullup_enhanced | 'magicset' |  |  | 中 |
|  | enable_bbox_dump | on | on |
|  | enable_partition_analyze | on | off |  |  | 中 |
|  | max_compile_functions | 2147483647 | 1000 |  |  | 中 |
|  | use_part_stat_to_get_cost | on | off |  |  | 中 |
|  | enable_oranumber_type | off | on |  |  | 高 |
|  | resource_limit | off | on |  |  | 中 |
|  | new_sort | off | adaptive_sort |  |  | 高 |
|  | vb_enable_remove_unused_outputs | on | off |  |  | 中 |
|  | b_format_behavior_compat_options | 'set_keyword_as_colname,b_format_escape,show_attalias_as_colname,pg_todate_format,enable_autoinc_adaptive_maxvalue' | 'set_keyword_as_colname,b_format_escape,show_attalias_as_colname,pg_todate_format' |  |  | 中 |
| 其他高危参数 | query_dop | 1 | 1 |  |  | 高 |
|  | max_flashback_time | 0 | 0 |  |  | 高 |
|  | max_parallel_workers_per_gather | 0 | 0 |  |  | 高 |
|  | enable_global_result_cache | off | off |  |  | 高 |
|  | enable_recyclebin | off | off |  |  | 高 |
|  | b_compatibility_mode | off | off |  |  | 高 |

## 参数问题记录（14 行全量）

| 参数名称 | 问题描述 | 解决办法 | 问题版本 | 修复版本 | 问题链接 |
| behavior_compat_options | 存储过程/函数运行时会重新计算search_path，需要在behavior_compat_options追加bind_procedure_searchpath，否则会丢失部分search_path，导致找不到依赖的对象 | 在behavior_compat_options参数中追加bind_procedure_searchpath | 2.2.15 PSU2 | 无 | https://support.vastdata.com.cn/#/info/790059337237139457 |
| behavior_compat_options | 若配置plsql_security_definer选项再创建postGis插件，使用postGis相关函数不会走索引，因为创建函数时其默认的prosecodef为true,无法使用索引扫描。 | 方案一：设置behavior_compat_options不包含plsql_security_definer，然后重建postgis插件或单独的那个函数 方案二：不调整参数，修改函数alter function xx security invoker（对于其它类似的需要使用索引的函数可能都需要重建） | 2.2.10 | 无 | https://support.vastdata.com.cn/#/info/750501619447107585 |
| enable_opfusion | 开启enable_opfusion参数时，通过jdbc驱动执行insert all into会引起数据库实例crash | enable_opfusion=off | 2.2.10 PSU11以下 | 2.2.10 PSU11 | https://support.vastdata.com.cn/#/knowledgeInfo/723365955740110849 |
| enable_pbe_optimization | 参数修改为off后，部分在on时执行很快的过程变得非常慢 | enable_pbe_optimization=on | 2.2.10 | 无 |
| enable_pbe_optimization | 【1030688】【严重】修复了在过滤条件选择率高时，关闭enable_pbe_optimization参数后，PBE执行计划第6次走的generic plan与打开enable_pbe_optimization参数时的执行计划一样，未作优化的问题。 | 这种数据分布倾斜的情况,可以考虑在执行该语句之前设置会话级参数 set plan_cache_mode = force_custom_plan , 在每次执行时都代入具体的常量参数值进行代价估算,生成更优的执行计划 | 2.2.10 PSU16以下 | 2.2.10 PSU16 | 内部缺陷库1030688 |
| enable_stmt_track | 开启enable_stmt_track参数跟踪所有执行的SQL语句导致会话内存持续增加，会话不断开则无法释放 | enable_stmt_track=off | 2.2.10 PSU7以下 | 2.2.10 PSU7 | 内部缺陷库1160475194001018425&url_cache_key=from_url_release_list_856dae921b4f51d1360f68e9e90687dc&action_entry_type=bugs |
| query_dop | 在非线程池模式下，使用query_dop后（包含Hint级别使用），实际上query_dop相关的线程已退出，但由于代码逻辑问题导致asp统计有误 | 保持query_dop=1或升级版本 | 2.2.10 PSU15以下 | 2.2.10 PSU15 | 内部缺陷库1160475194001038298 |
| shared_preload_libraries | 2.2.10 PSU9  MySQL兼容模式下执行加解密函数报错：ERROR:  operator too long at or near "**" LINE 2: gs_decrypt(v.a0101,'BJhxyz0318','sm4') | 参数shared_preload_libraries不配置pg_stat_statements | 2.2.10 PSU9 | 无 | 内部缺陷库1160475194001036179 |
| sql_beta_feature | 在Postgres兼容性模式下，FDW可以支持访问远程Postgres风格声明式分区表。非Postgres兼容模式使用postgres_fdw插件访问Postgres数据库的分区表报错 ERROR：could not operate foreign table on partitioned table | sql_beta_feature 参数值增加 'PARTITION_FDW_ON'，多个参数值以逗号分隔 | 2.2.15 | 无 |
| synchronous_commit | synchronous_commit=remote_apply，主库使用vacuum清理死元组时ddl和grant的执行时间变为原来的10倍以上 | 无 | 2.2.10 | 无 | 内部缺陷库1160475194001027618 |
| synchronous_standby_names | Has1.2集群主库执行vb_ctl stop命令后，未立即恢复，而是多次重启后才恢复启动。 | postgresql.base.conf内设定synchronous_standby_names='*' | 2.2.10 | 无 | 内部缺陷库1160475194001019711 |
| track_stmt_stat_level | 全量 SQL 级别跟踪级别设置为非 OFF 时，性能下降 80% ARM 架构 1000 仓 800 并发 'OFF,OFF': 108.48w 'OFF,L1': 105.56w 'OFF,L1': 104.52w 'L0,L0': 18:40w 'L0,L1': 17.54w 'L1,L1': 16.85w 1. 该参数对性能影响很大，第一部分为全量 SQL 跟踪级别，第二部分为慢 SQL 跟踪级别 2. 全量 SQL 级别跟踪级别设置为非 OFF 时，性能下降 80% 3. 慢 SQL 跟踪级别设置越高时，每高出一个级别，性能下降 1~2% | track_stmt_stat_level='off,L1' | 2.2.10 | 无 | 极限测试场景+客户化场景收集表 |
| vastbase_sql_mode | 当vastbase_sql_mode为空时，Vpatch升级失败，vpatch --upgrade环节出错 ERROR:  syntax error at or near ""any"" LINE 1: DROP FUNCTION IF EXISTS pg_catalog.json_length("any"); | 当客户通过alter database dbname set vastbase_sql_mode=''时，vastbase_sql_mode参数并不会在postgresql.conf文件体现。此时需要查询pg_db_role_setting表，将对应用户或库的vastbase_sql_mode参数重置。  vpatch升级时判断如果在postgresql.conf中写明了vastbase_sql_mode=''，这样在重启后每个库的vastbase_sql_mode都是空。vpatch也可以对应检测到配置文件这一行，在升级期间注释掉，启动时就是默认值，这样升级就不会报错。而通过alter方式修改的，vpatch检测不到，导致升级时vpatch未将vastbase_sql_mode设置为默认值，导致升级失败。 | 2.2.10 PSU9 升级至 2.2.10 PSU 14 | 无 | https://support.vastdata.com.cn/#/info/775081735632195585 |

## 参数更新记录（38 行全量）

| 参数名 | 修改前 | 修改后 | 修改原因 | 修改影响 | 涉及版本 | 提出人 | 提出日期 | 完成日期 | 评估建议 |
| rewrite_rule | 略 | 移除naaj_optimizer选项 | 该选项应按需配置 | 部分not in查询性能可能劣化 | 309 | — | 2026-06-24 00:00:00 | 2026-06-25 00:00:00 | 修改 |
| enable_early_free | on | off | 避免并发更新时宕库 | 避免宕库 | 所有版本 | — | 2026-06-23 00:00:00 |
| audit_directory_size | 131072 | 1048576 | 避免审计目录满后夯住数据库 | 降低夯库风险 | >=309 PSU1 | — | 2026-06-23 00:00:00 | 2026-06-25 00:00:00 | 修改 |
| vb_auto_analyze_stmt_track | full | off | vb_auto_analyze_stmt_track = full 在309 psu1 有bug 会导致coredown 【压测触发数据库coredump】 内部缺陷库1160475194001079257 | 避免宕库 | >=309 PSU1 | — | 2026-06-22 00:00:00 | 2026-06-25 00:00:00 | 修改 |
| enable_pbe_optimization | on | off | Bug标题： 函数中数组使用<@方式比较触发core Bug ID：1076882  Bug链接： 内部缺陷库1160475194001076882 |  | 2.2.15 | — | 2026-06-12 00:00:00 | 2026-06-12 00:00:00 | 修改 |
| recovery_optimization_mask | '0,1,0,0,0,1,1,1' | '0,0,0,0,0,0,0,0' | 规避备库回放宕库风险 | 降低串行回放性能 | >=309 PSU1 | — | 2026-06-07 00:00:00 | 2026-06-08 00:00:00 | 修改 |
| recovery_optimization_mask | '0,1,1,1,0,1,1,1' | '0,1,0,0,0,1,1,1' | 规避备库回放宕库风险 | 降低串行回放性能 | >=309 PSU1 | — | 2026-05-25 00:00:00 | 2026-05-27 00:00:00 | 修改 |
| enable_db_ts_space_limit | on | off | 资质功能，无实际作用，研发建议关闭。 | 影响较小 | 2.2.15,3.0.8,309 | — | 2026-05-25 00:00:00 | 2026-05-27 00:00:00 | 修改 |
| recovery_show_time_stats | off | on | 309 PSU1新增参数 | 增强备库回放可观测性 | >=309 PSU1 | — | 2026-04-14 00:00:00 | 2026-04-17 00:00:00 | 修改 |
| recovery_optimization_mask | '0,0,0,0,0,0,0,0' | '0,1,1,1,0,1,1,1' | 309 PSU1新增参数 | 增强串行回放性能 | >=309 PSU1 | — | 2026-04-14 00:00:00 | 2026-04-17 00:00:00 | 修改 |
| hot_standby_lock_redo_opt | off | on | 309 PSU1新增参数 | 减少备库回放冲突 | >=309 PSU1 | — | 2026-04-14 00:00:00 | 2026-04-17 00:00:00 | 修改 |
| vb_auto_analyze_stmt_track | off | full | 309 PSU1新增参数 | 记录详细慢SQL计划 | >=309 PSU1 | — | 2026-04-14 00:00:00 | 2026-04-17 00:00:00 | 修改 |
| recovery_max_workers | 4 | 1 | 配合309 PSU1新增串行回放优化，预期回放性能超过并行回放。 | 并行回放调整为串行回放 | >=309 PSU1 | — | 2026-04-14 00:00:00 | 2026-04-17 00:00:00 | 修改 |
| enable_pbe_optimization | on | off | Bug标题： 函数中数组使用<@方式比较触发core Bug ID：1076882  Bug链接： 内部缺陷库1160475194001076882 |  | 3.0.8,309 | — | 2026-03-24 00:00:00 | 2026-04-17 00:00:00 | 修改 |
| most_available_sync | off | on | 单机部署的时候不会去修改该参数为on 。vb_basebackup 向单机拉取备份的时候  会hung住主节点。引发了  HL2026014779这个故障 ，建议单机部署也用ON | 提升可用性 | 所有版本 | — | 2026-02-06 00:00:00 | 2026-02-06 00:00:00 | 修改单机部署文档 |
| vb_enable_compute_xid_horizon | on | off（使用全局索引时） | 高风险ID：277。 描述：308 PSU4分区表写入报错 could not read block xx in file "base/xx/xx": read only 0 of 8192 bytes”，建议308 PSU4版本使用全局索引的关闭参数vb_enable_compute_xid_horizon。  触发条件：分区表使用全局索引且vb_enable_compute_xid_horizon=on  影响：业务插入数据报错。  原因：308 PSU4版本vb_enable_compute_xid_horizon=on新特性开发没考虑全局索引触发的缺陷  Workaround：vb_enable_compute_xid_horizon改为off。 | 规避业务插入数据报错缺陷，建议308 PSU4版本使用全局索引的关闭参数 | 3.0.8 PSU4 | — | 2026-01-15 00:00:00 | 2026-01-26 00:00:00 | 修改 |
| num_internal_lock_partitions | 'CLOG_PART=256,CSNLOG_PART=512,LOG2_LOCKTABLE_PART=4,TWOPHASE_PART=1,FASTPATH_PART=20' | 'CLOG_PART=256,CSNLOG_PART=512,LOG2_LOCKTABLE_PART=4,TWOPHASE_PART=1,FASTPATH_PART=100' | FASTPATH_PART=20太小，调到100 | 加快轻量锁分配，减少阻塞 | 所有版本 | — | 2025-12-18 00:00:00 | 2026-01-26 00:00:00 | 修改 |
| recovery_max_workers | 4 | 1（备库有只读业务时） | 添加参数说明，若备库需要承载只读业务，则建议该备库使用串行回放，避免主备库取快照卡死。 | 仅备库有只读业务时建议修改为1，避免主备库取快照卡死 | 所有版本 | — | 2025-12-16 00:00:00 | 2026-01-26 00:00:00 | 增加参数说明 |
| vb_enable_remove_unused_outputs | off | on | 用于减少子查询中不必要的目标列投影。对子查询的目标列进行优化，如果没有被上层引用，满足条件的情况下可以不进行投影，以提升效率。 | 提升性能 | >=3.0.8 PSU1 | — | 2025-12-03 00:00:00 |  | 修改 |
| use_part_stat_to_get_cost | off | on | enable_partition_analyze配套参数，使分区表生成查询计划时能用上分区的统计信息。 | 使分区表查询走上更合适的执行计划 | 3.0.8,>=2.2.15 PSU8 | — | 2025-11-05 00:00:00 |  | 修改 |
| autoanalyze_timeout | 300 | 900 | enable_partition_analyze开启后，大分区表可能analyze超时，因此适当调大超时时间。避免因超时导致分区表统计信息不准确。 | 避免大分区表analyze超时 | 3.0.8,>=2.2.15 PSU8 | — | 2025-11-05 00:00:00 |  | 修改 |
| max_compile_functions | 1000 | 2147483647 | 【问题描述】 数据库实例crash导致主备切换，需要排查原因。  【问题原因】  由于max_compile_functions参数限制，当会话中缓存的函数超过此值时会出现内存置换，从而触发宕机。  【解决方案】将max_compile_functions参数调整为最大值2147483647。  相关TAPD链接：  【max_compile_functions参数问题】  内部缺陷库1160475194001061484 | 避免宕机 | 所有版本 | — | 2025-10-28 00:00:00 |  | 修改 |
| new_sort | adaptive_sort | off | 308 PSU1后默认开启，但属于POC特性 | 开启影响稳定性 | >=3.0.8 PSU1 | — | 2025-09-05 00:00:00 | 2025-10-21 00:00:00 | 修改 |
| enable_slot_log | off | on | 主备逻辑复制槽信息保持同步，避免阻塞xlog清理 | 主备同步逻辑复制槽信息，避免阻塞xlog清理 | 所有版本 | — | 2025-08-21 00:00:00 | 2025-08-21 00:00:00 | 修改 |
| enable_nestloop | on | off | 大表nestloop join性能很差 | 提升SQL性能 | 所有版本 | — | 2025-08-21 00:00:00 | 2025-08-22 00:00:00 | 修改 |
| log_lock_waits | off | on | 等锁时间超过deadlock_timeout时，打印到日志 | 增加日志量，有助于排查阻塞问题 | 所有版本 | — | 2025-08-12 00:00:00 | 2025-08-21 00:00:00 | 修改 |
| join_collapse_limit | 30 | 8 | 8为默认值，调大可能性能劣化，因此删除该参数，不做显式配置，保持默认值。 | 优化性能 | 所有版本 | — | 2025-07-25 00:00:00 | 2025-07-25 00:00:00 | 修改 |
| from_collapse_limit | 30 | 8 | 8为默认值，调大可能性能劣化，因此删除该参数，不做显式配置，保持默认值。 | 优化性能 | 所有版本 | — | 2025-07-25 00:00:00 | 2025-07-25 00:00:00 | 修改 |
| random_page_cost | 4 | 1.1 | 默认值偏大，导致索引扫描成本偏高 | 降低索引扫描成本，使执行计划更倾向于走索引 | 所有版本 | — | 2025-07-21 00:00:00 | 2025-08-21 00:00:00 | 修改 |
| enable_partition_analyze | off | on | 使pg_statistic 收集 analyze 分区表分区的统计信息，在 pg_statistic 系统表中对应 starelkind='p'，starelid 为分区 oid | 优化分区表查询性能 | 3.0.8,>=2.2.15 PSU8 | — | 2025-07-07 00:00:00 | 2025-08-21 00:00:00 | 修改 |
| enable_bbox_dump | off | on | 该参数关闭时，默认使用/proc/sys/kernel/core_pattern记录的路径，而非bbox_dump_path指定路径 | 能够确保core文件目录正确 | 2.2.15,3.0.8 | — | 2025-07-01 00:00:00 | 2025-08-21 00:00:00 | 修改 |
| rewrite_rule | magicset | magicset,enable_any_sublink_pullup_enhanced | in子查询时，带有关联条件子查询无法提升，exists查询快，添加选项后in查询也可以提升 | 提升in查询等子连接查询性能 | 2.2.15,3.0.8 | — | 2025-07-01 00:00:00 | 2025-08-21 00:00:00 | 修改 |
| enable_oranumber_type | on | off | Oracle兼容参数，默认为on，导致不走索引，但是如果修改参数，需要重新初始化数据才能生效 | 提升查询性能，但number类型显示为numeric可能引起客户质疑 | 2.2.15,3.0.8 | — | 2025-07-01 00:00:00 | 2025-08-21 00:00:00 | 修改 |
| resource_limit | on | off | 参数为 on，则session_timeout、password_effect_time等参数不再生效，相关功能由 Profile 配置控制 | session_timeout等参数行为与之前版本保持一致，但会降低Oracle兼容性 | >=3.0.8 PSU1 | — | 2025-07-01 00:00:00 | 2025-08-21 00:00:00 | 修改 |
| log_line_prefix | %m %r %d %u [%p] | %m %r %d %u [%x] [%p] | 增加x%，显示事务号 | 增加日志量 | 所有版本 | — | 2025-07-01 00:00:00 | 2025-08-21 00:00:00 | 修改 |
| enable_wdr_snapshot | on | off | 发生多起snapshot占用空间很大的问题，建议关掉，按需打开。 | 节省空间和磁盘IO，但不利于分析性能问题 | 所有版本 | — | 2025-07-01 00:00:00 | 2025-08-21 00:00:00 | 不修改，保持打开，按需关闭 |
| b_format_behavior_compat_options | < 3.0.8 PSU1: set_keyword_as_colname, b_format_escape >=3.0.8 PSU1:  set_keyword_as_colname, b_format_escape, show_attalias_as_colname, pg_todate_format | 增加enable_autoinc_adaptive_maxvalue | enable_autoinc_adaptive_maxvalue创建序列时自适应设置maxvalue，否则默认设置int128_max。而hibernate中sequence的maxvalue默认取long类型，无法手动调整，操作序列时将出错， enable_user_as_keyword：user字符不作为对象名，保留create user mapping功能。但配置后不支持create/alter user，故不配置 |  | >=3.0.8 PSU1 | — | 2025-07-01 00:00:00 | 2025-08-21 00:00:00 | 暂时仅对308 PSU1 以上调整 |

## 版本注记

- 覆盖 3.0.8 PSU4 / 3.0.9 PSU1；2.2.10 PSU16 与 2.2.15 PSU2 列见原件"参数列表"表
- 更新记录中多条为"避免宕库"级修改（enable_early_free、vb_auto_analyze_stmt_track、
  recovery_optimization_mask 等）——低版本环境必查

## 来源

- 公司标准化参数表 xlsx（2026-06-25 更新版），2026-10-25 脚本提取
- 表内链接为内部 TAPD/支持平台地址，仅内部使用

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
