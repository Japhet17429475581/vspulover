import asyncio
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from conftest import ADMIN, AUTHOR
from sqlalchemy import text

pytestmark = pytest.mark.postgres


async def test_upgrade_from_initial_schema_preserves_existing_approved_data(service, postgres_url):
    submission, _ = await service.submit(AUTHOR, "люблю тебя", "тебя", uuid4())
    await service.review(ADMIN, submission.id, "approved")
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "downgrade", "0001"],
            env=dict(os.environ, DATABASE_URL=postgres_url),
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0
    finally:
        # Restore head even if the assertion fails; only the guarded *_test DB is used.
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env=dict(os.environ, DATABASE_URL=postgres_url),
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0
    restored = (await service.mine(AUTHOR))[0]
    assert restored.id == submission.id
    assert restored.status == "approved" and restored.normalized_text == "тебя"
    assert restored.reviewed_by == ADMIN


async def test_migrations_preserve_unrelated_tables_and_match_models(service, postgres_url):
    engine = service.sessions.kw["bind"]
    async with engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE IF NOT EXISTS public.love_bot_test_sentinel (value integer)")
        )
        await connection.execute(text("DELETE FROM public.love_bot_test_sentinel"))
        await connection.execute(text("INSERT INTO public.love_bot_test_sentinel VALUES (42)"))
    try:
        for command in (["upgrade", "head"], ["check"]):
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "alembic", *command],
                env=dict(os.environ, DATABASE_URL=postgres_url),
                capture_output=True,
                check=False,
            )
            assert result.returncode == 0
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT value FROM public.love_bot_test_sentinel"))
                == 42
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE public.love_bot_test_sentinel"))
