# DataPro 元数据字典与指标口径库模板

| 文档信息 | 内容 |
|---|---|
| 版本 | v0.1 |
| 用途 | W3 起业务接口人 + 数据工程填写，先覆盖高频 50 张表 |
| 上游文档 | DataPro-架构设计HLD.md §5 数据模型 |
| 状态 | 模板，待业务方填写 |

> 本文档是两个模板合辑。线上形态为 MySQL 结构化表（`meta_table` / `meta_column` / `metric_caliber`），本模板为填报用的人工整理版，评审通过后由脚本导入。

---

## 1. 元数据字典

### 1.1 表级信息（meta_table）

| 字段 | 填写要求 | 示例 |
|---|---|---|
| db_name | 库名 | `dwd_shop` |
| table_name | 表名 | `dwd_order_detail_di` |
| cn_name | 中文业务名（必填） | 订单明细日报表 |
| granularity | 数据粒度（一行代表什么） | 一行 = 一笔订单的一个商品 |
| update_freq | 更新频率 | 每日 T+1 凌晨 2 点 |
| owner | 业务负责人（口径答疑人） | 张三 |
| dt_level | 数据分级 L1/L2/L3 | L2 |
| description | 业务场景说明，2~3 句 | 用于订单量、GMV 等分析；含退款标记 |
| priority | P0(高频)/P1/P2 | P0 |

### 1.2 字段级信息（meta_column）

| 字段 | 填写要求 | 示例 |
|---|---|---|
| col_name | 字段名 | `pay_status` |
| cn_name | 中文名（必填，召回主要依据） | 支付状态 |
| data_type | 类型 | `tinyint` |
| enum_meaning | 枚举值含义（枚举字段必填） | 0=未支付 1=已支付 2=已退款 |
| caliber_id | 关联口径 ID（指标字段填） | M003 |
| is_pii | 是否敏感（L3 字段必标） | false |
| sample_values | 2~3 个脱敏样例值 | 1, 2 |
| common_pitfalls | 常见误用说明 | 统计"已支付订单"需排除 pay_status=2 |

### 1.3 填报质量要求

- `cn_name` 禁止直接复制英文字段名翻译腔（如 `order_cnt` → "订单数量"而非"订单计数"）
- 枚举字段 `enum_meaning` 必填——这是取数错误的高发区
- 一张表的填报含表级 + 全部字段，预估 30 分钟/表，按 P0 优先

## 2. 指标口径库（metric_caliber）

### 2.1 口径条目模板

| 字段 | 填写要求 | 示例 |
|---|---|---|
| metric_id | 唯一编号 | M003 |
| metric_name | 指标中文名（唯一，避免同义多词） | 支付订单数 |
| aliases | 常见叫法（召回用） | 付费订单量、成交订单数 |
| definition | 一句话业务定义 | 当日支付成功的订单去重数 |
| logic | 精确计算逻辑（SQL 级描述） | `COUNT(DISTINCT order_id) FROM dwd_order_detail_di WHERE pay_status=1 AND dt='{date}'` |
| dimensions | 常用维度 | 日期、渠道、品类 |
| source_tables | 来源表 | dwd_order_detail_di |
| owner | 口径负责人 | 张三 |
| version / status | v1 / 有效 | 变更需走评审 |
| change_log | 变更记录 | 2026-11-20 初版 |

### 2.2 口径冲突处理规则

1. 发现同名不同义 → 两口径同时标记 `冲突待裁决`，当周口径对齐会裁决
2. 裁决结果更新 `logic` 与 `change_log`，旧版本不删除（版本化）
3. 召回服务遇到"冲突待裁决"状态的口径 → Agent 明确拒答并提示联系口径负责人

## 3. 沉淀机制（历史问答对）

用户反馈 👍 的取数记录自动进入候选池：

| 字段 | 说明 |
|---|---|
| question | 原始问题（含澄清轮次拼接） |
| sql | 最终执行 SQL |
| verified_by | 数据工程抽检确认人 |

每月由数据工程抽检后导入向量库，作为 `search_schema` 的增强召回源（同类问题直接命中历史正确 SQL）。

## 4. 变更记录

| 日期 | 版本 | 变更 | 变更人 |
|---|---|---|---|
| 2026-11-20 | v0.1 | 模板初版 | — |
