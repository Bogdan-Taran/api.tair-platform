from datetime import date, timedelta


def pay(c, key="payment-0001", on="2026-10-01", tariff=None):
    return c["post"](
        "/payments",
        {
            "athlete_id": c["athlete"]["id"],
            "group_id": c["group"]["id"],
            "tariff_id": (tariff or c["tariff"])["id"],
            "paid_on": on,
            "request_key": key,
        },
    )


def training(c, day):
    return c["post"](
        "/trainings",
        {
            "group_id": c["group"]["id"],
            "coach_id": c["coach"]["id"],
            "date": str(day),
            "starts_at": "16:00:00",
            "ends_at": "17:00:00",
        },
    )


def mark(c, t, status="present"):
    r = c["client"].put(
        f"/api/v1/trainings/{t['id']}/attendance",
        json={"items": [{"athlete_id": c["athlete"]["id"], "status": status}]},
    )
    assert r.status_code == 200, r.text
    return r.json()["items"][0]


def card(c):
    return c["client"].get(f"/api/v1/athletes/{c['athlete']['id']}/card").json()


def report(c, start="2026-10-01", end="2026-11-30", **filters):
    r = c["client"].get("/api/v1/reports/finance", params={"starts_on": start, "ends_on": end, **filters})
    assert r.status_code == 200, r.text
    return r.json()


def test_auth_and_rotation(client):
    assert client.get("/api/v1/athletes").status_code in (401, 403)
    assert (
        client.post("/api/v1/auth/login", json={"username": "test", "password": "wrong"}).status_code == 401
    )
    tokens = client.post(
        "/api/v1/auth/login", json={"username": "test", "password": "test-password-123"}
    ).json()
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    assert (
        client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code
        == 401
    )
    assert (
        client.post("/api/v1/auth/logout", json={"refresh_token": r.json()["refresh_token"]}).status_code
        == 204
    )


def test_full_subscription_rounding_carryover_debt(club):
    c = club
    pay(c)
    values = []
    for i in range(13):
        day = date(2026, 10, 24) + timedelta(days=i)
        values.append(mark(c, training(c, day)))
    b = card(c)["balance"]
    assert b == {
        "remaining_lessons": 0,
        "debt_lessons": 1,
        "debt_kopecks": 50000,
        "attended": 13,
        "earned_kopecks": 550000,
    }
    october = report(c, end="2026-10-31")
    november = report(c, start="2026-11-01")
    assert october["attended"] == 8
    assert october["earned_kopecks"] == 366666
    assert november["earned_kopecks"] == 183334
    assert october["received_kopecks"] == 550000
    assert november["received_kopecks"] == 0
    assert report(c)["salary_kopecks"] == 220000
    assert sum(v["value_kopecks"] for v in values) == 550000
    sheet = (
        c["client"]
        .get("/api/v1/reports/timesheet", params={"group_id": c["group"]["id"], "year": 2026, "month": 10})
        .json()
    )
    assert sheet["items"][0]["balance"]["remaining_lessons"] == 4


def test_idempotency_and_tariff_snapshot(club):
    c = club
    payment = pay(c)
    assert pay(c)["id"] == payment["id"]
    assert len(card(c)["payments"]) == 1
    r = c["client"].put(
        f"/api/v1/tariffs/{c['tariff']['id']}",
        params={"reason": "Новая цена"},
        json={"name": "12 занятий", "lessons": 12, "amount_kopecks": 600000},
    )
    assert r.status_code == 200
    assert pay(c)["amount_kopecks"] == 550000
    body = {
        "athlete_id": c["athlete"]["id"],
        "group_id": c["group"]["id"],
        "tariff_id": c["tariff"]["id"],
        "paid_on": "2026-10-02",
        "request_key": "payment-0001",
    }
    assert c["client"].post("/api/v1/payments", json=body).status_code == 409


