"""Alembic 迁移：升级后的结构与模型一致；引入迁移前由 create_all 建出的旧库能被识别并升级。"""

import asyncio

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from device_agent import models  # noqa: F401
from device_agent.database import BASELINE_REVISION, Base, alembic_config, create_engine, init_db


def _ours(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in Base.metadata.tables
    table = getattr(obj, "table", None)
    return table is None or table.name in Base.metadata.tables


def _diff(connection) -> list:
    context = MigrationContext.configure(connection, opts={"compare_type": True, "include_object": _ours})
    return compare_metadata(context, Base.metadata)


def _revision(connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def test_migrations_match_models(database_url):
    async def run():
        engine = create_engine(database_url)
        await init_db(engine)
        async with engine.connect() as conn:
            diff = await conn.run_sync(_diff)
            revision = await conn.run_sync(_revision)
        await engine.dispose()
        return diff, revision

    diff, revision = asyncio.run(run())
    assert diff == []
    assert revision == ScriptDirectory.from_config(alembic_config()).get_current_head()


def test_legacy_create_all_database_is_upgraded(database_url):
    async def run():
        engine = create_engine(database_url)
        # 模拟旧版本：基线结构 + 数据，但没有 alembic_version 表
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: command.upgrade(alembic_config(c), BASELINE_REVISION))
            await conn.execute(text("DROP TABLE alembic_version"))
            await conn.execute(
                text(
                    "INSERT INTO devices (device_id, name, device_type, description, mcp_url, "
                    "supported_data_types, hardware_info, tools, is_online, created_at, updated_at) "
                    "VALUES ('old_cam', '旧设备', 'other', '', 'http://x/mcp', '[]', '{}', '[]', "
                    ":online, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"online": True},
            )
        await init_db(engine)
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT device_id, tool_policies FROM devices"))).one()
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
            diff = await conn.run_sync(_diff)
        await engine.dispose()
        return row, tables, diff

    row, tables, diff = asyncio.run(run())
    assert row[0] == "old_cam" and row[1] in ({}, "{}")
    assert {"incidents", "approvals", "alembic_version"} <= tables
    assert diff == []
