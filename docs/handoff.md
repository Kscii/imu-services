# 交付内容、版本与发布记录

交付版本：服务 1.1.1、推理库 1.0.1、HTTP v2、数据库 schema 2。安装入口为[在线部署教程](../README.md)。本页供接收核对和维护者追溯，不是首次安装的前置阅读材料。

## 发布状态

2026-10-24 核对：两个仓库已公开，[推理库 v1.0.1](https://github.com/Kscii/imu-inference/releases/tag/v1.0.1) 和[服务 v1.1.1](https://github.com/Kscii/imu-services/releases/tag/v1.1.1) 均已正式发布。runtime/query 的固定镜像摘要均可匿名访问。**公开下载、在线安装和两种模式的接口验收已通过。**

验收使用公开在线包、空 Docker 凭据目录、新数据库和默认配置，未使用维护者 GitHub／GHCR 凭据。完整结果见 [2026-10-24 在线接收报告](https://github.com/Kscii/imu-services/releases/download/v1.1.1/recipient-online-acceptance-1.1.1-20261024.json)及同名 `.sha256` 附件。

## 交付对象

| 对象 | 内容与入口 |
|---|---|
| 推理库仓库 | [Kscii/imu-inference](https://github.com/Kscii/imu-inference)：模型、算法、Python API、训练来源与指标证据 |
| 服务仓库 | [Kscii/imu-services](https://github.com/Kscii/imu-services)：接入、持久队列、计算、查询统计、部署和运维 |
| 推理库 Release v1.0.1 | wheel、源码分发包、校验与构建验证 |
| 推荐在线包 | `imu-deploy-1.1.1-docs-r1.tar.gz` 及同名 `.sha256`；配套文档和静态图已包含，无需额外文档补丁 |
| 完整离线包 | `imu-delivery-1.1.1.tar.gz` 及同名 `.sha256`；供镜像仓库不可访问时使用，安装见其根 README |
| 镜像 | GHCR 的 runtime/query 应用镜像，以及 Compose 固定的 PostgreSQL 镜像 |
| 验证材料 | 随 Release 提供构建、镜像一致性、容器恢复、离线接收等报告；按报告范围解读 |

无需第三个源码仓库。旧 Git 历史及标签保留，旧 Release 草稿保留，参与者评估表使用 unikey。独立走跑二分类不在当前库中，研究候选和验证情况见[走跑说明](https://github.com/Kscii/imu-inference/blob/main/docs/walk-run.md)。

## 如何核对版本

| 标识 | 核对什么 | 在哪里找 |
|---|---|---|
| 软件版本 | 服务与推理库的版本号 | `release-versions.json`、升级脚本生成的 `release-installed.json` |
| 镜像摘要 | 实际安装的镜像内容 | `deployment.json` 的 `images` |
| 源码 commit | 镜像构建时对应的服务源码 | `deployment.json` 的 `source_commit` 与镜像来源标签 |
| 包校验值 | 下载文件是否与发布制品一致 | 外层 `.sha256` 和包内 `SHA256SUMS` |
| 文档修订 | 当前说明文档的来源及基础包 | 文档修订版中的 `documentation-revision.json` |

GHCR 是 GitHub 的镜像仓库。`ghcr.io/…@sha256:…` 中，`@` 后的摘要固定镜像内容；版本标签可以重新指向其他内容，摘要则用于选择同一份镜像。配置脚本自动读取配套引用，首次部署无需手动填写。摘要不证明模型准确率；源码 commit、镜像摘要、压缩包校验值也不能互换。

冻结服务源码为 `ee19c6b370de241756793a4e2500d0fd51959abc`，标签 v1.1.1；冻结推理库源码为 `8c5b11e81fe1aacd29eecf746da3041d74a9cc41`，标签 v1.0.1。后续仅修改文档的 main 提交不替代镜像来源标识，升级脚本从目标包的 `deployment.json` 读取构建源码，而非使用当前 main SHA。

## 本次文档修订

`docs-r1` 重写在线安装和运维说明，补齐文档导航与静态图；服务、模型、脚本、Compose 配置和 `deployment.json` 均沿用原 `imu-deploy-1.1.1.tar.gz`。软件版本不变，已部署用户无需更新镜像。

新在线包使用独立文件名和校验值，并在 `documentation-revision.json` 中分别记录文档提交及冻结运行软件来源。原在线包、完整包、历史文档补充包、发布标签和镜像保持原样。新接收方优先使用 docs-r1 在线包；历史包内的旧版文档仅代表当时快照。

docs-r1 在正式发布前完成打包，因此包内本页及文档校验报告中的“草稿／query 401”保留为当时的检查快照。当前发布状态以[在线交付记录](https://github.com/Kscii/imu-services/blob/main/docs/handoff.md#发布状态)、正式 Release 和新增在线接收报告为准；无需修改已下载包的文件或校验清单。

## 接收检查与验证记录

接收方按在线 README 下载、校验、生成配置、拉取镜像、启动并运行样例，核验两种模式、查询和日历统计。公开接收验收必须使用没有维护者 GitHub／镜像仓库凭据的环境；API 凭据由接收方本地生成。

文档修订检查包括包内链接、图示、命令语法、配置生成和新旧包内容比较。随后完成的公开在线接收验收覆盖：匿名下载与内外校验、匿名拉取镜像、默认 2 个分块 worker 和 1 个整段 worker 启动、分块上传与封口、整段 gzip 上传、结果查询及日历／活动统计。两种模式均处理完 150 秒合成输入，并报告 149 秒有效观测，正确排除其中 1 秒缺失。

验收在已有 Ubuntu Docker 主机的新项目和新数据库中执行，Docker 客户端凭据目录全程为空；共享 daemon 可能复用镜像层缓存，不是全新操作系统或空镜像缓存测试。本次未验证客户 HTTPS 网关、持续并发负载或客户数据准确率。测试容器及测试数据库已清理，原有演示与验证服务保持运行。CI 通过、软件交付、生产接入和客户准确率验收分别记录。

原模型证据在源码仓库的 `evidence/model-baseline-1.0.0`，历史服务验证在 `reports/1.1.0`；1.1.1 验证随 Release 和完整包的 `validation/` 分发。开发构建入口见[开发说明](development.md)。

模型准确率、时长误差及适用范围见[模型手册](https://github.com/Kscii/imu-inference/blob/main/docs/model.md)。样例为合成信号，仅验证工程链路；原始设备数据和完整重训工程不随包提供。实际负载、目标人群准确率、生产网关、监控和备份须在接收环境落实。

自有代码与文档采用 MIT，署名 Kscii；R15、E1、E5 权重采用 CC BY-SA 4.0，其余可授权自有权重采用 MIT，第三方条款保留。具体范围见[模型许可](https://github.com/Kscii/imu-inference/blob/v1.0.1/MODEL_LICENSES.md)和[第三方声明](../THIRD_PARTY_NOTICES.md)。
