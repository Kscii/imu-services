# 源码开发与构建

此页面向修改服务代码的维护者。普通部署使用[在线安装](../README.md)，不需要克隆源码或自行构建镜像。

以下命令在完整的[服务源码仓库](https://github.com/Kscii/imu-services)中执行；在线部署包不包含开发环境或 vendor wheel。开发需要 Python 3.12、uv 和 Docker，数据库测试另需隔离 PostgreSQL。

```bash
uv sync --frozen --extra worker
uv run --frozen --extra worker ruff check .
uv run --frozen --extra worker pytest -q
```

测试前通过进程环境提供隔离数据库的 `IMU_TEST_DATABASE_URL`。缺少连接时数据库测试会跳过，不能记为完整验收；不要用生产数据库运行测试。

镜像构建示例：

```bash
docker build --target runtime -t localhost/imu-runtime:1.1.1 .
docker build --target query -t localhost/imu-query:1.1.1 .
```

`vendor/wheels/` 和锁文件固定推理库及依赖。模型更新形成新的 wheel、摘要、回归记录和镜像，不原地替换已有容器里的权重。

`scripts/container_acceptance.py` 验证真实模型和容器恢复，并清理自己的测试项目。正式构建须冻结源码、记录镜像来源并保留验证材料；开发构建成功不等于发布或部署成功。源码、镜像与文档修订的对应关系见[交付记录](handoff.md)。
