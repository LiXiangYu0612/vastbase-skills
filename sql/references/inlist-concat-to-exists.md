# col1||col2 IN (子查询) → EXISTS 两列等值的等价性边界（VB 实测）

**结论**：HIS 老 SQL 高频形状 `b.patient_id || b.visit_id in (select a.patient_id || a.visit_id ...)`，改写为两列等值 EXISTS 的等价性**有且仅有一个前提**：拼接映射单射（patient_id 定长/零填充即满足，HIS 标准 char(10) 病案号属于此类）。

| 前提 | 结果 |
|---|---|
| 拼接单射成立 | 完全等价：双 VB 实例 × 原句逐字(含 (+)) × 16 边界场景 × 双向 MINUS = 0 |
| 拼接非单射 | **原句自身幽灵放行 bug**：`'12'\|\|'34' = '1'\|\|'234'`，无手术住院 12/34 被 IN 放行、术式列全 NULL（实测复现）；EXISTS 版是修正不是偏离 |

## 冗余可删的证明式（子查询侧三件套）
子查询里重复联的 pat_visit / dept_dict_tj_union / 日期过滤，在"外层对同一行已有相同条件的内连接"时纯冗余：外层活下来的行必然通过内层同款谓词（dept 内连接保证字典存在性、日期为同一行同谓词、patient_id+visit_id 为 pat_visit 主键）。净语义 = `EXISTS(operation×dict WHERE 两列等值 AND mode IN (...))`。

## row_number()+rn=1 形状的两个实测行为
- 诊断重复（同住院双 type51/no1 脏数据）与科室字典 dept_code 重复行造成的行扇出，最终都被 rn=1 压成 1 行——**tie 组内挑哪行无语义保证**（原句同样没有）；要确定性在 `order by d.operation_no` 尾部补 tiebreaker（如 `, e.diagnosis_code, g.dept_name`）。
- IN/EXISTS 半连接先过滤后开窗：无术式住院（d 全 NULL）进不了窗口，不存在跨住院 NULL 分区塌缩。

## 红绿对账配方（等价性判定 SOP）
1. 最小 schema + 16 边界场景：合格双术式 / 陷阱（首台 mode3 次台 mode1）/ 全不合格 / 无手术有诊断 / 术式码无字典 / 诊断码无字典 / 双主诊断(tie) / 科室字典重复(tie) / 同病人多次住院 / 出院越窗 / 字典 mode 为 NULL / 无主索引行 / dept NULL / 同病人仅一次住院有术式 / 孤儿手术（visit 不存在）/ 无诊断行。
2. 碰撞赛道独立 schema：`('12','34')` vs `('1','234')`——期望 OLD_ONLY 恰一行幽灵。
3. 视图法对账：`v_old_ora`（用户原句逐字）/`v_old`（(+)→LEFT JOIN 机械翻译，子查询原样保留）/`v_new`，计数 + 双向 MINUS。别名语法差异（`col"别名"` vs `col as "别名"`）不影响集合语义。
4. VB 方言烟雾（跑原句逐字版前先验）：`(+)`、无空格别名、`to_date('...','yyyy-mm-dd')`、`MINUS` —— 5432 主线 VB 与 3.0.9@6432 全可用。

## 版本注记
- 客户生产 3.0.8 无本地实例，验证落在 204 两台 VB（主线 5432 + 3.0.9@6432）；`(+)`/IN/row_number/MINUS 均为稳定语法语义，跨小版本翻面风险极低。
- PG17@9432 侧同形测试亦绿，但该 SQL 属 VB 侧（用户 2026-11-09 令：VB 的 SQL 不测 PG），仅作旁证。

## 来源
2026-11-09 env204 双 VB 实测（主线 5432 + vb309@6432）：客户 HIS 出院手术统计 SQL 等价改写对账，场景矩阵全绿、碰撞幽灵行复现；资产 `/home/xiang/eq_opcase/`（00 烟雾/01 建数/02 视图/03-08 对账）。