def test_corrections_fifo_and_audit(club):
    c = club
    p = pay(c, tariff=c["single"])
    pay(c, "payment-0002")
    t1, t2 = training(c, "2026-10-03"), training(c, "2026-10-04")
    assert mark(c, t1)["value_kopecks"] == 50000
    assert mark(c, t2)["value_kopecks"] == 45833
    mark(c, t1, "absent")
    assert card(c)["balance"]["earned_kopecks"] == 50000
    assert mark(c, t2)["value_kopecks"] == 50000  # PUT is idempotent
    r = c["client"].patch(
        f"/api/v1/payments/{p['id']}",
        json={"amount_kopecks": 50000, "paid_on": "2026-10-01", "voided": True, "reason": "Ошибочная оплата"},
    )
    assert r.status_code == 200
    assert card(c)["balance"]["earned_kopecks"] == 45833
    logs = c["client"].get("/api/v1/audit", params={"entity_id": p["id"]}).json()["items"]
    assert len(logs) == 2 and logs[0]["before"]


def test_debt_before_payment_is_not_retroactively_consumed(club):
    c = club
    mark(c, training(c, "2026-10-01"))
    pay(c, on="2026-10-02")
    mark(c, training(c, "2026-10-03"))
    b = card(c)["balance"]
    assert b["remaining_lessons"] == 11 and b["debt_lessons"] == 1
    notifications = c["client"].get("/api/v1/notifications").json()
    assert notifications["items"][0]["kind"] == "debt"


def test_cancel_restore_and_coach_history(club):
    c = club
    pay(c)
    t = training(c, "2026-10-03")
    mark(c, t)
    coach = c["coach"]
    assert (
        c["client"]
        .put(
            f"/api/v1/coaches/{coach['id']}",
            params={"reason": "Изменение ставки"},
            json={"name": coach["name"], "salary_bps": 9000},
        )
        .status_code
        == 200
    )
    body = {k: t[k] for k in ["group_id", "coach_id", "date", "starts_at", "ends_at", "cancelled"]}
    body.update(cancelled=True, reason="Отмена занятия")
    assert c["client"].put(f"/api/v1/trainings/{t['id']}", json=body).status_code == 200
    assert card(c)["balance"]["remaining_lessons"] == 12
    assert report(c)["earned_kopecks"] == 0
    body["cancelled"] = False
    assert c["client"].put(f"/api/v1/trainings/{t['id']}", json=body).status_code == 200
    assert report(c)["salary_kopecks"] == 18333
    assert report(c, coach_id=coach["id"])["received_kopecks"] is None


def test_schedule_generation_and_validation(club):
    c = club
    c["post"](
        "/schedules",
        {
            "group_id": c["group"]["id"],
            "weekday": 0,
            "starts_at": "16:00",
            "ends_at": "17:00",
            "starts_on": "2026-10-01",
        },
    )
    data = {"group_id": c["group"]["id"], "starts_on": "2026-10-01", "ends_on": "2026-10-31"}
    assert c["post"]("/trainings/generate", data)["created"] == 4
    assert c["post"]("/trainings/generate", data)["created"] == 0
    assert (
        c["client"]
        .get("/api/v1/reports/finance", params={"starts_on": "2026-11-01", "ends_on": "2026-10-01"})
        .status_code
        == 422
    )
    assert (
        c["client"].post("/api/v1/branches", json={"name": "Ошибка", "timezone": "Bad/Zone"}).status_code
        == 422
    )


def test_batch_atomicity_and_enrollment(club):
    c = club
    t = training(c, "2026-10-03")
    outsider = c["post"]("/athletes", {"name": "Без группы"})
    r = c["client"].put(
        f"/api/v1/trainings/{t['id']}/attendance",
        json={
            "items": [
                {"athlete_id": c["athlete"]["id"], "status": "present"},
                {"athlete_id": outsider["id"], "status": "present"},
            ]
        },
    )
    assert r.status_code == 409
    assert card(c)["balance"]["attended"] == 0
    mark(c, t)
    enrollment = c["enrollment"]
    r = c["client"].put(
        f"/api/v1/enrollments/{enrollment['id']}",
        params={"reason": "Перевод в другую группу"},
        json={
            "athlete_id": c["athlete"]["id"],
            "group_id": c["group"]["id"],
            "starts_on": "2026-01-01",
            "ends_on": "2026-10-02",
        },
    )
    assert r.status_code == 409


