import os

os.environ.setdefault("JWT_SECRET", "testing-secret-which-is-longer-than-32-characters")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, db_session
from app.main import app
from app.models import User
from app.security import hasher


@pytest.fixture
def client():
    test_url = os.environ.get("TEST_DATABASE_URL")
    if test_url:
        from sqlalchemy.engine import make_url

        if make_url(test_url).database != "training_test":
            raise RuntimeError("TEST_DATABASE_URL must use the disposable training_test database")
        engine = create_engine(test_url)
        Base.metadata.drop_all(engine)
    else:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def fk(connection, _):
        if engine.dialect.name == "sqlite":
            connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as db:
        db.add(User(username="test", name="Тренер", password_hash=hasher.hash("test-password-123")))

    def override():
        with sessions() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    app.dependency_overrides[db_session] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    if test_url:
        Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def club(client):
    token = client.post(
        "/api/v1/auth/login", json={"username": "test", "password": "test-password-123"}
    ).json()
    client.headers["Authorization"] = "Bearer " + token["access_token"]

    def post(path, data):
        response = client.post("/api/v1" + path, json=data)
        assert response.status_code in (200, 201), response.text
        return response.json()

    branch = post("/branches", {"name": "Центр"})
    coach = post("/coaches", {"name": "Иван", "salary_bps": 4000})
    group = post("/groups", {"name": "7–10", "branch_id": branch["id"], "coach_id": coach["id"]})
    athlete = post("/athletes", {"name": "Петров Пётр"})
    enrollment = post(
        "/enrollments", {"athlete_id": athlete["id"], "group_id": group["id"], "starts_on": "2026-01-01"}
    )
    single = post("/tariffs", {"name": "Разовая", "lessons": 1, "amount_kopecks": 50000})
    tariff = post("/tariffs", {"name": "12 занятий", "lessons": 12, "amount_kopecks": 550000})
    return {
        "client": client,
        "post": post,
        "branch": branch,
        "coach": coach,
        "group": group,
        "athlete": athlete,
        "enrollment": enrollment,
        "single": single,
        "tariff": tariff,
        "tokens": token,
    }
