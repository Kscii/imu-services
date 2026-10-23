# 接口指南

服务 1.1.1，HTTP v2。接入默认 `http://127.0.0.1:8100`，查询默认 `http://127.0.0.1:8101`。请求头 `Authorization: Bearer <token>`：接入用 IMU_WRITE_TOKEN，查询分析用 IMU_READ_TOKEN。这是内部服务鉴权，不承担终端用户或租户隔离。

运行后的 `/openapi.json` 提供接口结构；字段严格校验。完整示例见 [examples](../examples/)。

## 标准输入

固定胸前规范佩戴；25 Hz，每个位置 40 ms。加速度 `N×3`、m/s²、保留重力，角速度 `N×3`、rad/s，两者等长。不能用零角速度冒充实际测量。上游负责解包、对时、坐标、单位与重采样。

start_time 必须带 UTC 偏移；start_sample 从记录起点的 0 开始，是采样位置而非包序号。区间为 `[start,end)`。缺失位置保留，用有限占位值及 valid=false；排除范围用 excluded=true，省略时默认分别 true / false。不得传 NaN 或删除位置压缩时间。

## 分块：创建 → 上传 → seal → 查询

`POST /v2/runs`：

```json
{"request_key":"record-001-fc1","recording_id":"record-001","device_id":"device-001","start_time":"2026-10-22T10:00:00+08:00","mode":"incremental","steps_enabled":true}
```

取得 run_id 后调用 `POST /v2/runs/{run_id}/batches`：

```json
{"batch_id":"batch-0","start_sample":0,"sample_rate_hz":25,"acceleration_mps2":[[0.1,0.2,9.8]],"angular_velocity_rads":[[0.01,0.02,0.03]],"valid":[true],"excluded":[false]}
```

每批 1～15,000 个位置。允许乱序抵达，计算只推进连续接收前沿。永久缺失可调用 `POST /v2/runs/{run_id}/gaps`，传 batch_id/start_sample/sample_count/excluded；暂未收到不等于永久缺失。

确认记录结束后调用 `POST /v2/runs/{run_id}/seal`，例如 `{"end_sample":3750}`，结束位置不包含在数据内。

分类每次发布 60 秒核心，左右各至多 30 秒上下文；正常首段需累计收到 90 秒，之后每 60 秒推进，seal 后完成尾段。上传包大小独立；90 秒不含上传、排队和计算，不是 SLA。

## 实验整段上传

`POST /v2/runs/whole-record`，Content-Type 为 application/x-ndjson，可带 Content-Encoding: gzip。首行为元信息，其后每行 `[ax,ay,az,gx,gy,gz,valid,excluded]`：

```text
{"format":"imu.recording.ndjson.v2","mode":"whole_record","request_key":"record-001-e7","recording_id":"record-001","device_id":"device-001","start_time":"2026-10-22T10:00:00+08:00","sample_rate_hz":25,"sample_count":2,"steps_enabled":true}
[0.1,0.2,9.8,0.01,0.02,0.03,true,false]
[0,0,0,0,0,0,false,false]
```

最多 2,250,000 个位置，即含缺失的 25 小时。默认传输 256 MiB、解压后 512 MiB、单行 4096 字节、上传 300 秒，每个 API 进程同时接收 2 个整段请求。不支持拼接 gzip member。

202 表示完整输入已持久接受，不表示推理完成。成功后结果整体发布。整段可以是完整短录制；E7 利用未来信息，不能将多个 90 秒独立结果拼接冒充整段结果。

## 查询与重试

| 查询接口 | 用途 |
|---|---|
| GET /v2/runs/{id} | 状态、模式、指纹与当前统计 |
| GET /v2/runs/{id}/results?after=0&limit=100 | 不可变事件分页；使用 next_after 续读，limit 最大 500 |
| GET /v2/recordings/{recording_id}/versions | 同一录制的处理版本 |
| GET /v2/runs/{id}/analysis | 记录级统计 |
| GET /v2/runs/{id}/calendar | 已完成单记录日历；参数 timezone/date_from/date_to |
| POST /v2/analysis/activity | 已完成多记录的逐人日历统计 |
| GET /v2/operations、GET /metrics | 队列、worker 与 HTTP 指标 |
| GET /health/live、GET /health/ready | 存活和数据库 schema 就绪 |

状态为 waiting/queued/running/completed/failed；waiting 可能是在等上下文。available_end 为接收前沿，processed_samples 为检查点前沿，summary.through_sample 为分类前沿，summary.steps.through_sample 为计步前沿，不能混用。实际活动覆盖以 activity_core 事件为准。

重试保持相同 request_key、batch_id 与内容；同身份同内容不重复计算，同身份不同内容返回 409。丢失响应后先原样重试。429/503 按 Retry-After 退避，上游保留未确认数据。

整段最终失败已清理输入，须新 request_key 完整重传。`POST /v2/runs/{id}/retry` 只用于仍可恢复的失败分块。事务接受不替代异地备份，上游需保留原始副本。

错误：401 凭据无效；404 不存在；409 身份/版本/状态/范围冲突；408 上传超时；413 超限；415 类型/编码错误；422 输入错误；429/503 暂时过载。

## 小时、日、周分析

```json
{"participant_id":"person-001","run_ids":["00000000-0000-0000-0000-000000000001"],"timezone":"Asia/Shanghai","date_from":"2026-10-22","date_to":"2026-10-28"}
```

调用方负责人员归属与版本选择。最多 100 个 run、31 个含首尾日期、各 100,000 个活动段和步段；空列表返回无观测。重复 run 去重；同一录制多个版本、不同配置指纹、重叠或未完成记录返回冲突。单记录与跨记录日历共享每进程 1 个计算名额，忙时返回 429，普通状态查询仍可进行。

calendar.hourly/daily/weekly 包含活动 duration_s、fraction_of_observed 和覆盖。invalid_s/excluded_s/unobserved_s 分别为无效、排除、未观测时间。周统计只含请求日期；夏令时按实际 23/25 小时。观测两小时中行进一小时只代表观测内 50%，不能外推全天。

日历总计及小时、日、周均新增：

```json
{"steps":{"estimated_step_count":12,"analyzed_s":30.0,"unresolved_valid_s":10.0,"coverage_of_processed_valid":0.75,"experimental":true}}
```

步事件按时间落入 `[start,end)` 桶，与分类标签独立。已分析且零步返回 0，无分析结果返回 null。覆盖率分母为已处理且有效的计步时间，不是全天，也不能证明规范佩戴。recording_steps 保持整条记录口径，不按日期裁剪。

analysis_version 为 activity-calendar-v2；sources 与 source_manifest_sha256 追溯来源。1.1.0 保留既有字段含义，仅扩展日历步数。四类标签为 lying/sitting/standing/walking_running，具体分组见模型说明。
