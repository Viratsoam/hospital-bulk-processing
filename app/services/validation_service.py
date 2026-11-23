"""CSV validation business rules.

The service answers one question: *is this upload acceptable, and if not, why?*
It performs no I/O and makes no external calls, so it is fully synchronous and
trivially unit-testable. Callers get an exhaustive list of problems rather than
the first one, so a user can fix an entire file in a single pass.
"""

from typing import List, Optional, Set, Tuple

from app.core.config import Settings
from app.core.logging import get_logger
from app.schemas.csv import (
    CsvValidationResult,
    HospitalRow,
    ValidationError,
    ValidationErrorCode,
)
from app.services.csv_parser import (
    KNOWN_COLUMNS,
    REQUIRED_COLUMNS,
    decode_csv_bytes,
    extra_cells,
    map_rows_to_fields,
    normalize,
    parse_csv,
)

logger = get_logger(__name__)


class CsvValidationService:
    """Validates uploaded CSV payloads against the hospital record contract."""

    def __init__(self, settings: Settings) -> None:
        """Initialize the service.

        Args:
            settings: Supplies the record-count and file-size limits.
        """
        self._settings = settings

    def validate_csv(self, payload: bytes) -> CsvValidationResult:
        """Validate a raw CSV upload.

        Args:
            payload: Raw bytes exactly as received from the multipart upload.

        Returns:
            A :class:`CsvValidationResult`. ``valid`` is true only when the whole
            file is acceptable and ``hospitals`` is populated.
        """
        errors: List[ValidationError] = []

        if not payload or not payload.strip():
            return self._failure(
                errors=[
                    self._error(
                        ValidationErrorCode.EMPTY_FILE, "The uploaded file is empty."
                    )
                ]
            )

        if len(payload) > self._settings.max_csv_size_bytes:
            return self._failure(
                errors=[
                    self._error(
                        ValidationErrorCode.FILE_TOO_LARGE,
                        "File is {0} bytes, exceeding the {1} byte limit.".format(
                            len(payload), self._settings.max_csv_size_bytes
                        ),
                    )
                ]
            )

        text, decode_error = decode_csv_bytes(payload)
        if decode_error is not None:
            return self._failure(
                errors=[self._error(ValidationErrorCode.INVALID_ENCODING, decode_error)]
            )
        assert text is not None  # narrows Optional for type checkers

        outcome = parse_csv(text)
        if outcome.failed:
            code = (
                ValidationErrorCode.EMPTY_FILE
                if not outcome.header
                else ValidationErrorCode.MALFORMED_CSV
            )
            return self._failure(
                errors=[self._error(code, outcome.error or "Malformed CSV.")]
            )

        header_errors = self._validate_header(outcome.header)
        if header_errors:
            # Without a trustworthy header, row-level findings would be
            # misattributed to the wrong columns. Report header problems alone.
            return self._failure(errors=header_errors)

        if not outcome.rows:
            return self._failure(
                errors=[
                    self._error(
                        ValidationErrorCode.HEADER_ONLY,
                        "File contains a header row but no hospital records.",
                    )
                ]
            )

        data_rows = [row for row in outcome.rows if not row.blank]
        if not data_rows:
            return self._failure(
                errors=[
                    self._error(
                        ValidationErrorCode.HEADER_ONLY,
                        "File contains a header row but no hospital records.",
                    )
                ]
            )

        if len(data_rows) > self._settings.max_hospitals_per_batch:
            return self._failure(
                errors=[
                    self._error(
                        ValidationErrorCode.TOO_MANY_RECORDS,
                        "File contains {0} hospital records, exceeding the "
                        "maximum of {1}.".format(
                            len(data_rows), self._settings.max_hospitals_per_batch
                        ),
                    )
                ],
                total_records=len(data_rows),
            )

        hospitals, row_errors = self._validate_rows(outcome.header, outcome.rows)
        errors.extend(row_errors)

        if errors:
            # Partial success is not a valid outcome for this contract: either
            # the whole file can be processed or none of it can.
            return self._failure(errors=errors, total_records=len(data_rows))

        logger.info(
            "CSV validation succeeded with %d record(s).", len(hospitals)
        )
        return CsvValidationResult(
            valid=True,
            total_records=len(hospitals),
            hospitals=hospitals,
            errors=[],
        )

    # -- header validation --------------------------------------------------

    def _validate_header(self, header: List[str]) -> List[ValidationError]:
        """Check that the header declares the required columns exactly once.

        Header names are matched case-insensitively, since spreadsheet exports
        vary in capitalization. Unknown columns are rejected rather than
        ignored: a typo such as ``adress`` must not be silently dropped while
        the upload appears to succeed.

        Args:
            header: Raw header cells, already stripped of outer whitespace.

        Returns:
            Every header-level problem found.
        """
        errors: List[ValidationError] = []
        normalized = [cell.lower() for cell in header]

        seen: Set[str] = set()
        duplicates: Set[str] = set()
        for name in normalized:
            if name and name in seen:
                duplicates.add(name)
            seen.add(name)

        for name in sorted(duplicates):
            errors.append(
                self._error(
                    ValidationErrorCode.DUPLICATE_HEADERS,
                    "Column '{0}' appears more than once in the header.".format(name),
                    field=name,
                )
            )

        for name in REQUIRED_COLUMNS:
            if name not in seen:
                errors.append(
                    self._error(
                        ValidationErrorCode.MISSING_HEADERS,
                        "Required column '{0}' is missing from the header. "
                        "Expected columns: {1}.".format(
                            name, ", ".join(KNOWN_COLUMNS)
                        ),
                        field=name,
                    )
                )

        for position, name in enumerate(normalized, start=1):
            if not name:
                errors.append(
                    self._error(
                        ValidationErrorCode.UNEXPECTED_HEADERS,
                        "Column {0} in the header has an empty name.".format(position),
                    )
                )
            elif name not in KNOWN_COLUMNS:
                errors.append(
                    self._error(
                        ValidationErrorCode.UNEXPECTED_HEADERS,
                        "Unknown column '{0}'. Supported columns: {1}.".format(
                            name, ", ".join(KNOWN_COLUMNS)
                        ),
                        field=name,
                    )
                )

        return errors

    # -- row validation -----------------------------------------------------

    def _validate_rows(
        self, header: List[str], rows: List
    ) -> Tuple[List[HospitalRow], List[ValidationError]]:
        """Validate every data row and build hospital records from the good ones.

        Blank rows, missing values, and short rows are reported with the line
        number where the record begins. Duplicate records are intentionally
        *not* collapsed: two identical rows represent two hospitals that the
        caller asked to create.

        Args:
            header: Normalized header names.
            rows: Every parsed row, including blanks.

        Returns:
            A ``(hospitals, errors)`` pair. ``hospitals`` is only meaningful when
            ``errors`` is empty; the caller enforces that.
        """
        hospitals: List[HospitalRow] = []
        errors: List[ValidationError] = []

        for row, fields in map_rows_to_fields(header, rows):
            if row.blank:
                errors.append(
                    self._error(
                        ValidationErrorCode.EMPTY_ROW,
                        "Row {0} is empty.".format(row.row_number),
                        row=row.row_number,
                    )
                )
                continue

            name = normalize(fields.get("name"))
            address = normalize(fields.get("address"))
            phone = normalize(fields.get("phone"))

            row_has_error = False
            if name is None:
                errors.append(
                    self._error(
                        ValidationErrorCode.MISSING_NAME,
                        "Row {0} is missing a hospital name.".format(row.row_number),
                        row=row.row_number,
                        field="name",
                    )
                )
                row_has_error = True

            if address is None:
                errors.append(
                    self._error(
                        ValidationErrorCode.MISSING_ADDRESS,
                        "Row {0} is missing an address.".format(row.row_number),
                        row=row.row_number,
                        field="address",
                    )
                )
                row_has_error = True

            if row_has_error:
                continue

            surplus = extra_cells(header, row)
            if surplus:
                # More cells than columns means the row cannot be trusted to
                # align with the header, so it is rejected even though the
                # required values parsed cleanly.
                errors.append(
                    self._error(
                        ValidationErrorCode.COLUMN_COUNT_MISMATCH,
                        "Row {0} has {1} value(s) beyond the header's {2} "
                        "column(s). Check for unquoted commas.".format(
                            row.row_number,
                            len(surplus),
                            len(header),
                        ),
                        row=row.row_number,
                    )
                )
                continue

            assert name is not None and address is not None
            hospitals.append(
                HospitalRow(
                    row=row.row_number,
                    name=name,
                    address=address,
                    phone=phone,
                )
            )

        return hospitals, errors

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _error(
        code: ValidationErrorCode,
        message: str,
        row: Optional[int] = None,
        field: Optional[str] = None,
    ) -> ValidationError:
        """Build a :class:`ValidationError` without repeating keyword plumbing."""
        return ValidationError(row=row, field=field, code=code, message=message)

    def _failure(
        self,
        errors: List[ValidationError],
        total_records: int = 0,
    ) -> CsvValidationResult:
        """Assemble an invalid result.

        Args:
            errors: Every error found.
            total_records: Number of data rows parsed, for caller diagnostics.

        Returns:
            An invalid :class:`CsvValidationResult` carrying no hospitals.
        """
        return CsvValidationResult(
            valid=False,
            total_records=total_records,
            hospitals=[],
            errors=errors,
        )
