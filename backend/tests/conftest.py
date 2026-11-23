import os
import sys
import time
import hmac
import hashlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import Config, set_config_override
from src.database import configure_database, init_db, reset_database_state
from src.main_refactored import create_app


class _FakeRedisPool:
    async def ping(self):
        return True


class FakeQueueAdapter:
    enqueued_jobs = []

    @classmethod
    async def get_pool(cls):
        return _FakeRedisPool()

    @classmethod
    async def close_pool(cls):
        return None

    @classmethod
    async def enqueue_processing_job(cls, function_name: str, processing_mode: str, *args):
        cls.enqueued_jobs.append(
          {
            "function_name": function_name,
            "processing_mode": processing_mode,
            "args": args,
          }
        )
        return "job-test-1"


@pytest.fixture(scope="session")
def test_database_url():
    return os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")


@pytest.fixture(scope="session")
async def initialized_database(test_database_url):
    if not test_database_url:
        pytest.skip("DATABASE_URL or TEST_DATABASE_URL must be set for backend tests")

    engine = create_async_engine(test_database_url, poolclass=NullPool)
    configure_database(engine=engine)
    await init_db()
    yield engine
    await reset_database_state()


@pytest.fixture()
async def db_session(initialized_database):
    session_maker = async_sessionmaker(
        initialized_database,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_maker() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.close()


@pytest.fixture(autouse=True)
def _isolated_manifest_mirror(monkeypatch):
    """阶段5·点1：单测默认关闭台账 PG 镜像/修复。

    镜像按 project_id 落库且跨测试进程持久——固定 pid 的文件级测试会通过
    「磁盘缺失→修复」路径读到其它测试提交的台账（实测污染 clip_config /
    inspector / active_clip 等数十个用例）。默认双 no-op 保住旧的文件隔离语义；
    需要真实验证镜像链路的用例，接收本 fixture 的返回值（原始实现二元组）并
    在用例内 monkeypatch 恢复（见 test_studio_project_store 的两个 PG 用例）。
    """
    from src import studio_project_store as store

    saved = (store._mirror_manifest_to_pg, store._restore_manifest_from_pg)
    monkeypatch.setattr(store, "_mirror_manifest_to_pg", lambda *a, **k: None)
    monkeypatch.setattr(store, "_restore_manifest_from_pg", lambda *a, **k: None)
    yield saved


@pytest.fixture()
async def app(db_session):
    config = Config()
    config.self_host = True
    config.monetization_enabled = False
    config.backend_auth_secret = "test-backend-auth-secret"
    config.allow_unsigned_backend_auth = False
    config.redis_host = os.getenv("REDIS_HOST", "127.0.0.1")
    config.redis_port = int(os.getenv("REDIS_PORT", "6379"))

    test_app = create_app(config=config, queue_adapter=FakeQueueAdapter)
    try:
        yield test_app
    finally:
        set_config_override(None)


@pytest.fixture()
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as async_client:
        yield async_client


@pytest.fixture()
def auth_headers():
    user_id = "user-1"
    timestamp = str(int(time.time()))
    payload = f"{user_id}:{timestamp}".encode("utf-8")
    signature = hmac.new(
        b"test-backend-auth-secret", payload, hashlib.sha256
    ).hexdigest()

    return {
        "x-supoclip-user-id": user_id,
        "x-supoclip-ts": timestamp,
        "x-supoclip-signature": signature,
    }
