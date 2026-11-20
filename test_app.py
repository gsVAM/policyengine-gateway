"""pytest -q. Importing app loads PolicyEngine and runs the warm-up (15-30 s)."""

import copy
import json
import math
import os
import subprocess
import sys
import threading
import time
from importlib.metadata import version

import pytest

os.environ["API_KEY"] = "test-key"
import app  # noqa: E402
from policyengine_core.reforms import Reform  # noqa: E402
from policyengine_us import Simulation  # noqa: E402

AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture
def client():
    return app.app.test_client()


def household(**changes):
    """The $50,000-wage 2026 single filer in CA that the app warms up with."""
    return {**copy.deepcopy(app.WARM_UP), "id": "single", **changes}


def couple():
    people = {"a": {"age": {"2026": 45}, "employment_income": {"2026": 120000},
                    "long_term_capital_gains": {"2026": 30000}, "is_tax_unit_head": {"2026": None}},
              "b": {"age": {"2026": 43}, "employment_income": {"2026": 40000}},
              "kid": {"age": {"2026": 8}}}
    everyone = {"members": list(people)}
    return household(id="couple", filing_status="JOINT", situation={
        "people": people,
        "tax_units": {"tu": {**everyone, "income_tax": {"2026": None}, "filing_status": {"2026": None}}},
        "families": {"f": everyone},
        "spm_units": {"s": everyone},
        "marital_units": {"m": {"members": ["a", "b"]}, "k": {"members": ["kid"]}},
        "households": {"h": {**everyone, "state_name": {"2026": "NY"}}},
    })


def post(client, households, headers=AUTH):
    return client.post("/calculate", json={"households": households}, headers=headers)


def income_tax(result):
    return result["situation"]["tax_units"]["tu"]["income_tax"]["2026"]


def test_healthcheck_is_open(client):
    assert client.get("/healthcheck").status_code == 200


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "test-key"}])
def test_missing_or_wrong_key_is_401(client, headers):
    assert post(client, [household()], headers).status_code == 401
    assert client.get("/anything", headers=headers).status_code == 401


def test_refuses_to_start_without_api_key():
    env = {k: v for k, v in os.environ.items() if k != "API_KEY"}
    run = subprocess.run([sys.executable, "-c", "import app"], env=env, capture_output=True,
                         text=True, cwd=os.path.dirname(os.path.abspath(__file__)), timeout=120)
    assert run.returncode != 0 and "API_KEY must be set" in run.stderr


def test_busy_process_answers_503_and_closes(client):
    with app._admission:  # as if another request were simulating
        response = post(client, [household()])
    assert response.status_code == 503
    assert response.get_json() == {"error": "busy"}
    assert response.headers["Connection"] == "close"


def test_watchdog_exits_only_past_its_limit(monkeypatch):
    exits = []
    monkeypatch.setattr(app, "_sim_started", None)
    dog = threading.Thread(target=app.watchdog, args=(0.1, exits.append, 0.01), daemon=True)
    dog.start()
    time.sleep(0.3)
    assert exits == [] and dog.is_alive()  # idle process: never fires
    monkeypatch.setattr(app, "_sim_started", time.monotonic())
    dog.join(2)
    assert exits == [1]


def test_single_filer_2026(client):
    response = post(client, [household()])
    assert response.status_code == 200
    body = response.get_json()
    assert body["identity"]["policyengine_us"] == version("policyengine-us")
    assert body["identity"]["policyengine_core"] == version("policyengine-core")
    assert {"wrapper_commit", "image_build"} <= body["identity"].keys()
    [result] = body["results"]
    assert (result["id"], result["tax_year"], result["filing_status"]) == ("single", 2026, "SINGLE")
    assert math.isfinite(income_tax(result)) and income_tax(result) > 0
    ordinary = result["ordinary_brackets"]
    thresholds = [b["threshold"] for b in ordinary]
    assert ordinary and thresholds[0] == 0 and thresholds == sorted(thresholds)
    assert all(0 <= b["rate"] < 1 for b in ordinary)
    assert result["capital_gains_brackets"][0] == {"threshold": 0, "rate": 0}
    assert len(result["capital_gains_brackets"]) > 1


def test_batch_matches_single_households(client):
    alone = [post(client, [h]).get_json()["results"][0] for h in (household(), couple())]
    together = post(client, [household(), couple()]).get_json()["results"]
    assert together == alone
    assert together[1]["situation"]["tax_units"]["tu"]["filing_status"]["2026"] == "JOINT"
    assert together[1]["situation"]["people"]["a"]["is_tax_unit_head"]["2026"] is True


def test_override_changes_schedule_and_matches_policyengine(client):
    overrides = {"gov.irs.income.bracket.thresholds.1.SINGLE": {"2026-01-01.2026-12-31": 20000}}
    base, reformed = post(client, [household(), household(id="reformed", overrides=overrides)]).get_json()["results"]
    assert reformed["id"] == "reformed"
    assert reformed["ordinary_brackets"][1]["threshold"] == 20000 != base["ordinary_brackets"][1]["threshold"]
    assert income_tax(reformed) < income_tax(base)
    # The cached reformed system gives what PolicyEngine's own reform path gives.
    sim = Simulation(situation=household()["situation"], reform=Reform.from_dict(overrides, country_id="us"))
    assert income_tax(reformed) == float(str(sim.calculate("income_tax", "2026")[0]))


def body(**changes):
    return json.dumps({"households": [household(**changes)]})


def without_families():
    h = household()
    del h["situation"]["families"]
    return json.dumps({"households": [h]})


@pytest.mark.parametrize("raw", [
    "not json",
    json.dumps({"households": []}),
    json.dumps({"households": [household()] * (app.MAX_HOUSEHOLDS + 1)}),
    body().replace("50000", "NaN"),
    body().replace("50000", "1e999"),
    body(tax_year="2026"),
    body(tax_year=1990),
    body(filing_status="MARRIED"),
    body(extra=1),
    without_families(),
    body().replace('"income_tax"', '"no_such_variable"'),
    body().replace('"members": ["you"]', '"members": "you"', 1),
    body(overrides={"gov.no.such.parameter": {"2026-01-01": 1}}),
])
def test_malformed_body_is_400(client, raw):
    response = client.post("/calculate", data=raw, content_type="application/json", headers=AUTH)
    assert response.status_code == 400
    assert response.get_json()["error"] and response.get_json()["identity"]
