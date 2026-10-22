# 部署、升级与恢复

## 首次部署

Linux x86_64、Docker Engine 与 Compose；开发构建另需 Python 3.12。默认 2 个分块 worker、1 个整段 worker，容器内存额度合计约 14 GiB；建议至少 16 GiB 可用内存并为宿主机留余量。CPU、磁盘与并发按真实负载调整。

离线包按 [README](../README.md) 校验并加载。在线部署从私有 GHCR 拉取 runtime/query，将完整 @sha256 摘要填入 .env，不使用 latest。运行时不下载模型。

scripts/create_env.py 独占创建权限 0600 的 .env，生成数据库密码与读写 token，不打印凭据。按 .env.example 调整资源和线程。PostgreSQL 不开放宿主机端口；API/query 只监听 127.0.0.1:8100/8101，跨主机接入由客户部署受控 TLS 入口。

```bash
docker compose ps
curl --fail http://127.0.0.1:8100/health/ready
curl --fail http://127.0.0.1:8101/health/ready
python3 scripts/smoke_http.py
```

GP2 默认开启且标为实验性，可用 IMU_STEPS_ENABLED=false 关闭。E7 需显式 whole_record 请求；纵向符号适配默认关闭。分块与整段池独立，队列有限，过载返回可重试错误。

## 日常运行与备份

检查 HTTP 延迟、失败任务、队列积压、worker 心跳、数据库/WAL/磁盘和备份时间。/metrics 提供 HTTP 指标，/v2/operations 提供队列状态。unhealthy 不保证自动重启；退出由 restart 策略恢复，持续异常需外部报警。

租约和 fencing token 防止旧 worker 覆盖结果；结果、进度、检查点事务提交。查询角色不能读原始输入。终态输入清理不是 WAL、备份或底层磁盘的物理擦除。

operations/backup.sh 导出 custom-format dump 与摘要。通过受保护环境提供 IMU_BACKUP_DESTINATION 才复制异地；只有本机副本时返回 2，不能认作异地备份成功。先落实每日备份与恢复演练，再按允许丢失的窗口调整频率。

校验备份后，operations/restore.sh dump-file imu_restore_NAME 恢复到新空数据库，不覆盖现有库。核对角色、schema、任务及结果后切换。配置、备份、密钥和镜像摘要需受保护保存，保留期与异地权限由接收方落实。

## 更新与回滚

1. 设置 IMU_ADMIT_NEW_RUNS=false，重启 API；继续处理已有上传和 seal，等待任务完成。
2. 保存备份、旧配置和旧镜像，选定同一源码版本的 runtime/query 不可变摘要。
3. operations/deploy.sh 检查指纹与 schema，迁移、启动并验证健康；失败时停止候选，成功后仍保持接入暂停。
4. 验证安装清单和样例，设置接入开关 true，再执行 docker compose up -d --no-deps --wait api。

服务 1.0.0 → 1.1.0 不改 schema 2 与模型指纹。换模型需要新的不可变 wheel、摘要和锁文件，回归后构建镜像，不热换既有任务。相同 schema 可回退兼容镜像；跨 schema 必须恢复配套备份与旧配置。

库的小版本兼容取决于输入、标签、结构与语义，不仅是函数签名。模型权重和固定算法共同决定结果，不能原地替换 wheel 内文件。

## 验证和发布

```bash
uv sync --frozen --extra worker
uv run --frozen --extra worker ruff check .
IMU_TEST_DATABASE_URL='postgresql://USER:PASSWORD@HOST/TEST_DB' uv run --frozen --extra worker pytest -q
```

数据库测试只创建并清理随机 schema；缺少连接会跳过，不能算验收通过。scripts/container_acceptance.py 验证真实模型及容器恢复，清理自己的 Compose 项目。模型原证据位于 evidence/model-baseline-1.0.0，本服务验证位于 reports/1.1.0。

GitHub main/PR 验证代码。维护者先完成验收、提交报告并打包，建立 Release 草稿并上传离线包及摘要；随后手动运行 publish 工作流，将草稿中已校验的同一镜像上传私有 GHCR，并附上 registry-images.txt 与权限记录，不自动部署生产。离线包包含配套源码、模型库、镜像、报告和摘要。仓库与镜像权限分别核验；客户账号、主机、TLS 和备份配置由交接时指定。
