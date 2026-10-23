# 服务架构与数据流

服务 1.1.1、推理库 1.0.1、HTTP v2、数据库 schema 2。标准输入为规范胸前佩戴的 25 Hz 六轴及掩码；模型计算细节见推理库模型手册。一个任务在创建时确定计算模式、模型及配置，已开始任务不热换模型。

## 先看数据如何流动

![服务数据流](diagrams/overview.svg)

[打开原尺寸图](diagrams/overview.svg) · [Mermaid 源文件](diagrams/overview.mmd)

1. 上游通过接入 API 提交标准化 IMU 数据，服务把输入和任务持久保存到 PostgreSQL。
2. 计算进程领取任务，调用镜像内的模型与算法，再把结果写入数据库。
3. 调用方通过查询 API 读取状态、时间线和活动统计，不直接访问数据库。

PostgreSQL 是本服务的任务与结果数据库，可以与公司的设备原始数据库并存。上传请求被接受不等于计算完成；两个 API 的用途和读写 token 见[安装后的对接说明](../README.md#6-向公司其他服务提供调用入口)。

## 组件与镜像

| 镜像 | 启动的服务 | 作用 |
|---|---|---|
| runtime | `api`、`worker`、`worker-whole` | 接入及两类计算进程；包括模型和运行依赖 |
| query | `query`、`migrate` | 查询统计；初始化数据库结构和角色 |
| PostgreSQL | `postgres` | 保存任务、队列、检查点及结果 |

默认 2 个分块 worker、1 个整段 worker，各进程有资源限额；初始化任务完成后退出。数据库文件放在持久卷中，应用容器替换后数据仍可保留。运行时无需下载权重；不依赖独立 Redis。

## 单机部署拓扑

![deployment 架构图](diagrams/deployment.svg)

[打开原尺寸图](diagrams/deployment.svg) · [Mermaid 源文件](diagrams/deployment.mmd)

<details>
<summary>查看 Mermaid 源码</summary>

```mermaid
flowchart TB
    U["公司上游服务<br/>标准化数据／保留原始记录"]
    B["公司业务服务<br/>查询结果／活动统计"]
    subgraph HOST["客户服务器 · 单机 Docker Compose"]
        direction TB
        G["受控 HTTPS 入口<br/>由部署环境配置"]
        subgraph RT["复用 runtime 镜像"]
            A["API · 8100<br/>校验／幂等／接入限额"]
            I["分块 worker × 2<br/>SB1 / FC1 · 持续输出"]
            W["整段 worker × 1<br/>E7 · 录制后分析推荐"]
        end
        subgraph QT["复用 query 镜像"]
            Q["query · 8101<br/>只读查询／小时日周统计"]
            M["migrate<br/>启动时初始化 schema 和角色"]
        end
        P[("PostgreSQL 18<br/>输入／持久队列／租约<br/>检查点／结果")]
        V[("数据库持久卷")]
        BK["备份脚本<br/>导出 dump 与校验文件"]
        G -->|"写 token"| A
        G -->|"读 token"| Q
        A -->|"事务写入"| P
        I <-->|"领任务／提交结果"| P
        W <-->|"领任务／提交结果"| P
        Q -->|"只读角色"| P
        M -->|"初始化后退出"| P
        P --- V
        P --> BK
    end
    U -->|"完整记录或增量数据"| G
    B -->|"HTTP v2"| G
    BK -.->|"需配置定时任务和目的地"| OFF["异地主机／备份存储"]
```

</details>

API 与 worker 复用 runtime 镜像，query 与初始化复用 query 镜像；两个镜像连同 PostgreSQL 构成完整部署。数据库不暴露宿主机端口，API/query 默认只绑定回环地址，受控跨机入口由客户配置 TLS。持久队列位于 PostgreSQL，不依赖单独 Redis 或消息队列基础设施。

## 接入、计算与查询

![request-flow 架构图](diagrams/request-flow.svg)

[打开原尺寸图](diagrams/request-flow.svg) · [Mermaid 源文件](diagrams/request-flow.mmd)

<details>
<summary>查看 Mermaid 源码</summary>

```mermaid
sequenceDiagram
    participant U as 上游服务
    participant A as 接入 API
    participant P as PostgreSQL
    participant W as 对应模式 worker
    participant Q as 查询服务
    alt 整段处理（录制后推荐）
        U->>A: 一次请求上传完整录制
        A->>P: 校验并持久保存输入和任务
        A-->>U: 202 已接受，等待计算
        W->>P: 领取任务、租约及 fencing token
        P-->>W: 完整输入
        W->>W: E7 整段计算
        W->>P: 事务提交完整结果和终态
    else 分块处理（持续输出）
        U->>A: 创建任务，再分批上传
        A->>P: 持久保存、检查连续接收前沿
        W->>P: 领取任务和已有检查点
        W->>W: FC1 有限上下文计算
        W->>P: 事务提交核心结果、进度和检查点
        U->>A: seal 确认记录结束
        A->>P: 保存最终位置
        W->>P: 处理尾段并提交终态
    end
    U->>Q: 查询任务、时间线和统计
    Q->>P: 只读已提交结果
    P-->>Q: 结果及有效覆盖范围
    Q-->>U: 分类时长；可选实验步数
```

</details>

上传包长不决定算法上下文。分块模式正常累计 90 秒后发布首个 60 秒核心，后续每 60 秒推进；整段模式（录制结束后分析推荐）使用整条记录，上游可在录制结束时提交，不强制等自然日末。同一研究参考中 E7 整体分类准确性更高，但不保证每类时长都改善，见[指标比较](https://github.com/Kscii/imu-inference/blob/main/docs/model.md#分类和时长指标)。时区和日历统计按接口约定处理，缺失时间不会补成静止或零步。

## 恢复、过载与升级

![recovery-upgrade 架构图](diagrams/recovery-upgrade.svg)

[打开原尺寸图](diagrams/recovery-upgrade.svg) · [Mermaid 源文件](diagrams/recovery-upgrade.mmd)

<details>
<summary>查看 Mermaid 源码</summary>

```mermaid
flowchart TB
    subgraph REC["计算进程退出"]
        direction TB
        X["worker 异常退出"] --> L["租约超时，任务可重新领取"]
        L --> F["新 worker 获得新的 fencing token"]
        F --> C{"任务模式"}
        C -->|"incremental"| S["恢复已提交检查点<br/>继续未提交范围"]
        C -->|"whole_record"| R["读取持久完整输入<br/>重新计算整段"]
        S --> T["事务提交<br/>旧 token 不能覆盖新结果"]
        R --> T
    end
    subgraph CAP["容量管理"]
        direction TB
        O["待处理字节／任务数达限额"] --> E["返回可重试错误<br/>上游保留数据并退避重试"]
    end
    subgraph UPG["维护窗升级"]
        direction TB
        D["暂停新增<br/>完成已有上传和 seal，排空任务"]
        D --> B["备份数据库／旧配置<br/>保留旧镜像"]
        B --> N["校验新版本来源和兼容性<br/>迁移、启动和健康检查"]
        N --> H["业务调用方保持暂停<br/>允许样例任务并验收"]
        H --> A["验收通过后恢复业务接入"]
    end
```

</details>

不同模式有独立计算池和容量上限；不会因 whole_record 占用计算而改用分块模型。终态清理服务保存的输入，重算依赖上游重传；不宣称底层 WAL 或备份已经物理擦除。`restart` 处理进程退出，持续 unhealthy 仍需部署方监控。

本补丁的模型参数和计算不变，但库版本和代码摘要会改变任务指纹，不能接着恢复旧库检查点。单机内的持久队列可吸收短时突发，队列限额会拒绝超量请求，但不提供跨机高可用。容量、监控和备份操作见[运维说明](operations.md)。