def test_filters_archive_and_missing_salary(club):
    c = club
    pay(c)
    mark(c, training(c, "2026-10-03"))
    assert report(c, branch_id="missing")["earned_kopecks"] == 0
    athlete = c["athlete"]
    assert (
        c["client"]
        .put(
            f"/api/v1/athletes/{athlete['id']}",
            params={"reason": "Ушёл из секции"},
            json={"name": athlete["name"], "archived": True},
        )
        .status_code
        == 200
    )
    assert c["client"].get("/api/v1/athletes").json()["total"] == 0
    assert c["client"].get("/api/v1/athletes", params={"archived": True}).json()["total"] == 1
    assert card(c)["balance"]["attended"] == 1


def test_openapi_contract(client):
    spec = client.get("/openapi.json").json()
    assert spec["paths"]["/api/v1/payments"]["post"]["requestBody"]
    assert spec["components"]["schemas"]["AttendanceBatch"]
    assert spec["paths"]["/api/v1/athletes"]["post"]["requestBody"]


def test_no_salary_rate_is_not_zero_salary(club):
    c = club
    pay(c)
    coach = c["coach"]
    c["client"].put(
        f"/api/v1/coaches/{coach['id']}",
        params={"reason": "Ставка не определена"},
        json={"name": coach["name"], "salary_bps": None},
    )
    mark(c, training(c, "2026-10-03"))
    assert report(c)["salary_kopecks"] is None
    assert report(c)["earned_kopecks"] > 0


def test_correction_reprices_and_future_protection(club):
    c = club
    p = pay(c)
    t = training(c, "2026-10-03")
    mark(c, t)
    r = c["client"].patch(
        f"/api/v1/payments/{p['id']}",
        json={"amount_kopecks": 600000, "paid_on": "2026-10-01", "reason": "Исправлена сумма"},
    )
    assert r.status_code == 200
    assert card(c)["balance"]["earned_kopecks"] == 50000
    future = training(c, "2099-10-03")
    r = c["client"].put(
        f"/api/v1/trainings/{future['id']}/attendance",
        json={"items": [{"athlete_id": c["athlete"]["id"], "status": "present"}]},
    )
    assert r.status_code == 409


def test_active_tariff_unique(club):
    c = club
    r = c["client"].post(
        "/api/v1/tariffs", json={"name": "Другая разовая", "lessons": 1, "amount_kopecks": 60000}
    )
    assert r.status_code == 409
    assert c["client"].get("/api/v1/tariffs").json()["total"] == 2


def test_group_balances_and_notification_scope(club):
    c = club
    pay(c)
    group2 = c["post"](
        "/groups", {"name": "Другая группа", "branch_id": c["branch"]["id"], "coach_id": c["coach"]["id"]}
    )
    c["post"](
        "/enrollments",
        {"athlete_id": c["athlete"]["id"], "group_id": group2["id"], "starts_on": "2026-01-01"},
    )
    t = c["post"](
        "/trainings",
        {
            "group_id": group2["id"],
            "coach_id": c["coach"]["id"],
            "date": "2026-10-03",
            "starts_at": "16:00",
            "ends_at": "17:00",
        },
    )
    assert mark(c, t)["debt_kopecks"] == 50000
    assert card(c)["balance"]["remaining_lessons"] == 12
    notes = c["client"].get("/api/v1/notifications").json()["items"]
    assert len(notes) == 1 and notes[0]["group_id"] == group2["id"]


def test_migrations_round_trip(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from app.config import settings
    from app.db import Base

    original = settings().database_url
    settings().database_url = "sqlite:///" + str(tmp_path / "migrations.db")
    cfg = Config("alembic.ini")
    try:
        command.upgrade(cfg, "head")
        engine = create_engine(settings().database_url)
        assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
        command.check(cfg)
        command.downgrade(cfg, "base")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        command.upgrade(cfg, "head")
        engine.dispose()
    finally:
        settings().database_url = original
