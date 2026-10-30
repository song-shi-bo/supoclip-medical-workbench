import pytest

from fastapi.routing import APIRoute

from src.database import get_db


@pytest.mark.asyncio
async def test_health_endpoints_report_healthy(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}

    db_response = await client.get("/health/db")
    assert db_response.status_code == 200
    assert db_response.json()["status"] == "healthy"

    redis_response = await client.get("/health/redis")
    assert redis_response.status_code == 200
    assert redis_response.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_database_health_returns_503_when_the_database_is_unavailable(
    app, client
):
    class UnavailableDatabaseSession:
        async def execute(self, _statement):
            raise RuntimeError("database unavailable")

    async def unavailable_database():
        yield UnavailableDatabaseSession()

    app.dependency_overrides[get_db] = unavailable_database
    try:
        response = await client.get("/health/db")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    assert response.json() == {
        "status": "unhealthy",
        "database": "disconnected",
    }


@pytest.mark.parametrize(
    "path",
    [
        "/fonts/{font_name}",
    ],
)
def test_database_backed_file_routes_release_sessions_before_streaming(app, path):
    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == path
    )
    database_dependency = next(
        dependency
        for dependency in route.dependant.dependencies
        if dependency.call is get_db
    )

    assert database_dependency.scope == "function"


@pytest.mark.asyncio
async def test_legacy_public_clips_mount_is_not_available(client):
    response = await client.get("/clips/seeded.mp4")

    assert response.status_code == 404
