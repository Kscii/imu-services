# IMU 推理与分析服务

**服务 1.1.1 · 推理库 1.0.1 · HTTP v2 · Linux x86_64 CPU**

将标准化胸前 IMU 数据转成四类活动时间线、累计时长和实验性计步结果。包含持久队列、分块／整段计算、查询与小时／日／周分析。本次补丁整理交付与安装，不更换模型或提高准确率。

## 交接入口

| 任务 | 阅读材料 |
|---|---|
| 安装、启动、更新和恢复 | [部署与运维](docs/operations.md) |
| 上传、查询、统计活动和步数 | [接口指南](docs/api.md)、[调用示例](examples/) |
| 服务拓扑、排队、结果和恢复 | [架构说明](docs/architecture.md) |
| 模型、训练来源、划分、指标和局限 | [完整模型手册](https://github.com/Kscii/imu-inference/blob/v1.0.1/docs/model.md) |
| 版本、产物、验证与责任边界 | [交付清单](docs/handoff.md)、对应 Release 的验证附件 |

## 在线安装：推荐入口

主机需要 Linux x86_64、Docker Engine、Docker Compose 和 Python 3.10+。Python 只用于配置和接口检查；模型的 Python 3.12 CPU 环境包含在镜像中。默认资源配置建议至少 16 GiB 可用内存，另为宿主机留余量。

在新的空目录执行。网络下载或校验失败应停止，不能跳过校验继续解压：

```bash
set -e
curl --fail --location --retry 3 -o imu-deploy-1.1.1.tar.gz https://github.com/Kscii/imu-services/releases/download/v1.1.1/imu-deploy-1.1.1.tar.gz
curl --fail --location --retry 3 -o imu-deploy-1.1.1.tar.gz.sha256 https://github.com/Kscii/imu-services/releases/download/v1.1.1/imu-deploy-1.1.1.tar.gz.sha256
sha256sum -c imu-deploy-1.1.1.tar.gz.sha256
tar -xzf imu-deploy-1.1.1.tar.gz
cd imu-deploy-1.1.1
sha256sum -c SHA256SUMS
python3 scripts/create_env.py
docker compose pull
docker compose up -d --wait --wait-timeout 300
python3 scripts/smoke_http.py
```

轻量部署包固定 runtime/query 的 GHCR 摘要，无需手填镜像地址或安装模型依赖。公开安装无需 GitHub 登录；调用 API 仍需本地生成的读写 token。`.env` 以 0600 权限独占创建，不打印密钥、不覆盖已有配置，也不提交 Git。

检查使用 150 秒合成六轴信号及 1 秒缺失；两种模式均应得到 149 秒有效观测，并通过日历统计一致性检查。这是工程功能验证，不是识别准确率测试。API/query 默认仅监听本机 8100/8101，远程接入按运维文档配置 TLS 入口。

## 完整离线安装

下载同一 Release 的 `imu-delivery-1.1.1.tar.gz` 和同名 `.sha256`，先校验外层文件再解压。在解压的完整交付包根目录执行：

```bash
set -e
sha256sum -c SHA256SUMS
docker load -i imu-images-1.1.1.tar.gz
cd imu-services
python3 scripts/create_env.py --mode offline
docker compose -f compose.yaml -f compose.offline.yaml up -d --wait --wait-timeout 300
python3 scripts/smoke_http.py
```

离线包包含应用和 PostgreSQL 镜像、两个仓库源码、推理库安装产物、文档及验证记录。在线、离线路径对应相同应用镜像内容。

## 开发与更新

开发环境需要 Python 3.12 和 uv。`vendor/wheels/` 固定推理库版本及摘要：

```bash
uv sync --frozen --extra worker
uv run --frozen --extra worker ruff check .
docker build --target runtime -t localhost/imu-runtime:1.1.1 .
docker build --target query -t localhost/imu-query:1.1.1 .
```

数据库测试必须提供隔离 PostgreSQL，跳过数据库测试不能作为完整验收。升级前暂停新任务并排空旧任务，保留备份和旧镜像。1.0.1 库的版本／代码摘要会改变任务指纹，不能直接恢复旧库检查点。

## 边界与授权

上游负责设备解包、对时、六轴标准化、原始记录及人员归属。本服务不解原始设备包，不推断未观测时长；没有经验证的独立走跑分类、跌倒或医学诊断能力。GP2 和 E7 的实验标记及默认行为保持原版。

交付包括固定模型推理与服务源码，不包括原始受控数据和完整重训工程。Compose 提供单机部署与恢复；客户主机、TLS、异地备份、跨机高可用和设备联调由接收环境落实。现有参考数据不是独立客户盲测，发布不代表客户准确率达到 80%。

自有服务代码与文档由 Kscii 按 [MIT](LICENSE) 授权。模型及第三方部分保留单独条款，见 [第三方声明](THIRD_PARTY_NOTICES.md) 和推理库 MODEL_LICENSES.md。
