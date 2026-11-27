-- 先知-代理商系统投放数据 ODS 表：DDL + dp_ods_writer 授权模板
-- 执行人：运维/DBA（管理账号），不是采集进程账号。
-- 安全红线：本文件禁止出现任何真实口令；口令由运维通过环境变量注入并在生产强制轮换。
--   Linux/macOS 示例（自行替换 <运维注入> 的来源，勿落盘）：
--     export ODS_WRITER_PWD="<由运维安全注入>"
--     mysql -h 127.0.0.1 -u root -p dwd_shop < ods_xianzhi_info_analysis.sql
--   CREATE USER/GRANT 不接受绑定参数，下面用 PREPARE + CONCAT 把环境变量值拼进语句，
--   脚本本身仍然只有占位符 '<ODS_WRITER_PWD_FROM_ENV>'。

-- 1) ODS 表（MySQL 8.0）；逻辑分区列为 dt，不做物理 PARTITION BY：
--    日增约 82 行，物理分区无收益，且 MySQL 要求分区键包含所有唯一键会与 PRIMARY KEY(id) 冲突。
CREATE TABLE IF NOT EXISTS dwd_shop.ods_xianzhi_info_analysis (
  id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '代理主键',
  dt                  DATE            NOT NULL                COMMENT '投放日期(源:投放日期)',
  account_name        VARCHAR(128)    NOT NULL DEFAULT ''     COMMENT '投放账户名称',
  media               VARCHAR(64)     NOT NULL DEFAULT ''     COMMENT '媒体',
  plan_name           VARCHAR(255)    NOT NULL DEFAULT ''     COMMENT '投放计划名称',
  landing_page_name   VARCHAR(255)    NOT NULL DEFAULT ''     COMMENT '落地页名称(--表示源端空)',
  plan_id             VARCHAR(64)     NOT NULL DEFAULT ''     COMMENT '投放计划ID(字符串,可能为0)',
  impressions         BIGINT UNSIGNED     DEFAULT NULL        COMMENT '展示',
  clicks              BIGINT UNSIGNED     DEFAULT NULL        COMMENT '点击',
  account_cost        DECIMAL(18,4)       DEFAULT NULL        COMMENT '账面消耗',
  login_users         INT UNSIGNED        DEFAULT NULL        COMMENT '登录用户数',
  lead_signups        INT UNSIGNED        DEFAULT NULL        COMMENT '引流课报名人数',
  lead_signup_cost    DECIMAL(18,4)       DEFAULT NULL        COMMENT '引流课报名成本',
  grade1_signups      INT UNSIGNED        DEFAULT NULL        COMMENT '高一引流课报名人数',
  grade2_signups      INT UNSIGNED        DEFAULT NULL        COMMENT '高二引流课报名人数',
  grade3_signups      INT UNSIGNED        DEFAULT NULL        COMMENT '高三引流课报名人数',
  ingest_batch_id     BIGINT              DEFAULT NULL        COMMENT '最近写入批次ID(血缘)',
  source_url          VARCHAR(255)    NOT NULL DEFAULT ''     COMMENT '采集来源页面',
  created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ods_xianzhi_grain (dt, plan_id, media, landing_page_name),
  KEY idx_ods_xianzhi_dt (dt),
  KEY idx_ods_xianzhi_batch (ingest_batch_id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='先知-代理商系统投放数据ODS,一行=某日某计划某媒体某落地页投放表现';

-- 2) 写入账号：只给这张 ods 表的 SELECT/INSERT/UPDATE。
--    禁止 GRANT ... ON dwd_shop.dwd_* / 禁止 DROP/CREATE/ALTER 等 DDL 权限；
--    数仓查询链路仍走 ro / 会话派生只读账号，红线不破。
SET @ods_writer_pwd = '<ODS_WRITER_PWD_FROM_ENV>';
SET @ods_create_sql = CONCAT(
  'CREATE USER IF NOT EXISTS ''dp_ods_writer''@''%'' IDENTIFIED BY ''', @ods_writer_pwd, '''');
PREPARE ods_create_stmt FROM @ods_create_sql;
EXECUTE ods_create_stmt;
DEALLOCATE PREPARE ods_create_stmt;

SET @ods_alter_sql = CONCAT(
  'ALTER USER ''dp_ods_writer''@''%'' IDENTIFIED BY ''', @ods_writer_pwd, '''');
PREPARE ods_alter_stmt FROM @ods_alter_sql;
EXECUTE ods_alter_stmt;
DEALLOCATE PREPARE ods_alter_stmt;

GRANT SELECT, INSERT, UPDATE ON dwd_shop.ods_xianzhi_info_analysis TO 'dp_ods_writer'@'%';
FLUSH PRIVILEGES;

-- 3) 验收：SHOW GRANTS FOR 'dp_ods_writer'@'%'; 输出中不应出现任何 dwd_* 写权限或 DDL 权限。
