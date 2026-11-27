-- 数据源库（datapro_source）：仅首次初始化空数据卷时由 MySQL 入口脚本执行。
-- 生产请由 DBA 预建并只授予摄取服务账号，不要复用业务库账号的全部权限。
CREATE DATABASE IF NOT EXISTS `datapro_source` DEFAULT CHARACTER SET utf8mb4;

-- MYSQL_USER 由镜像入口创建，默认只授权 MYSQL_DATABASE；这里补第二个库的权限。
GRANT ALL PRIVILEGES ON `datapro_source`.* TO 'datapro'@'%';

FLUSH PRIVILEGES;
