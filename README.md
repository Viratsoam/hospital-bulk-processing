# hospital-bulk-processing

A bulk-processing service layered on top of the Hospital Directory API. The
first milestone implements CSV ingestion and validation; bulk creation and the
upstream API integration come later.

## Layout

```
app/
  main.py                 Application factory (uvicorn app.main:app), lifespan
                          manages the shared HospitalAPIClient
  api/
    dependencies.py       FastAPI dependency providers (DI seam for tests)
    routes/
      health.py           GET /health liveness probe
      csv.py              POST /hospitals/validate
  clients/
    hospital_api_client.py  Async client for the Hospital Directory API
                          (create hospital, fetch batch, activate batch)
  core/
    config.py             Settings (env vars, HBP_ prefix, .env support)
    logging.py            Process logging setup
  repositories/
    base.py               In-memory storage abstraction
  schemas/
    csv.py                Pydantic models and error codes
  services/
    csv_parser.py         Pure CSV mechanics (decode, parse, normalize)
    validation_service.py Business rules (headers, rows, limits)
tests/
  conftest.py             Shared fixtures and CSV builder helper
  test_validation_service.py  Unit tests, one per validation rule
  test_api.py             HTTP-level tests through TestClient
  test_hospital_api_client.py  Client tests: respx-mocked HTTP, injected
                          transports for connection/timeout failures
```

## Installation

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Run

```bash
cp .env.example .env        # optional; all values have defaults
uvicorn app.main:app --reload
```

Swagger UI: <http://127.0.0.1:8000/docs>
OpenAPI JSON: <http://127.0.0.1:8000/openapi.json>

## Test

```bash
pytest -v
```

## Usage

```bash
curl -F "file=@hospitals.csv" http://127.0.0.1:8000/hospitals/validate
```

The endpoint returns `200 OK` with `{"valid": true, "hospitals": [...]}` for
acceptable files and `{"valid": false, "errors": [...]}` for rejected ones, so
clients can always inspect every problem in a single pass.

## Architecture notes

- **Layering.** Routes translate HTTP; the validation service owns business
  rules; the parser owns CSV mechanics. Neither service touches I/O, so unit
  tests run against raw byte payloads with no server.
- **Fail-exhaustive reporting.** Validation returns every problem found
  (header issues, per-row issues, code + row + field + message) instead of
  stopping at the first, so a user fixes a whole file at once.
- **No partial success.** Either the entire file is valid or it is rejected;
  `hospitals` is populated only when `valid` is true.
- **Preserve originals.** Row numbers are the physical CSV line where a record
  begins (so multi-line quoted values stay addressable), and duplicate records
  are kept as distinct records by design.
- **Configuration.** One `Settings` object, cached; environment variables use
  the `HBP_` prefix, and tests inject explicit instances so they are immune to
  the ambient environment.
