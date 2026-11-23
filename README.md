# hospital-bulk-processing

A bulk-processing service layered on top of the Hospital Directory API. It
ingests a hospital CSV, validates it, creates every hospital upstream under one
batch UUID, reconciles ambiguous outcomes, and activates the batch when safe.

## Layout

```
app/
  main.py                 Application factory (uvicorn app.main:app), lifespan
                          manages the shared HospitalAPIClient and repository
  api/
    dependencies.py       FastAPI dependency providers (DI seam for tests)
    routes/
      health.py           GET /health liveness probe
      csv.py              POST /hospitals/validate
      bulk.py             POST /hospitals/bulk (create + activate a batch)
  clients/
    hospital_api_client.py  Async client for the Hospital Directory API
                          (create hospital, fetch batch, activate batch)
  core/
    config.py             Settings (env vars, HBP_ prefix, .env support)
    logging.py            Process logging setup
  repositories/
    base.py               In-memory storage abstraction
    batch_repository.py   Batch record storage seam (in-memory reference
                          implementation; deep-copy retrieval)
  schemas/
    csv.py                Pydantic models and error codes
    batch.py              Batch lifecycle models (RowStatus, BatchStatus,
                          BatchRecord, RowResult)
  services/
    csv_parser.py         Pure CSV mechanics (decode, parse, normalize)
    validation_service.py Business rules (headers, rows, limits)
    bulk_processing_service.py  Bulk engine: concurrent creation, ambiguous-
                          outcome reconciliation, activation safety
tests/
  conftest.py             Shared fixtures and CSV builder helper
  test_validation_service.py  Unit tests, one per validation rule
  test_api.py             HTTP-level tests through TestClient
  test_hospital_api_client.py  Client tests: respx-mocked HTTP, injected
                          transports for connection/timeout failures
  test_bulk_processing_service.py  Engine tests against a programmable
                          fake client and recording repository
  test_bulk_api.py        HTTP integration tests for /hospitals/bulk
                          (fake client injected via dependency overrides)
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

### Validate a CSV (dry run)

```bash
curl -F "file=@hospitals.csv" http://127.0.0.1:8000/hospitals/validate
```

The endpoint returns `200 OK` with `{"valid": true, "hospitals": [...]}` for
acceptable files and `{"valid": false, "errors": [...]}` for rejected ones, so
clients can always inspect every problem in a single pass. No hospitals are
created.

### Create and activate a batch

```bash
curl -F "file=@hospitals.csv" http://127.0.0.1:8000/hospitals/bulk
```

**Request** — `multipart/form-data`, file field `file`, a CSV with columns
`name` and `address` (required) and `phone` (optional), at most 20 records:

```
name,address,phone
St. Mary,1 Hill St,555-0101
Grace Hospital,2 Lake Rd,
Riverside,3 River Ave,555-0103
```

The service creates every hospital upstream under one batch UUID, reconciles
any ambiguous creation outcomes by fetching the batch, verifies the upstream
state (exact count, every record accounted for, all explicitly inactive), and
then activates the batch **once** — only when every row is confirmed created.

**Successful response** (`200`), batch activated:

```json
{
  "batch_id": "b4e9f7a3-7b3b-4f1f-9a3a-5f5b8a9c3f2a",
  "status": "completed",
  "total_records": 2,
  "processed_records": 2,
  "successful_records": 2,
  "failed_records": 0,
  "unknown_records": 0,
  "processing_time": 11.4,
  "batch_activated": true,
  "activated_count": 2,
  "error_summary": null,
  "results": [
    {
      "row_number": 2,
      "name": "St. Mary",
      "address": "1 Hill St",
      "phone": "555-0101",
      "status": "created",
      "upstream_hospital_id": 2589,
      "error_message": null
    },
    {
      "row_number": 3,
      "name": "Grace Hospital",
      "address": "2 Lake Rd",
      "phone": null,
      "status": "created",
      "upstream_hospital_id": 2590,
      "error_message": null
    }
  ]
}
```

**Partial-failure response** (`200` — a business outcome, not a server error),
batch not activated:

```json
{
  "batch_id": "b4e9f7a3-7b3b-4f1f-9a3a-5f5b8a9c3f2a",
  "status": "partially_failed",
  "total_records": 2,
  "processed_records": 2,
  "successful_records": 1,
  "failed_records": 1,
  "unknown_records": 0,
  "processing_time": 6.9,
  "batch_activated": false,
  "activated_count": null,
  "error_summary": "1 record(s) failed to create of 2",
  "results": [
    {
      "row_number": 2,
      "name": "St. Mary",
      "address": "1 Hill St",
      "phone": "555-0101",
      "status": "created",
      "upstream_hospital_id": 2589,
      "error_message": null
    },
    {
      "row_number": 3,
      "name": "Grace Hospital",
      "address": "2 Lake Rd",
      "phone": null,
      "status": "failed",
      "upstream_hospital_id": null,
      "error_message": "[validation_error] Upstream create hospital returned HTTP 422"
    }
  ]
}
```

**Batch `status` values:** `processing` (in flight), `completed`,
`partially_failed`, `created_not_activated`, `requires_attention`.
**Row `status` values:** `pending`, `processing`, `created`, `failed`,
`unknown`.

**HTTP status codes**

| Code | Meaning |
|------|---------|
| 200 | Batch processing completed, including partial failures and activation problems. |
| 400 | Invalid CSV (bad headers, missing values, too many records, ...); nothing was created. |
| 413 | Upload exceeds the configured byte limit. |
| 415 | Upload is not a CSV file. |
| 422 | Missing `file` part or a malformed multipart body. |
| 500 | Unexpected internal error. |

Per-row results are returned on every 200 response, including partial
failures, so a caller can see exactly which rows succeeded.

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
