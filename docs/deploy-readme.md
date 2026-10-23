# IMU 1.1.1 在线部署包

服务 1.1.1，推理库 1.0.1，HTTP v2，数据库 schema 2。Linux x86_64 CPU，Docker Engine + Compose，主机 Python 3.10+。默认配置建议至少 16 GiB 可用内存，并为宿主机留余量。

本包不包含模型／镜像；Compose 按 deployment.json 中的固定 GHCR 摘要拉取，模型随镜像提供。公开安装不需要 GitHub 登录，接口调用需要本地读写 token。

## 安装

先核对下载文件的外层 SHA256，再解压并在本目录执行：

```bash
set -e
sha256sum -c SHA256SUMS
python3 scripts/create_env.py
docker compose pull
docker compose up -d --wait --wait-timeout 300
python3 scripts/smoke_http.py
```

环境生成失败、下载失败或摘要校验失败时停止；不跳过、不自动换 latest。.env 已存在时不会覆盖。验收样例为 150 秒合成信号、1 秒缺失，两个模式均应报告 149 秒有效观测；不证明模型准确率。

## 文档与源码

- [接口](docs/api.md)、[运维](docs/operations.md)、[架构](docs/architecture.md)。
- [服务源码及完整离线包](https://github.com/Kscii/imu-services/releases/tag/v1.1.1)。
- [模型完整手册](https://github.com/Kscii/imu-inference/blob/v1.0.1/docs/model.md)及[模型授权](https://github.com/Kscii/imu-inference/blob/v1.0.1/MODEL_LICENSES.md)。

配置及密钥仅保存在本地，API 默认监听本机回环地址。客户远程接入、TLS、备份目的地和生产主机另行配置。升级先排空旧任务，不混用不同版本检查点。
