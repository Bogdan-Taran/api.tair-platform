"""Requires an isolated MySQL database named training_test."""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_api import card, pay, training


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="Requires MySQL test container")
def test_parallel_payment_and_attendance_are_not_duplicated(club):
    c = club
    with ThreadPoolExecutor(max_workers=2) as pool:
        payments = list(pool.map(lambda _: pay(c), range(2)))
    assert payments[0]["id"] == payments[1]["id"]
    t = training(c, "2026-10-03")

    def attend(_):
        return c["client"].put(
            f"/api/v1/trainings/{t['id']}/attendance",
            json={"items": [{"athlete_id": c["athlete"]["id"], "status": "present"}]},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attend, range(2)))
    assert [r.status_code for r in results] == [200, 200]
    assert card(c)["balance"]["remaining_lessons"] == 11
    assert card(c)["balance"]["attended"] == 1
