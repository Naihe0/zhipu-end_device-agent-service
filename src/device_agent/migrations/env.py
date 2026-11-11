"""Alembic 迁移环境。

- 服务启动时由 `database.init_db()` 以共享连接的方式调用（config.attributes["connection"]）。
- 也可手动执行：`uv run alembic upgrade head` / `uv run alembic revision --autogenerate -m "..."`，
  数据库地址取自 DATABASE_URL（.env）。
只管理本服务自己的表；a2a-sdk 的 tasks / push_notification_configs 表由 SDK 自行维护。
"""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from device_agent import models  # noqa: F401  注册模型
from device_agent.database import Base

config = context.config
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in target_metadata.tables
    table = getattr(obj, "table", None)
    return table is None or table.name in target_metadata.tables


def run_with_connection(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_with_new_engine() -> None:
    from device_agent.config import get_settings

    url = config.get_main_option("sqlalchemy.url") or get_settings().database_url
    engine = create_async_engine(url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(run_with_connection)
    await engine.dispose()


if (shared := config.attributes.get("connection")) is not None:
    run_with_connection(shared)
else:
    asyncio.run(run_with_new_engine())
