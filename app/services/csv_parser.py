"""Low-level CSV mechanics.

This module is deliberately free of business rules: it turns raw bytes into
``(row_number, cells)`` tuples and reports structural problems in CSV terms.
Everything about *what a hospital row means* lives in
:mod:`app.services.validation_service`, which keeps the two independently
testable.
"""

import csv
import io
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Encodings attempted in order. ``utf-8-sig`` is first so a UTF-8 BOM is
#: consumed transparently and never leaks into the first header name.
_ENCODINGS: Tuple[str, ...] = ("utf-8-sig", "utf-8")

#: Column names required in the CSV header.
REQUIRED_COLUMNS: Tuple[str, ...] = ("name", "address")

#: Optional column names recognized in the CSV header.
OPTIONAL_COLUMNS: Tuple[str, ...] = ("phone",)

#: Every column this service understands.
KNOWN_COLUMNS: Tuple[str, ...] = REQUIRED_COLUMNS + OPTIONAL_COLUMNS


@dataclass
class ParsedRow:
    """One logical CSV record.

    Attributes:
        row_number: 1-based line number where the record *starts* in the source
            file. Records containing embedded newlines span several lines, so
            this is the start line rather than the line the record ended on.
        cells: Raw cell values, positionally aligned with the header.
        blank: True when the record carries no content at all. A blank row is
            recorded rather than dropped so the validator can report it.
    """

    row_number: int
    cells: List[str] = field(default_factory=list)
    blank: bool = False


@dataclass
class CsvParseOutcome:
    """Result of the purely structural parse.

    Attributes:
        header: Normalized header names, or an empty list if the file had none.
        rows: Every non-header record, including blank ones.
        error: A structural failure description, or None on success.
    """

    header: List[str] = field(default_factory=list)
    rows: List[ParsedRow] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def failed(self) -> bool:
        """Whether a structural failure prevented a usable parse."""
        return self.error is not None


def decode_csv_bytes(payload: bytes) -> Tuple[Optional[str], Optional[str]]:
    """Decode raw upload bytes into text.

    Tries UTF-8 with BOM handling first, then plain UTF-8. A BOM is stripped
    rather than reported, since spreadsheet exports add one routinely.

    Args:
        payload: Raw bytes from the uploaded file.

    Returns:
        A ``(text, error)`` pair; exactly one element is ``None``.
    """
    if payload.startswith(b"\xef\xbb\xbf"):
        payload = payload[3:]
    for encoding in _ENCODINGS:
        try:
            return payload.decode(encoding), None
        except UnicodeDecodeError:
            continue
    return None, "File is not valid UTF-8 text."


def parse_csv(text: str) -> CsvParseOutcome:
    """Parse CSV text into a header and row list.

    Uses strict dialect handling so unterminated quotes and stray characters
    after a closing quote surface as errors instead of being silently
    reinterpreted. Blank lines are preserved as blank records, because whether
    they are acceptable is a business decision, not a parsing one.

    Args:
        text: Decoded CSV content.

    Returns:
        A :class:`CsvParseOutcome` describing the parse.
    """
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    outcome = CsvParseOutcome()

    previous_line_number = 0
    try:
        header_cells = next(reader)
    except StopIteration:
        outcome.error = "File contains no data."
        return outcome
    except csv.Error as exc:
        outcome.error = f"Malformed CSV: {exc}."
        return outcome

    start_line = 1
    previous_line_number = reader.line_num
    outcome.header = [cell.strip() for cell in header_cells]

    while True:
        try:
            cells = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            outcome.error = f"Malformed CSV at line {start_line}: {exc}."
            return outcome

        # line_num reports the line the record *ended* on; subtract the number
        # of physical lines consumed so the reported row is the start line.
        end_line = reader.line_num
        span = max(1, end_line - previous_line_number)
        start_line = end_line - span + 1
        previous_line_number = end_line

        outcome.rows.append(
            ParsedRow(
                row_number=start_line,
                cells=cells,
                blank=not any(cell.strip() for cell in cells),
            )
        )

    return outcome


def map_rows_to_fields(
    header: List[str], rows: List[ParsedRow]
) -> Iterator[Tuple[ParsedRow, Dict[str, Optional[str]]]]:
    """Align each row's cells with the header by column name.

    Missing trailing cells are treated as absent rather than an error, matching
    how spreadsheets and CSV writers commonly omit trailing empties. Extra
    cells beyond the header are ignored here; the validator reports them via
    :func:`extra_cells` so the two concerns stay separate.

    Args:
        header: Normalized header column names.
        rows: Parsed rows to align.

    Yields:
        ``(row, fields)`` pairs where ``fields`` maps column name to raw value
        or ``None`` when the column had no cell in that row.
    """
    # Headers are matched case-insensitively (see the validator's header
    # checks), so the alignment index must normalize the same way.
    index_by_column = {name.lower(): position for position, name in enumerate(header)}

    for row in rows:
        fields: Dict[str, Optional[str]] = {}
        for column in KNOWN_COLUMNS:
            position = index_by_column.get(column)
            if position is None or position >= len(row.cells):
                fields[column] = None
            else:
                fields[column] = row.cells[position]
        yield row, fields


def extra_cells(header: List[str], row: ParsedRow) -> List[str]:
    """Return any populated cells lying beyond the last header column.

    A row with more cells than the header is almost always a sign of an
    unquoted comma inside a value, which would otherwise silently shift data
    into the wrong columns. Trailing empties are ignored so a harmless extra
    comma does not fail the upload.

    Args:
        header: Normalized header column names.
        row: Row to inspect.

    Returns:
        The non-empty surplus cells, in order. Empty when there are none.
    """
    surplus = row.cells[len(header) :]
    return [cell for cell in surplus if cell.strip()]


def normalize(value: Optional[str]) -> Optional[str]:
    """Strip surrounding whitespace from a cell value.

    Args:
        value: Raw cell text, possibly ``None``.

    Returns:
        The stripped value, or ``None`` when the value was absent or became
        empty. Collapsing an all-whitespace cell to ``None`` lets callers treat
        "missing" and "present but blank" identically.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
