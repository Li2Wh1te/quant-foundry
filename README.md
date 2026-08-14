# Quant Foundry

基于 FastAPI 的 API 服务。

## 环境要求

- Python 3.12.2
- uv

项目通过 `.python-version` 固定 Python 版本，通过 `pyproject.toml` 声明直接依赖，并通过 `uv.lock` 锁定完整依赖树。未来构建容器镜像时，应使用对应的 `python:3.12.2-slim` 基础镜像，并执行 `uv sync --locked` 安装锁定依赖。

## 本地运行

```bash
uv sync
uv run python -m app
```

`uv sync` 会自动创建并管理项目的 `.venv`。服务默认监听 `127.0.0.1:8000`。应用配置项及说明见 `.env.example`，本地配置写入 `.env`。

## PostgreSQL

项目使用 SQLAlchemy 2.0、psycopg 3 和 Alembic。先创建数据库，再在 `.env` 中配置 `QF_DATABASE_URL`：

```env
QF_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/quant_foundry
```

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
