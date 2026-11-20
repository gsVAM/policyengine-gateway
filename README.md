# policyengine-gateway

A small HTTP wrapper (`app.py`, Flask under gunicorn) over PolicyEngine's **unmodified**
[`policyengine-us`](https://pypi.org/project/policyengine-us/) and
[`policyengine-core`](https://pypi.org/project/policyengine-core/) packages, installed from PyPI
at the exact versions pinned in `requirements.txt`. No PolicyEngine source is copied, vendored or
changed. The wrapper is licensed under the **AGPL-3.0** (see `LICENSE`), as PolicyEngine is.
Deployed on Aptible (app `policyengine-gateway`, port 8080).

Stateless. It never logs a request or response body: they hold a household's tax facts.

## Routes

- `GET /healthcheck` (open): `200 ok` once PolicyEngine is imported and one warm-up simulation has run.
  The warm-up runs in the gunicorn master (`preload`), before the fork, so every worker starts warm.
- Every other route needs `Authorization: Bearer <API_KEY>`; a missing or wrong key gets `401`.
- `POST /calculate`: a batch of 1 to 100 households, one result per household in request order.

### `POST /calculate`

```json
{"households": [{
  "id": "client-1",
  "tax_year": 2026,
  "filing_status": "SINGLE",
  "situation": {
    "people": {"you": {"age": {"2026": 40}, "employment_income": {"2026": 50000}}},
    "tax_units": {"tu": {"members": ["you"], "income_tax": {"2026": null}}},
    "families": {"f": {"members": ["you"]}},
    "spm_units": {"s": {"members": ["you"]}},
    "marital_units": {"m": {"members": ["you"]}},
    "households": {"h": {"members": ["you"], "state_name": {"2026": "CA"}}}
  },
  "overrides": {"gov.irs.income.bracket.thresholds.1.SINGLE": {"2026-01-01.2026-12-31": 12400}}
}]}
```

- `situation` is PolicyEngine's household situation (the household API's shape). Set a variable's
  period to `null` to have it computed. All six entity groups are required, so that a household
  computes the same alone or in a batch.
- `filing_status` (`SINGLE`, `JOINT`, `SEPARATE`, `HEAD_OF_HOUSEHOLD`, `SURVIVING_SPOUSE`) and
  `tax_year` select the bracket schedules returned. They are not written into the situation; set
  `filing_status` there too if PolicyEngine must not infer it.
- `overrides` (optional) is PolicyEngine parameter path → `{period: value}`, applied as a reform
  (`Reform.from_dict`) for that household. Period keys follow PolicyEngine: `"2026-01-01.2026-12-31"`
  is that year only; a bare `"2026-01-01"` applies from then on.

Response (`200`):

```json
{"identity": {"policyengine_us": "2.140.3", "policyengine_core": "3.36.1",
              "wrapper_commit": "<git sha>", "image_build": "<CI run id>"},
 "results": [{
  "id": "client-1", "tax_year": 2026, "filing_status": "SINGLE",
  "situation": {"...": "the request's situation with every null filled in, e.g. income_tax {\"2026\": 3820.0}"},
  "ordinary_brackets": [{"threshold": 0.0, "rate": 0.1}, {"threshold": 12400.0, "rate": 0.12}, "..."],
  "capital_gains_brackets": [{"threshold": 0.0, "rate": 0.0}, {"threshold": 49450.0, "rate": 0.15},
                             {"threshold": 545500.0, "rate": 0.2}]
 }]}
```

Bracket schedules are read from PolicyEngine's own parameters (`gov.irs.income.bracket`,
`gov.irs.capital_gains`) for the year and filing status, after any overrides. Each `threshold` is
the bracket's lower bound in taxable income. A non-finite computed value is returned as `null`.

| Status | When | Body |
| --- | --- | --- |
| 200 | computed | `{"results": [...], "identity": {...}}` |
| 400 | malformed JSON, a non-finite number, a bad field, more than 100 households, or input PolicyEngine rejects | `{"error": "...", "identity": {...}}` |
| 401 | missing or wrong key | `{"error": "unauthorized"}` |
| 413 | body over 2 MB | |
| 500 | PolicyEngine failed while computing | `{"error": "calculation failed", "identity": {...}}` |
| 503 | this process is already simulating; retry (another process may be free) | `{"error": "busy"}`, header `Connection: close` |

In a PolicyEngine error message, entity ids carry the household's batch index as a prefix (`0:tu`).

## Cost and limits

- Each process runs one simulation at a time; a concurrent `/calculate` gets an immediate `503`, never
  a queue. Households that share the same `overrides` run as one merged simulation, about 0.25 s
  for the whole batch.
- A new `overrides` set makes PolicyEngine rebuild its parameter tree: about 10-15 s and about
  1.3 GB per process. Each process keeps its last override set, so repeats are fast. Send one
  override set per request; each extra distinct set in a request costs another rebuild.
- Watchdog: a process whose simulation runs past `WATCHDOG_SECONDS` exits; gunicorn's master forks
  a warm replacement and the caller sees a dropped connection.
- Memory, measured in Docker with 2 workers: about 1.3 GB idle, about 4.4 GB with both workers
  holding an override set. The Aptible container is 7 GB.

## Configuration

| Env | Default | |
| --- | --- | --- |
| `API_KEY` | (required) | the container refuses to start without it |
| `PORT` | `8080` | |
| `WORKERS` | `2` | gunicorn processes |
| `THREADS` | `2` | per process, at least 2 so `/healthcheck` is always answered |
| `WATCHDOG_SECONDS` | `30` | |

`WRAPPER_COMMIT` and `IMAGE_BUILD` are baked in at build time (`--build-arg GIT_SHA=...
--build-arg BUILD_ID=...`; CI passes the commit SHA and the workflow run id).

## Develop

```sh
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest==9.2.0
.venv/bin/pytest -q          # imports PolicyEngine first: expect about 30-60 s
```

- New PolicyEngine version: bump the pins in `requirements.txt`, run the tests, push to `main`,
  deploy the new `ghcr.io/gsvam/policyengine-gateway:sha-<commit>` image.
- Verify any deployment: `./smoke-test.sh <base-url> <api-key>`.
