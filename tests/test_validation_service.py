"""Unit tests for :class:`CsvValidationService`."""

from app.core.config import Settings
from app.schemas.csv import ValidationErrorCode
from app.services.csv_parser import decode_csv_bytes, normalize, parse_csv
from app.services.validation_service import CsvValidationService

from tests.conftest import csv_bytes


def _codes(result) -> set:
    """Collect the error codes of a result."""
    return {error.code for error in result.errors}


# -- valid inputs -------------------------------------------------------------


def test_valid_csv_returns_records(service: CsvValidationService) -> None:
    payload = csv_bytes(
        "name,address,phone",
        "St. Mary, 1 Hill St, 555-0101",
        "Grace Hospital, 2 Lake Rd",
        "Riverside, 3 River Ave, 555-0103",
    )
    result = service.validate_csv(payload)

    assert result.valid is True
    assert result.total_records == 3
    assert result.errors == []
    assert [hospital.name for hospital in result.hospitals] == [
        "St. Mary",
        "Grace Hospital",
        "Riverside",
    ]
    # Row numbers are original CSV line numbers (header is line 1).
    assert [hospital.row for hospital in result.hospitals] == [2, 3, 4]
    assert [hospital.phone for hospital in result.hospitals] == [
        "555-0101",
        None,
        "555-0103",
    ]


def test_optional_phone_absent_is_none(service: CsvValidationService) -> None:
    result = service.validate_csv(csv_bytes("name,address", "A, 1 B"))
    assert result.valid is True
    assert result.hospitals[0].phone is None


def test_optional_phone_empty_cell_is_none(service: CsvValidationService) -> None:
    result = service.validate_csv(csv_bytes("name,address,phone", "A, 1 B,"))
    assert result.valid is True
    assert result.hospitals[0].phone is None


def test_whitespace_is_normalized(service: CsvValidationService) -> None:
    payload = csv_bytes(
        "  name , address , phone ",
        "  St. Mary  ,   1 Hill St  ,  555-0101  ",
        "Grace,2 Lake Rd,\t",
    )
    result = service.validate_csv(payload)

    assert result.valid is True
    hospital = result.hospitals[0]
    assert hospital.name == "St. Mary"
    assert hospital.address == "1 Hill St"
    assert hospital.phone == "555-0101"
    # A whitespace-only phone cell normalizes to absent.
    assert result.hospitals[1].phone is None


def test_utf8_bom_is_stripped(service: CsvValidationService) -> None:
    result = service.validate_csv(csv_bytes("name,address", "A, 1 B", bom=True))
    assert result.valid is True
    assert result.hospitals[0].name == "A"


def test_duplicate_records_are_preserved(service: CsvValidationService) -> None:
    payload = csv_bytes(
        "name,address",
        "Same, 1 Place",
        "Same, 1 Place",
        "Same, 1 Place",
    )
    result = service.validate_csv(payload)

    assert result.valid is True
    assert result.total_records == 3
    assert [hospital.row for hospital in result.hospitals] == [2, 3, 4]


def test_exactly_max_records_is_accepted(
    service: CsvValidationService,
) -> None:
    lines = ["name,address"] + [
        "Hospital {0}, {0} Street".format(index) for index in range(20)
    ]
    result = service.validate_csv(csv_bytes(*lines))
    assert result.valid is True
    assert result.total_records == 20


def test_quoted_commas_and_newlines_are_valid(service: CsvValidationService) -> None:
    payload = (
        'name,address\n"St. Mary","1 Hill St, Suite 2"\n'
        '"Grace","2 Lake Rd, Apt 3\nAnnex B"\n'
    ).encode("utf-8")
    result = service.validate_csv(payload)
    assert result.valid is True
    assert result.total_records == 2
    assert result.hospitals[0].address == "1 Hill St, Suite 2"
    assert result.hospitals[1].address == "2 Lake Rd, Apt 3\nAnnex B"


def test_case_insensitive_headers(service: CsvValidationService) -> None:
    result = service.validate_csv(csv_bytes("Name,Address,Phone", "A, 1 B, 555"))
    assert result.valid is True
    assert result.hospitals[0].phone == "555"


# -- document-level failures --------------------------------------------------


def test_empty_file(service: CsvValidationService) -> None:
    result = service.validate_csv(b"")
    assert result.valid is False
    assert result.total_records == 0
    assert _codes(result) == {ValidationErrorCode.EMPTY_FILE}


def test_whitespace_only_file_is_empty(service: CsvValidationService) -> None:
    result = service.validate_csv(b"   \n  \n")
    assert result.valid is False
    assert _codes(result) == {ValidationErrorCode.EMPTY_FILE}


def test_header_only_csv(service: CsvValidationService) -> None:
    result = service.validate_csv(csv_bytes("name,address,phone"))
    assert result.valid is False
    assert _codes(result) == {ValidationErrorCode.HEADER_ONLY}
    assert result.errors[0].row is None


def test_blank_rows_below_header_are_header_only(
    service: CsvValidationService,
) -> None:
    result = service.validate_csv(csv_bytes("name,address", ""))
    assert result.valid is False
    assert _codes(result) == {ValidationErrorCode.HEADER_ONLY}


def test_too_many_records(service: CsvValidationService) -> None:
    lines = ["name,address"] + [
        "Hospital {0}, {0} Street".format(index) for index in range(21)
    ]
    result = service.validate_csv(csv_bytes(*lines))
    assert result.valid is False
    error = result.errors[0]
    assert error.code is ValidationErrorCode.TOO_MANY_RECORDS
    assert error.row is None
    assert "21" in error.message
    # The count of offending rows is reported for diagnostics.
    assert result.total_records == 21


