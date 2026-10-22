# 服务 1.1.0 工程验收

本次固定推理库 1.0.0、模型与算法指纹；服务新增小时、日、周计步统计。HTTP 主版本 2 和数据库 schema 2 不变。

| 检查 | 结果与证据 |
|---|---|
| 服务单元、数据库和 HTTP 测试 | 67 项通过，0 跳过，见 service-tests.xml 和 tests.json |
| 固定推理库测试 | 27 项通过，0 跳过，见库仓库 provenance/delivery-library-tests.xml |
| 真实 CPU 模型容器测试 | 分块与整段 worker 强杀后恢复；结果、步事件与直接推理一致；单记录和跨记录日历接口一致，见 container-acceptance.json |
| 镜像与源码 | 两个镜像的服务源码、固定库全部 197 个文件一致；查询镜像不含 Torch，见 image-consistency.json |
| 离线安装 | 对导出镜像逐层校验，再用新空数据库、不拉取镜像完成两种模式 HTTP 回放，见 image-export.json 和 clean-install.json |
| 分析请求上限 | 31 天、各 100000 个活动与步数区间、100000 个步事件；8.28 秒，进程峰值 RSS 318.17 MiB，响应约 497 kB，见 analysis-capacity.json |

容量测试为指定 Arch 主机的单请求合成测试，不含数据库和网络耗时，不能当作生产并发承诺。容器回放也是合成信号，用于验证服务与计算一致性，不是新准确率证据。

最终判定及证据摘要在 acceptance.json。模型的历史研究证据保留在 ../../evidence/model-baseline-1.0.0；模型训练与评价口径见推理库 docs/model.md。尚未完成独立客户准确率验收与生产部署。
