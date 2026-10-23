# 日常运维、升级与恢复

适用服务 1.1.1、数据库 schema 2。首次安装按[在线部署教程](../README.md)执行。本页所有命令都在**服务器上当前使用的安装目录**执行，该目录包含 `compose.yaml` 和 `.env`。

| 现在要做什么 | 阅读位置 |
|---|---|
| 判断是否正常、定位报错 | [查看状态和日志](#查看状态和日志) |
| 调整端口、计算资源或功能开关 | [修改配置并生效](#修改配置并生效) |
| 保存可恢复的数据副本 | [备份数据库和配置](#备份数据库和配置) |
| 安装新软件版本 | [升级服务](#升级服务) |
| 新软件异常，换回旧版本 | [回滚版本](#回滚版本) |
| 从备份找回数据 | [恢复数据库](#恢复数据库) |

## 查看状态和日志

```bash
docker compose ps -a
curl --fail http://127.0.0.1:8100/health/ready
curl --fail http://127.0.0.1:8101/health/ready
docker compose logs --tail 100 migrate api worker worker-whole query postgres
```

如果修改过端口，上述地址也使用实际端口。正常时，应用、worker 和数据库运行且健康，两个接口返回 `{"status":"ready"}`；`migrate` 为 `Exited (0)`。

| 现象 | 处理顺序 |
|---|---|
| 初始化未完成，应用未启动 | 先查看 `postgres` 和 `migrate` 日志；修正具体错误后再执行启动命令 |
| API 可查询，但任务迟迟没有结果 | 查询任务状态；`waiting` 可能在等连续输入、上下文或 seal，再检查对应 worker 的日志与健康 |
| worker 退出或反复重启 | 检查日志与宿主机内存；输入和已提交结果在数据库中，不必删除任务或重建数据库 |
| 返回 429／503 | 查看队列和容量；调用方保留数据，按接口返回的重试要求退避 |
| 容器持续 unhealthy | 查看错误原因；restart 策略处理进程退出，不保证自动处理所有 unhealthy 情况 |

查询接口 `GET /v2/operations` 提供任务数量、队列占用及 worker 心跳；`GET /metrics` 提供 HTTP 指标，两者使用读 token。持续运行时重点监控队列积压、失败任务、worker 心跳、主机内存、数据库磁盘和最近成功备份时间。

## 修改配置并生效

先保存原 `.env`，再编辑需要改变的项。不要把该文件提交到 Git，也不要重新生成数据库密码来处理连接故障。

| 配置 | 当前默认值 | 影响 |
|---|---|---|
| `IMU_API_PORT` / `IMU_QUERY_PORT` | 8100 / 8101 | 宿主机回环监听端口；改变后同步更新网关和本机检查地址 |
| `IMU_WORKERS` / `IMU_WHOLE_WORKERS` | 2 / 1 | 两类计算进程的副本数 |
| `IMU_THREADS` | 2 | 单进程推理线程配置 |
| `IMU_WHOLE_MEMORY` | 8g | 单个整段计算容器的内存上限 |
| `IMU_STEPS_ENABLED` | true | GP2 实验计步；关闭后任务兼容性需检查 |
| `IMU_SIGN_ADAPTATION` | false | 实验性纵向符号适配；不作为任意佩戴校准 |
| `IMU_ADMIT_NEW_RUNS` | true | 是否接收新任务；升级维护时使用 |

仅修改端口、资源或副本数时，选择维护时间，修改 `.env` 后执行：

```bash
docker compose up -d --wait --wait-timeout 300
docker compose ps -a
```

**检查：** 新的端口和副本数符合配置，容器恢复健康。`docker compose restart` 不会重新读取并应用新的容器环境配置。

影响任务处理配置的功能开关调整，先按下节升级流程暂停新增并排空旧任务，避免未完成任务与新配置不兼容。若修改后异常，恢复原配置，再执行上述 `up` 命令并查看日志。

应用重启或替换容器会继续使用数据库持久卷。**不要在生产执行 `docker compose down -v`**，它会删除持久卷。项目名固定为 `imu`；随意换项目名或 `-p` 会选到另一组资源，可能看到一个新空库。

## 备份数据库和配置

备份分为两部分：数据库 dump，以及单独保护的 `.env`、Compose 文件和对应版本包。数据库 dump 不包含密码文件或镜像。

**前提：** PostgreSQL 正常运行；执行脚本的服务器账号具有写入备份目录的权限。异地备份还需提前配置 rsync、可信 SSH 连接和目标目录。

本机备份命令为 `bash operations/backup.sh`。它生成 `backups/imu-时间戳.dump` 和同名 `.sha256`；只有本机副本时会返回 **2**，表示尚未完成异地复制。

配置好异地主机后，替换下面的账号、主机和目录执行：

```bash
IMU_BACKUP_DESTINATION='backupuser@backup-host:/srv/imu-backups/' bash operations/backup.sh
```

| 返回结果 | 含义与下一步 |
|---|---|
| 退出码 0 | dump 和校验文件已复制到配置的目的地；检查远端文件并按保留策略保存 |
| 退出码 2，提示缺少目的地 | 本机备份已生成，但还没有异地副本；配置目的地后补齐异地备份 |
| 其他错误 | 检查数据库、磁盘、SSH 和 rsync；不能将本次任务记为成功备份 |

脚本从进程环境读取 `IMU_BACKUP_DESTINATION`，仅写入 `.env` 不会自动传给该 shell 脚本。脚本也不会自动建立定时任务；部署方需设置 cron／systemd timer、检查退出码，并安排恢复演练。

## 升级服务

修改说明文档无需升级运行中的服务。下面用于安装新的服务、推理库或模型版本；数据库主版本升级需要单独方案，不包含在这条流程中。

### 1. 暂停新增，完成旧任务

通知上游暂存新录制，在**旧安装目录**的 `.env` 中设置 `IMU_ADMIT_NEW_RUNS=false`，然后执行：

```bash
docker compose up -d --no-deps --wait api
```

已有分块任务仍需上传完数据并调用 seal。等待计算完成，处理仍可恢复的失败任务。可用以下只读检查确认需要保留输入的旧任务已经清空：

```bash
docker compose exec -T postgres psql -U imu_admin -d imu -c "SELECT count(*) AS unfinished_runs FROM runs WHERE status <> 'completed' AND NOT input_cleared;"
```

**成功标志：** `unfinished_runs` 为 0。非零时通过任务接口和日志处理原因，不直接改数据库状态来绕过检查。

### 2. 保留旧版本，准备新目录

完成并校验数据库备份，保护好旧 `.env`、Compose 文件和镜像。校验、解压新版本在线包到**新的安装目录**，将旧 `.env` 安全复制过去；保留原密码、token 和接入暂停状态，不运行首次配置生成脚本。

按目标版本的说明合并新增配置。新旧目录必须使用同一个 Compose 项目 `imu` 和数据库卷，避免误建空数据库。下列步骤从**新安装目录**执行。

### 3. 安装并检查新版本

从新包 `deployment.json` 读取镜像与源码标识，调用配套升级脚本；无需人工复制长摘要：

```bash
python3 - <<'PY'
import json
import subprocess
from pathlib import Path
release = json.loads(Path("deployment.json").read_text())
subprocess.run([
    "bash", "operations/deploy.sh",
    release["images"]["runtime"], release["images"]["query"],
    release["source_commit"],
], check=True)
PY
```

脚本会校验镜像来源、保存 `.env.previous`、停止应用、生成部署前备份、检查任务兼容性、迁移并启动服务。

**成功标志：** 命令正常结束，服务健康，产生 `release-installed.json`，版本与目标一致。此时新任务接入仍然暂停。失败时先看脚本输出，按下一节处理；不能忽略错误继续放行。

### 4. 受控验收，再恢复业务

保持上游业务提交暂停，在新 `.env` 中把 `IMU_ADMIT_NEW_RUNS` 改为 true，再执行：

```bash
docker compose up -d --no-deps --wait api
python3 scripts/smoke_http.py
```

样例需要创建新任务，因此必须在开关开启后执行。报告通过后，才通知上游恢复业务提交。失败时再次暂停新增，保留报告和任务状态，调查或回滚。

当前库 1.0.1 的版本和代码摘要改变了任务指纹，不能拿旧库检查点直接续算，即使模型权重相同也要排空。后续升级同样以兼容性检查和目标版本说明为准。

## 回滚版本

回滚是换回旧软件；恢复数据库是找回备份中的数据，二者不能混用。

| 情况 | 怎么处理 |
|---|---|
| 升级脚本中途失败，schema 未变化 | 脚本尝试恢复旧 `.env` 和镜像；操作者仍需检查容器健康及失败原因，再决定何时恢复业务 |
| 升级脚本发现 schema 已变化 | 服务保持停止；按恢复流程使用升级前数据库备份、旧配置和旧软件 |
| 升级完成后的验收或业务出现异常 | 暂停新增并保持业务流量停止；核对数据库和任务是否兼容旧版本，再执行人工回退 |

兼容旧版本时，停止候选应用 `docker compose stop api worker worker-whole query`，回到保存完整的旧安装目录，保持接入暂停，启动旧应用并核验版本、健康和样例，然后恢复业务。相同 schema 号不自动保证所有未来版本的数据语义兼容；不能确认时先保留现场，不盲目覆盖数据库。

## 恢复数据库

用于数据库损坏、主机迁移或需要恢复升级前数据的情况。**前提：** 已有受保护的备份和配套配置／软件，业务写入已暂停。先用 `.sha256` 校验对应 dump；校验失败时不要恢复。

在目标安装目录设置实际备份路径与一个尚不存在的新数据库名：

```bash
set -e
backup_file='backups/imu-YYYYMMDDTHHMMSSZ.dump'
restore_db='imu_restore_YYYYMMDD'
sha256sum -c "${backup_file}.sha256"
bash operations/restore.sh "$backup_file" "$restore_db"
```

替换示例时间戳后再执行，若校验返回非零则停止。脚本仅恢复到**新空数据库**，成功后会提示核验并重新授权；它不会覆盖当前 `imu`，也不会自动切换服务。

核对恢复库的结构版本及记录数量：

```bash
docker compose exec -T postgres psql -U imu_admin -d "$restore_db" -c "SELECT max(version) AS schema_version FROM schema_migrations; SELECT status,count(*) FROM runs GROUP BY status; SELECT count(*) AS result_rows FROM results;"
```

当前版本应为 schema 2；任务及结果数量应与备份时间点对应，不能拿备份后新增的数据量作比较。还需要按已保存的任务样例核对结果。后续切换由运维在维护窗完成：

1. 停止 API、两类 worker 和 query，确认没有业务写入或旧数据库连接。
2. 隔离保留旧库，让验证通过的恢复库接替默认逻辑库名 `imu`。现有 Compose、备份和升级脚本都使用这个名字，不能只改某一个服务的数据库地址。
3. 使用与备份匹配的软件及配置，重新安装数据库角色与权限；当前版本可通过 `docker compose run --rm --no-deps migrate` 执行初始化。
4. 启动并检查服务，在业务暂停时完成样例验收，再恢复入口。

**恢复完成的标准是业务入口和结果验证通过，不只是导入命令成功。** 当前没有封装自动切库命令；操作失败时保持业务暂停并保留旧库、恢复库和日志，先定位失败阶段。备份之后新增的数据需从上游保留的原始记录重放，单机备份不提供零数据损失保证。
