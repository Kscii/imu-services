# 服务 1.1.1 交付补丁

配套推理库 1.0.1。模型权重及算法不变，HTTP v2、数据库 schema 2、两种计算模式及实验功能默认值不变。

- 新增轻量在线部署包：由固定镜像摘要生成 deployment.json，一条 create_env 命令生成本地凭据，避免手抄摘要或依赖 latest。
- 保留完整离线包；源码快照、wheel、sdist、镜像及新验证报告分别绑定摘要。
- 补齐服务架构、交付清单及运维入口，模型手册由推理库维护。
- 推荐在线包为 `imu-deploy-1.1.1-docs-r1.tar.gz`，已补齐配套文档和静态架构图；运行文件及镜像保持原版本。
- 自有服务代码采用 MIT，署名 Kscii；推理库 R15、E1、E5 权重采用 CC BY-SA 4.0，其余可授权自有权重采用 MIT，保留第三方条款和完整许可文本。旧 Git 历史、unikey 和旧 Release 草稿保留。

升级前排空已有任务：库版本常量影响代码摘要和任务指纹，旧检查点不可跨库版本恢复。升级不自动提高准确率，也不改变已有工程验证与客户独立验收的边界。

2026-10-24 已正式发布，两个 GHCR 应用镜像均可匿名拉取。公开在线包在空 Docker 凭据目录、新数据库和默认 worker 配置下通过安装、两种模式上传、查询及统计验收，见 Release `recipient-online-acceptance-1.1.1-20261024.json`。原工程验证见 `acceptance.json` 和完整包 `validation/`；生产接入及客户准确率另行验收。包内发布状态为打包时快照，最新状态见[在线交付记录](https://github.com/Kscii/imu-services/blob/main/docs/handoff.md)。