# -- header failures ----------------------------------------------------------


def test_missing_required_headers(service: CsvValidationService) -> None:
    result = service.validate_csv(csv_bytes("name"))
    assert result.valid is False
    assert _codes(result) == {ValidationErrorCode.MISSING_HEADERS}
    assert result.errors[0].field == "address"


def test_missing_all_required_headers(service: CsvValidationService) -> None:
    result = service.validate_csv(csv_bytes("phone"))
    assert result.valid is False
    assert {error.field for error in result.errors} == {"name", "address"}


def test_unknown_column_rejected(service: CsvValidationService) -> None:
    result = service.validate_csv(csv_bytes("name,address,city", "A, 1 B, C"))
    assert result.valid is False
    error = result.errors[0]
    assert error.code is ValidationErrorCode.UNEXPECTED_HEADERS
    assert error.field == "city"


def test_duplicate_headers_rejected(service: CsvValidationService) -> None:
    result = service.validate_csv(csv_bytes("name,name,address", "A, B, 1 C"))
    assert result.valid is False
    assert _codes(result) == {ValidationErrorCode.DUPLICATE_HEADERS}
    assert result.errors[0].field == "name"


def test_empty_header_cell_rejected(service: CsvValidationService) -> None:
    result = service.validate_csv(csv_bytes("name,address,", "A, 1 B, 555"))
    assert result.valid is False
    assert ValidationErrorCode.UNEXPECTED_HEADERS in _codes(result)


# -- row-level failures -------------------------------------------------------


def test_missing_name_reported_with_row(service: CsvValidationService) -> None:
    result = service.validate_csv(csv_bytes("name,address", ", 1 B"))
    error = result.errors[0]
    assert result.valid is False
    assert error.code is ValidationErrorCode.MISSING_NAME
    assert error.row == 2
    assert error.field == "name"


def test_missing_address_reported_with_row(service: CsvValidationService) -> None:
    result = service.validate_csv(csv_bytes("name,address", "St. Mary,"))
    error = result.errors[0]
    assert result.valid is False
    assert error.code is ValidationErrorCode.MISSING_ADDRESS
    assert error.row == 2
    assert error.field == "address"


def test_multiple_row_problems_all_reported(service: CsvValidationService) -> None:
    payload = csv_bytes(
        "name,address",
        ", 1 B",
        "St. Mary,",
        "Grace, 2 Lake Rd",
    )
    result = service.validate_csv(payload)

    assert result.valid is False
    assert [(error.row, error.code) for error in result.errors] == [
        (2, ValidationErrorCode.MISSING_NAME),
        (3, ValidationErrorCode.MISSING_ADDRESS),
    ]


def test_empty_row_reported_with_row(service: CsvValidationService) -> None:
    payload = csv_bytes(
        "name,address",
        "A, 1 B",
        ",,",
        "C, 3 D",
    )
    result = service.validate_csv(payload)
    assert result.valid is False
    error = result.errors[0]
    assert error.code is ValidationErrorCode.EMPTY_ROW
    assert error.row == 3


def test_surplus_cells_reported(service: CsvValidationService) -> None:
    result = service.validate_csv(csv_bytes("name,address", "A, 1 B, 555, x"))
    error = result.errors[0]
    assert result.valid is False
    assert error.code is ValidationErrorCode.COLUMN_COUNT_MISMATCH
    assert error.row == 2


def test_short_row_with_missing_address(service: CsvValidationService) -> None:
    result = service.validate_csv(csv_bytes("name,address", "A"))
    error = result.errors[0]
    assert result.valid is False
    assert error.code is ValidationErrorCode.MISSING_ADDRESS
    assert error.row == 2


# -- encoding ------------------------------------------------------------------


def test_non_utf8_bytes_rejected(service: CsvValidationService) -> None:
    result = service.validate_csv(b"name,address\nSt. M\xe4ry, 1 B")
    assert result.valid is False
    assert _codes(result) == {ValidationErrorCode.INVALID_ENCODING}


def test_file_over_size_limit(settings: Settings) -> None:
    payload = "name,address\nA, 1 B\n".encode("utf-8")
    service = CsvValidationService(settings=settings)
    assert service.validate_csv(payload).valid is True

    small_settings = settings.model_copy(
        update={"max_csv_size_bytes": len(payload) - 1}
    )
    limited_service = CsvValidationService(settings=small_settings)
    result = limited_service.validate_csv(payload)
    assert result.valid is False
    assert _codes(result) == {ValidationErrorCode.FILE_TOO_LARGE}


# -- parser helpers -----------------------------------------------------------


def test_parse_csv_reports_malformed_structure() -> None:
    # An unterminated quote is rejected under strict dialect handling.
    outcome = parse_csv('name,address\n"A, 1 B')
    assert outcome.failed
    assert "Malformed CSV" in outcome.error


def test_parse_csv_on_lone_blank_line() -> None:
    # A single blank line parses as an empty header with no rows; the
    # validator turns that into the header-only / empty-file outcome.
    outcome = parse_csv("\n")
    assert not outcome.failed
    assert outcome.header == []
    assert outcome.rows == []


def test_decode_csv_bytes_strips_bom() -> None:
    text, error = decode_csv_bytes(b"\xef\xbb\xbfname,address")
    assert error is None
    assert text == "name,address"


def test_normalize_collapses_blank_values() -> None:
    assert normalize("  x  ") == "x"
    assert normalize("   ") is None
    assert normalize(None) is None
