"""PolicyEngine wrapper: a small HTTP API over the unmodified policyengine-us and
policyengine-core packages. AGPL-3.0, see LICENSE.

Stateless. Never log a request or response body: they hold a household's tax facts.
"""

import os
import sys

API_KEY = os.environ.get("API_KEY") or sys.exit("API_KEY must be set")  # fail closed

import copy
import gc
import hmac
import json
import math
import threading
import time
from importlib.metadata import version

from flask import Flask, jsonify, request
from policyengine_core.enums import EnumArray
from policyengine_core.reforms import Reform
from policyengine_us import CountryTaxBenefitSystem, Simulation
from policyengine_us.system import system as BASELINE

MAX_HOUSEHOLDS = 100
WATCHDOG_SECONDS = float(os.environ.get("WATCHDOG_SECONDS", "30"))
IDENTITY = {
    "policyengine_us": version("policyengine-us"),
    "policyengine_core": version("policyengine-core"),
    "wrapper_commit": os.environ.get("WRAPPER_COMMIT", "unknown"),
    "image_build": os.environ.get("IMAGE_BUILD", "unknown"),
}
ENTITIES = tuple(entity.plural for entity in BASELINE.entities)
FILING_STATUSES = tuple(
    BASELINE.parameters.gov.irs.income.bracket.thresholds.children["1"].children
)
FIELDS = {"id", "tax_year", "filing_status", "situation"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
_admission = threading.Lock()  # one simulation per process
_sim_started = None  # monotonic start of the running simulation; read by the watchdog
_warm = False


@app.before_request
def require_key():
    if request.path == "/healthcheck":
        return None
    sent = request.headers.get("Authorization", "").encode()
    if not hmac.compare_digest(sent, f"Bearer {API_KEY}".encode()):
        return jsonify(error="unauthorized"), 401


@app.get("/healthcheck")
def healthcheck():
    return ("ok", 200) if _warm else ("warming up", 503)


@app.post("/calculate")
def calculate():
    global _sim_started
    if not _admission.acquire(blocking=False):
        return jsonify(error="busy"), 503, {"Connection": "close"}
    try:
        _sim_started = time.monotonic()
        raw = request.get_data()  # outside the handlers below, so an oversized body stays a 413
        try:
            results = run_batch(parse(raw))
        except ValueError as error:
            return jsonify(error=str(error), identity=IDENTITY), 400
        except Exception as error:  # log the type only: a message can carry household values
            app.logger.error("calculate failed: %s", type(error).__name__)
            return jsonify(error="calculation failed", identity=IDENTITY), 500
        return jsonify(results=results, identity=IDENTITY)
    finally:
        _sim_started = None
        _admission.release()


def watchdog(limit=WATCHDOG_SECONDS, exit=os._exit, interval=1.0):
    """Exit the process once one simulation has run past `limit` seconds;
    gunicorn's master then forks a warm replacement from the preloaded state."""
    while _sim_started is None or time.monotonic() - _sim_started <= limit:
        time.sleep(interval)
    exit(1)  # not 3 or 4: gunicorn halts the whole server on those


def _finite(text):
    if not math.isfinite(float(text)):
        raise ValueError("numbers must be finite")
    return text


def parse(raw):
    """The validated list of households, or ValueError."""
    body = json.loads(
        raw,
        parse_constant=_finite,  # NaN, Infinity, -Infinity
        parse_float=lambda text: float(_finite(text)),
        parse_int=lambda text: int(_finite(text)),
    )
    households = body.get("households") if isinstance(body, dict) else None
    if not isinstance(households, list) or not 1 <= len(households) <= MAX_HOUSEHOLDS:
        raise ValueError(f"body must be {{\"households\": [1..{MAX_HOUSEHOLDS} households]}}")
    for i, household in enumerate(households):
        _check(household, f"households[{i}]")
    return households


def _check(household, where):
    if not isinstance(household, dict) or not FIELDS <= household.keys() <= FIELDS | {"overrides"}:
        raise ValueError(f"{where} needs exactly {', '.join(sorted(FIELDS))} and optional overrides")
    if not isinstance(household["id"], str):
        raise ValueError(f"{where}.id must be a string")
    if type(household["tax_year"]) is not int:
        raise ValueError(f"{where}.tax_year must be an integer")
    if household["filing_status"] not in FILING_STATUSES:
        raise ValueError(f"{where}.filing_status must be one of {', '.join(FILING_STATUSES)}")
    situation = household["situation"]
    # Every group is required so a household computes the same alone or merged into a batch.
    if not isinstance(situation, dict) or set(situation) != set(ENTITIES):
        raise ValueError(f"{where}.situation must have exactly {', '.join(ENTITIES)}")
    for plural, entities in situation.items():
        if not isinstance(entities, dict) or not all(isinstance(e, dict) for e in entities.values()):
            raise ValueError(f"{where}.situation.{plural} must map ids to entities")
        for entity in entities.values():
            for name, value in entity.items():
                if name == "members" and plural != "people":
                    if not (isinstance(value, list) and all(isinstance(m, str) for m in value)):
                        raise ValueError(f"{where}.situation.{plural}: members must be a list of person ids")
                elif not isinstance(value, dict):
                    raise ValueError(f"{where}.situation.{plural}: {name} must be {{period: value}}")
    overrides = household.get("overrides", {})
    if not isinstance(overrides, dict) or not all(isinstance(v, dict) for v in overrides.values()):
        raise ValueError(f"{where}.overrides must map parameter paths to {{period: value}}")
    for path in overrides:
        BASELINE.parameters.get_child(path)  # ValueError for an unknown path, before a slow rebuild


def run_batch(households):
    """One result per household, in order. Households sharing overrides run as one
    merged simulation, which costs about the same as a single household."""
    groups = {}
    for i, household in enumerate(households):
        key = json.dumps(household.get("overrides", {}), sort_keys=True)
        groups.setdefault(key, []).append(i)
    results = [None] * len(households)
    for key, indices in groups.items():
        try:  # PolicyEngine rejects the input here: unknown variable or parameter, bad period or value
            system = _reformed_system(key) if key != "{}" else None
            sim = Simulation(situation=_merge(households, indices), tax_benefit_system=system)
            parameters = sim.tax_benefit_system.parameters  # after any overrides
            schedules = {i: _schedules(parameters, households[i]) for i in indices}
        except Exception as error:
            raise ValueError(f"PolicyEngine rejected the input: {error}") from None
        for i in indices:
            household = households[i]
            _fill(sim, i, household["situation"])
            results[i] = {
                "id": household["id"],
                "tax_year": household["tax_year"],
                "filing_status": household["filing_status"],
                "situation": household["situation"],
                **schedules[i],
            }
    return results


_reformed = {}  # at most one {overrides_json: system}


def _reformed_system(overrides_json):
    """PolicyEngine rebuilds its whole parameter tree for a reform (~12 s, ~1.3 GB), so keep
    the last one, and free it before building the next so a worker never holds two."""
    if overrides_json not in _reformed:
        _reformed.clear()
        gc.collect()  # the old system and its last simulation reference each other
        reform = Reform.from_dict(json.loads(overrides_json), country_id="us")
        _reformed[overrides_json] = CountryTaxBenefitSystem(reform=reform)
    return _reformed[overrides_json]


def _schedules(parameters, household):
    year, status = household["tax_year"], household["filing_status"]
    return {
        "ordinary_brackets": _schedule(parameters.gov.irs.income.bracket, year, status),
        "capital_gains_brackets": _schedule(parameters.gov.irs.capital_gains, year, status),
    }


def _schedule(node, year, status):
    """[{threshold, rate}] where threshold is the bracket's lower bound;
    PolicyEngine's thresholds.N is the top of bracket N."""
    at = f"{year}-01-01"
    rates, tops = node.rates.children, node.thresholds.children
    keys = sorted(rates, key=int)
    lows = [0] + [tops[k].children[status](at) for k in keys[:-1]]
    values = [(low, rates[k](at)) for low, k in zip(lows, keys)]
    if any(v is None for pair in values for v in pair):
        raise ValueError(f"no {node.name} schedule for {year}")
    return [{"threshold": float(low), "rate": float(rate)} for low, rate in values]


def _merge(households, indices):
    """One situation for many households: each entity id gets its batch index as a prefix."""
    merged = {plural: {} for plural in ENTITIES}
    for i in indices:
        for plural, entities in copy.deepcopy(households[i]["situation"]).items():
            for entity_id, entity in entities.items():
                if plural != "people":
                    entity["members"] = [f"{i}:{m}" for m in entity.get("members", [])]
                merged[plural][f"{i}:{entity_id}"] = entity
    return merged


def _fill(sim, i, situation):
    """Replace every null in the household's situation with its computed value."""
    for plural, entities in situation.items():
        population = sim.get_population(plural)
        for entity_id, entity in entities.items():
            row = population.get_index(f"{i}:{entity_id}")
            for name, periods in entity.items():
                if name == "members":
                    continue
                for period, value in periods.items():
                    if value is None:
                        periods[period] = _cell(sim.calculate(name, period), row)


def _cell(array, row):
    if isinstance(array, EnumArray):
        return str(array.decode_to_str()[row])
    if array.dtype.kind == "f":
        value = float(str(array[row]))  # str() keeps float32 short, as PolicyEngine's household API does
        return value if math.isfinite(value) else None
    return array.tolist()[row]


WARM_UP = {
    "id": "warm-up",
    "tax_year": 2026,
    "filing_status": "SINGLE",
    "situation": {
        "people": {"you": {"age": {"2026": 40}, "employment_income": {"2026": 50000}}},
        "tax_units": {"tu": {"members": ["you"], "income_tax": {"2026": None}}},
        "families": {"f": {"members": ["you"]}},
        "spm_units": {"s": {"members": ["you"]}},
        "marital_units": {"m": {"members": ["you"]}},
        "households": {"h": {"members": ["you"], "state_name": {"2026": "CA"}}},
    },
}
run_batch([copy.deepcopy(WARM_UP)])  # under gunicorn --preload this runs in the master, before the fork
_warm = True
