<!-- 在线部署包根 README 模板；相对路径按包根目录解释。 -->

# IMU 推理与分析服务

**服务 1.1.1 · 推理库 1.0.1 · HTTP v2 · Linux x86_64 CPU**

本服务将标准化胸前 IMU 数据转换为活动时间线、各类活动时长和可选的实验性步数，运行在接收方自己的服务器上。模型和依赖已装进镜像，无需另外安装模型环境。

**推荐按本文在线部署。** 录制结束后的分析推荐整段处理，需要持续出结果时使用分块处理；接口与模型说明放在文末。可下载版本及公开访问状态见[交付记录](docs/handoff.md#发布状态)。

![服务数据流](docs/diagrams/overview.svg)

上游提交数据后，服务保存任务，由计算进程处理；业务服务通过查询接口取得结果。数据库和计算进程由 Compose 一起启动。详细组件见[架构说明](docs/architecture.md)。

## 1. 准备服务器

**执行位置：接收方服务器。** 使用可访问 Docker Engine 的账号，准备以下条件：

| 项目 | 要求 |
|---|---|
| 系统 | Linux x86_64，使用 CPU，不需要 GPU |
| 软件 | Docker Engine、`docker compose`、Python 3.10+、curl、tar、sha256sum |
| 内存 | 默认配置建议至少 16 GiB 可用内存，另为宿主机留余量 |
| 磁盘 | 为镜像、数据库增长及备份保留空间 |
| 网络 | 能下载 GitHub Release，并拉取 GHCR 应用镜像和 Docker Hub 的 PostgreSQL 镜像 |

先检查账号和软件：

```bash
uname -m
docker version
docker compose version
python3 --version
```

**成功标志：** 架构为 `x86_64`，`docker version` 能同时显示 Client 和 Server，Compose 和 Python 版本符合上述条件。

## 2. 下载并校验部署包

**执行位置：服务器上的新安装目录。** 以下命令下载推荐的文档修订版在线包。它仍部署服务 1.1.1，镜像版本已固定在包内配置中，无需手填镜像地址。

```bash
set -e
mkdir -p ~/imu-deployments
cd ~/imu-deployments
curl --fail --location --retry 3 -o imu-deploy-1.1.1-docs-r1.tar.gz https://github.com/Kscii/imu-services/releases/download/v1.1.1/imu-deploy-1.1.1-docs-r1.tar.gz
curl --fail --location --retry 3 -o imu-deploy-1.1.1-docs-r1.tar.gz.sha256 https://github.com/Kscii/imu-services/releases/download/v1.1.1/imu-deploy-1.1.1-docs-r1.tar.gz.sha256
sha256sum -c imu-deploy-1.1.1-docs-r1.tar.gz.sha256
tar -xzf imu-deploy-1.1.1-docs-r1.tar.gz
cd imu-deploy-1.1.1-docs-r1
sha256sum -c SHA256SUMS
```

**成功标志：** 两次校验列出的文件均为 `OK`，当前目录包含 `compose.yaml`、`deployment.json` 和 `scripts/`。下载或校验失败时停止，按文末排查表处理。

如果已经取得并解压此包，校验其外层和包内清单后可从第 3 步继续。以下命令都在这个解压目录执行；重新登录 SSH 后也先回到该目录。无需克隆源码仓库。

## 3. 生成本机配置

**作用：** 生成数据库密码、读写 token，并填入本版本镜像地址。

```bash
python3 scripts/create_env.py
```

**成功标志：** 显示 `Created .env (0600) for online installation`，本目录生成仅当前账号可读写的 `.env`，终端不会打印密钥。

首次安装通常无需编辑 `.env`。默认启动 2 个分块计算进程、1 个整段计算进程，两种处理方式都可使用。已有配置不会被覆盖；重新安装或升级时不要删除 `.env` 来绕过这一检查。需要调整资源时见[修改配置](docs/operations.md#修改配置并生效)。

## 4. 拉取镜像并启动

**作用：** 下载已固定版本的软件，初始化数据库并启动服务。

```bash
docker compose pull
docker compose up -d --wait --wait-timeout 300
docker compose ps -a
```

**成功标志：** 拉取和启动命令正常结束，容器状态符合下表：

| 服务 | 正常状态 |
|---|---|
| `postgres`、`api`、`query` | 运行中且健康 |
| `worker` 的两个副本、`worker-whole` | 运行中且健康 |
| `migrate` | `Exited (0)`；初始化完成后退出是正常行为 |

`up` 超时或容器反复退出时，先查看日志，不要继续执行验收。

## 5. 验证本机服务

先检查接入和查询接口，再运行完整样例：

```bash
curl --fail http://127.0.0.1:8100/health/ready
curl --fail http://127.0.0.1:8101/health/ready
python3 scripts/smoke_http.py
```

**成功标志：**

| 检查 | 预期结果 |
|---|---|
| 两个健康接口 | HTTP 200，内容为 `{"status":"ready"}` |
| 样例进程 | 正常结束，最后输出 `stage: completed` |
| 样例报告 | 本目录 `artifacts/smoke/result.json` 中 `passed` 为 `true` |
| 两种模式 | 报告 `results` 中，`incremental` 和 `whole_record` 的 `statistics.observed_s` 均为 `149.0` |

样例自动提交合成数据、等待计算并查询结果和日历统计；其中 150 秒输入含 1 秒缺失。这验证安装与接口链路，不是模型准确率测试。运行期间会产生样例任务及结果。

## 6. 向公司其他服务提供调用入口

本机验收通过后，为调用方准备以下信息：

| 用途 | 默认服务器本机地址 | 调用凭据 |
|---|---|---|
| 创建任务、上传数据、确认录制结束 | `http://127.0.0.1:8100` | `.env` 中的 `IMU_WRITE_TOKEN` |
| 查询状态、结果、活动统计 | `http://127.0.0.1:8101` | `.env` 中的 `IMU_READ_TOKEN` |

调用使用 `Authorization: Bearer <token>`。通过双方约定的配置渠道交付对应 token，不需要把包含数据库密码的整份 `.env` 交给调用方。

默认端口只允许服务器本机访问。跨服务器调用时，由接收方配置能访问这两个端口的 HTTPS 网关／反向代理，再提供实际的接入和查询基础地址。代理保留请求路径和 `Authorization`，支持 NDJSON／gzip 上传，并按[接口指南](docs/api.md#整段上传推荐用于录制结束后的分析)匹配上传大小和超时。PostgreSQL 无需对外开放。

网关就绪后，可在本安装目录沿用本机 token 验证实际地址；先将下面的示例域名替换为部署方提供的值：

```bash
python3 scripts/smoke_http.py --api-url https://imu-ingest.example.com --query-url https://imu-query.example.com
```

这一步通过后，才能确认其他服务所使用的入口可用。本文不指定网关产品或证书管理方式。

## 常见问题

| 现象 | 先检查什么 |
|---|---|
| 下载返回 404／403 | 检查[发布状态](docs/handoff.md#发布状态)、版本和附件名；草稿不能当作公开下载入口 |
| 拉取镜像返回 401／403，或连接超时 | 检查镜像访问权限、服务器到镜像仓库的网络和 Docker 代理配置；不要擅自换成 latest |
| 配置生成提示 `.env` 已存在 | 确认是否为同一安装的既有配置；已有部署按运维流程处理，不删除密码重建 |
| 启动超时、容器 unhealthy 或反复退出 | 执行 `docker compose ps -a` 和 `docker compose logs --tail 100 migrate api worker worker-whole query postgres`，检查初始化错误、端口和内存 |
| 样例返回 401／503 或超时 | 检查当前目录的 token、健康状态、接入开关和 worker；已生成任务先查其状态，再决定是否重跑，见[运维说明](docs/operations.md) |

## 网络受限时的选择

推荐在线部署。如果服务器无法访问镜像仓库，可使用同版本[完整离线包](https://github.com/Kscii/imu-services/releases/tag/v1.1.1)，按包内根 README 校验和导入镜像；主机基础软件仍需预先安装。离线安装不改变模型或接口。

## 后续阅读

| 任务 | 文档 |
|---|---|
| 日常检查、改配置、备份、升级、恢复 | [运维说明](docs/operations.md) |
| 上传数据、查询结果和统计 | [接口指南](docs/api.md) |
| 了解组件、计算与任务恢复 | [架构说明](docs/architecture.md) |
| 模型、数据集、指标及走跑候选 | [模型手册](https://github.com/Kscii/imu-inference/blob/main/docs/model.md)、[走跑说明](https://github.com/Kscii/imu-inference/blob/main/docs/walk-run.md) |
| 交付内容、版本对应关系和发布状态 | [交付记录](docs/handoff.md) |
| 修改源码、构建与开发测试 | [开发说明](docs/development.md) |

自有服务代码及文档按 [MIT](LICENSE) 授权，模型及第三方条款见[第三方声明](THIRD_PARTY_NOTICES.md)。
