# Quant Foundry

基于 FastAPI 的 API 服务。

## 环境要求

- 自托管部署：Docker、Docker Compose v2、make
- 本地源码运行：Python 3.12.2、uv

项目通过 `.python-version` 固定 Python 版本，通过 `pyproject.toml` 声明直接依赖，并通过 `uv.lock` 锁定完整依赖树。未来构建容器镜像时，应使用对应的 `python:3.12.2-slim` 基础镜像，并执行 `uv sync --locked` 安装锁定依赖。

## 本地运行

```bash
uv sync
uv run python -m app
```

`uv sync` 会自动创建并管理项目的 `.venv`。服务默认监听 `127.0.0.1:8000`。应用配置项及说明见 `.env.example`，本地配置写入 `.env`。

## 自托管部署

克隆仓库后，在项目根目录执行：

```bash
git clone <repository-url> quant-foundry
cd quant-foundry
make selfhost
```

首次执行 `make selfhost` 会：

1. 从 `.env.example` 创建被 Git 忽略的 `.env`；
2. 通过 Python `secrets` 生成 256-bit PostgreSQL 随机密码，并将 `.env` 权限设为 `0600`；
3. 基于当前 checkout 构建 Server 镜像；
4. 创建持久化数据卷并启动 PostgreSQL；
5. 执行全部 Alembic 迁移；
6. 启动 Server，并通过会实际查询 PostgreSQL 的 `/readyz` 等待服务就绪。

最终常驻 `postgres` 和 `server` 两个容器。Server 在容器网络中连接 `postgres:5432`，数据库密码由两个容器从同一份 `.env` 读取，不会写入镜像。再次执行会复用已有密码和数据卷。

常用管理命令：

```bash
make selfhost-status   # 查看两个容器状态
make selfhost-logs     # 跟踪 PostgreSQL 和 Server 日志
make selfhost-psql     # 进入 psql
make selfhost-migrate  # 手动执行迁移
make selfhost-down     # 停止容器但保留数据
make selfhost-reset    # 删除数据库和日志数据后重新部署
```

Server 默认从 [http://127.0.0.1:8000](http://127.0.0.1:8000) 访问，PostgreSQL 默认仅映射至 `127.0.0.1:5432`。端口、数据库名和账号可在首次部署前修改 `.env.example`，部署后配置位于 `.env`。数据库已经初始化后不要直接修改密码，否则会与 PostgreSQL 内部账号不一致；应执行 `ALTER ROLE`，或使用 `make selfhost-reset` 删除数据后重新初始化。

路由或服务通过 FastAPI 依赖获取会话，事务由业务逻辑显式提交：

```python
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db import get_db_session

DbSession = Annotated[Session, Depends(get_db_session)]
```

新增模型后，将模型模块导入 `app/models/__init__.py`，再创建并执行迁移：

```bash
uv run alembic revision --autogenerate -m "create initial tables"
uv run alembic upgrade head
```

## 测试

```bash
uv run python -m unittest discover -v
```

## 本地日志

服务使用 `structlog` 生成 JSON 日志，通过内存队列异步写入
`data/logs/app.jsonl`。日志文件每天轮转，历史文件使用 gzip 压缩；
`concurrent-log-handler` 负责多个进程并发写入和轮转时的文件锁。

日志查询接口：

```http
GET /api/admin/logs?keyword=timeout&level=WARNING&method=GET&status_class=4xx&limit=200
POST /api/admin/logs/clear
```

查询接口支持 `keyword`、`level`、`method`、`status_class`、`path`、
`start_time` 和 `end_time` 过滤，并返回筛选项计数。未传 `start_time` 时默认查询最近
24 小时，避免自动刷新反复扫描全部历史文件。`clear` 只隐藏调用时刻之前的
日志，不会截断正在并发写入的文件，物理文件仍按保留周期自动清理。
单次查询时间范围最多为 31 天，返回结果最多为 1000 条。

这些接口可能包含运行细节和敏感上下文。在服务监听非本机地址前，必须通过应用鉴权
或反向代理限制为管理员访问。
