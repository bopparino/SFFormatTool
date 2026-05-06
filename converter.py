"""Salesforce labor remittance -> ADP PRJISEPI.csv converter.

Pure logic, no GUI deps. The GUI calls into convert_workbook() and
write_output_csv(), and may consume the per-step events emitted via the
optional log callback.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Iterable, Optional

import openpyxl

OUTPUT_FILENAME = "PRJISEPI.csv"

OUTPUT_HEADER = [
    "Co Code",
    "Batch ID",
    "File #",
    "Tax Frequency",
    "Temp Dept",
    "Temp Rate",
    "Reg Hours",
    "O/T Hours",
    "Hours 3 Code",
    "Hours 3 Amount",
    "Hours 3 Code",
    "Hours 3 Amount",
    "Hours 3 Code",
    "Hours 3 Amount",
    "Hours 3 Code",
    "Hours 3 Amount",
    "Hours 4 Code",
    "Hours 4 Amount",
    "Hours 4 Code",
    "Hours 4 Amount",
    "Hours 4 Code",
    "Hours 4 Amount",
    "Hours 4 Code",
    "Hours 4 Amount",
    "Reg Earnings",
]

CO_CODE = "JIS"
DE_STATE = "DE"
OT_THRESHOLD = 40.0

REQUIRED_HEADERS = {"Resource", "Work Performed", "Amount", "Amount Owed"}

LogFn = Callable[[str], None]


class ConversionError(Exception):
    """Raised for user-friendly, expected failures (caught by the GUI)."""


@dataclass
class WorkRow:
    employee_name: str
    employee_number: str
    work_performed: str
    date_worked: date
    title: str
    lot_address: str
    amount: float
    amount_owed: float
    state: str
    is_de: bool
    is_piecework: bool
    source_index: int


@dataclass
class EmployeeBuckets:
    name: str
    number: str
    hourly_non_de_reg: float = 0.0
    hourly_non_de_ot: float = 0.0
    hourly_de_reg: float = 0.0
    hourly_de_ot: float = 0.0
    piecework_non_de: float = 0.0
    piecework_de: float = 0.0


@dataclass
class ConversionResult:
    pay_period_start: date
    pay_period_end: date
    batch_id: str
    work_rows: list[WorkRow] = field(default_factory=list)
    employees: list[EmployeeBuckets] = field(default_factory=list)
    output_rows: list[list[str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_total_amount_owed: float = 0.0


# -----------------------------------------------------------------------------
# Header / metadata parsing
# -----------------------------------------------------------------------------

_PAY_PERIOD_RE = re.compile(
    r"Date\s+Worked\s+equals\s+Custom\s*\(\s*(\d{1,2}/\d{1,2}/\d{4})\s*to\s*(\d{1,2}/\d{1,2}/\d{4})\s*\)",
    re.IGNORECASE,
)


def compute_batch_id(pay_end: date) -> str:
    """Derive the payroll Batch ID from the pay period end date.

    Why: payroll's internal week number runs one ahead of the ISO week
    (their week 20 = ISO week 19, etc.). Batch ID is derived from the
    end date in the source file — never from today() — so re-running
    the tool against the same xlsx always yields the same Batch ID.
    """
    iso_year, iso_week, _ = pay_end.isocalendar()
    week = iso_week + 1
    # ISO years have 52 or 53 weeks; Dec 28 is always in the last ISO
    # week of the year, so we use it to detect the upper bound.
    weeks_in_year = date(iso_year, 12, 28).isocalendar()[1]
    if week > weeks_in_year:
        week = 1
    return f"{week:02d}"


def parse_pay_period(rows: list[tuple]) -> tuple[date, date]:
    for row in rows:
        for cell in row:
            if isinstance(cell, str):
                m = _PAY_PERIOD_RE.search(cell)
                if m:
                    start = datetime.strptime(m.group(1), "%m/%d/%Y").date()
                    end = datetime.strptime(m.group(2), "%m/%d/%Y").date()
                    return start, end
    raise ConversionError(
        "Couldn't find the pay period dates in the file. "
        "Re-export from Salesforce and try again."
    )


def find_header_row(rows: list[tuple]) -> tuple[int, dict[str, int]]:
    """Locate the header row (contains 'Resource' as a non-empty cell) and
    return its 0-based index along with a mapping of header name -> column index.
    """
    for i, row in enumerate(rows):
        if any(isinstance(c, str) and c.strip().startswith("Resource") for c in row):
            mapping: dict[str, int] = {}
            for j, cell in enumerate(row):
                if isinstance(cell, str):
                    name = cell.strip().rstrip("↑↓").strip()
                    if name:
                        mapping[name] = j
            # Normalize: 'Resource ↑' -> 'Resource'
            for key in list(mapping):
                base = key.split()[0] if key else key
                if base == "Resource" and key != "Resource":
                    mapping["Resource"] = mapping.pop(key)
            missing = REQUIRED_HEADERS - set(mapping)
            if missing:
                raise ConversionError(
                    "This file is missing the columns we need "
                    "(Resource, Work Performed, Amount, Amount Owed). "
                    "Was this exported from Salesforce normally?"
                )
            return i, mapping
    raise ConversionError(
        "This file is missing the columns we need "
        "(Resource, Work Performed, Amount, Amount Owed). "
        "Was this exported from Salesforce normally?"
    )


# -----------------------------------------------------------------------------
# Per-row helpers
# -----------------------------------------------------------------------------

_EMPLOYEE_RE = re.compile(r"^(.*)-\s*(\d{3,6})\s*$")
_STATE_RE = re.compile(r"^[A-Z]{2}$")


def parse_employee_resource(value: str) -> Optional[tuple[str, str]]:
    """`Lastname, Firstname - 0563` -> ('Lastname, Firstname', '0563'). None
    if it doesn't look like an employee row (e.g. Subtotal/Total).
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.lower() in {"subtotal", "total"}:
        return None
    m = _EMPLOYEE_RE.match(text)
    if not m:
        return None
    name = m.group(1).strip().rstrip("-").strip()
    number = m.group(2).strip()
    return name, number


_COUNTRY_TOKENS = {"US", "USA", "UK"}


def extract_state(address: str) -> Optional[str]:
    """Pull a 2-letter uppercase state code out of a comma-separated address.
    The Salesforce address format ends with the country (`US`), so we ignore
    country-code tokens. Returns None if no candidate is found.
    """
    if not isinstance(address, str):
        return None
    for raw in address.split(","):
        token = raw.strip()
        if _STATE_RE.match(token) and token not in _COUNTRY_TOKENS:
            return token
    return None


def _fmt_date(d: date) -> str:
    """`date(2026, 5, 5)` -> `'5/5/2026'`. Cross-platform alternative to
    `strftime('%-m/%-d/%Y')` (which is POSIX-only).
    """
    return f"{d.month}/{d.day}/{d.year}"


def coerce_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        for fmt in ("%m/%d/%Y", "%-m/%-d/%Y", "%m/%d/%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        try:
            # Last resort: parse loose M/D/YYYY without leading zeros
            parts = text.split("/")
            if len(parts) == 3:
                m, d, y = (int(p) for p in parts)
                return date(y if y > 99 else 2000 + y, m, d)
        except (ValueError, TypeError):
            pass
    return None


def coerce_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.replace(",", "").replace("$", "").strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def is_subtotal_row(row: tuple, header_map: dict[str, int]) -> bool:
    """A subtotal row has 'Subtotal' or 'Total' in the Resource column or has
    the literal 'Sum' in the column to the right of Resource (col C in the
    sample). Either signal is enough.
    """
    resource_col = header_map["Resource"]
    cell = row[resource_col] if resource_col < len(row) else None
    if isinstance(cell, str) and cell.strip().lower() in {"subtotal", "total"}:
        return True
    next_col = resource_col + 1
    next_cell = row[next_col] if next_col < len(row) else None
    if isinstance(next_cell, str) and next_cell.strip().lower() == "sum":
        return True
    return False


# -----------------------------------------------------------------------------
# Main pipeline
# -----------------------------------------------------------------------------

def convert_workbook(path: str, log: Optional[LogFn] = None) -> ConversionResult:
    log = log or (lambda _msg: None)
    rows = _read_workbook(path)

    pay_start, pay_end = parse_pay_period(rows)
    batch_id = compute_batch_id(pay_end)
    log(f"Pay period: {_fmt_date(pay_start)} → {_fmt_date(pay_end)}")
    log(f"Batch ID: {batch_id}")
    log("")
    log("Parsing rows...")

    header_idx, header_map = find_header_row(rows)
    data_rows = rows[header_idx + 1:]

    work_rows, warnings, source_total_owed = _parse_work_rows(
        data_rows, header_map
    )
    if not work_rows:
        raise ConversionError(
            "This file has no work entries. Nothing to convert."
        )

    log(f"Parsing rows... done. {len(work_rows)} work entries processed.")
    for w in warnings:
        log(w)

    # Group by employee while preserving first-seen order.
    by_emp: dict[str, list[WorkRow]] = {}
    emp_meta: dict[str, str] = {}
    for r in work_rows:
        by_emp.setdefault(r.employee_number, []).append(r)
        emp_meta[r.employee_number] = r.employee_name

    de_employees = 0
    employees: list[EmployeeBuckets] = []
    for number, rs in by_emp.items():
        bucket = _aggregate_employee(emp_meta[number], number, rs)
        if bucket.hourly_de_reg or bucket.hourly_de_ot or bucket.piecework_de:
            de_employees += 1
        employees.append(bucket)

    log(f"Found {len(employees)} employees ({de_employees} with DE work).")

    output_rows = build_output_rows(employees, batch_id)
    output_rows.sort(key=_output_sort_key)

    total_reg = sum(b.hourly_non_de_reg + b.hourly_de_reg for b in employees)
    total_ot = sum(b.hourly_non_de_ot + b.hourly_de_ot for b in employees)
    total_pw = sum(b.piecework_non_de + b.piecework_de for b in employees)
    log("")
    log(f"Total hours: {total_reg:,.2f} regular, {total_ot:,.2f} OT")
    log(f"Total piecework: ${total_pw:,.2f}")
    log(f"Total amount owed (source): ${source_total_owed:,.2f}")
    log("")
    log("Click Save to choose where to save PRJISEPI.csv.")

    return ConversionResult(
        pay_period_start=pay_start,
        pay_period_end=pay_end,
        batch_id=batch_id,
        work_rows=work_rows,
        employees=employees,
        output_rows=output_rows,
        warnings=warnings,
        source_total_amount_owed=source_total_owed,
    )


def _read_workbook(path: str) -> list[tuple]:
    if not path.lower().endswith(".xlsx"):
        raise ConversionError(
            "That doesn't look like a Salesforce labor report. "
            "Please drop the .xlsx file from Salesforce."
        )
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception:
        raise ConversionError(
            "That doesn't look like a Salesforce labor report. "
            "Please drop the .xlsx file from Salesforce."
        )
    ws = wb.active
    return [tuple(row) for row in ws.iter_rows(values_only=True)]


def _parse_work_rows(
    data_rows: list[tuple],
    header_map: dict[str, int],
) -> tuple[list[WorkRow], list[str], float]:
    work_rows: list[WorkRow] = []
    warnings: list[str] = []
    current_name: Optional[str] = None
    current_number: Optional[str] = None

    res_col = header_map["Resource"]
    wp_col = header_map["Work Performed"]
    date_col = header_map.get("Date Worked")
    title_col = header_map.get("Title")
    addr_col = header_map.get("Lot Address")
    amt_col = header_map["Amount"]
    owed_col = header_map["Amount Owed"]

    source_total_owed = 0.0

    for idx, row in enumerate(data_rows):
        if not row or all(c is None or c == "" for c in row):
            continue

        if is_subtotal_row(row, header_map):
            if current_name and current_name.lower() == "total":
                # 'Total' acts like an employee marker we want to ignore.
                pass
            continue

        # Try to read employee marker from Resource column.
        resource_cell = row[res_col] if res_col < len(row) else None
        emp = parse_employee_resource(resource_cell) if isinstance(resource_cell, str) else None
        if emp is not None:
            current_name, current_number = emp

        amt = coerce_float(row[amt_col]) if amt_col < len(row) else None
        owed = coerce_float(row[owed_col]) if owed_col < len(row) else None
        wp = row[wp_col] if wp_col < len(row) else None

        # Skip rows that don't look like data (no work performed or no amount).
        if amt is None or owed is None or wp is None or wp == "":
            continue
        if current_number is None or current_name is None:
            continue

        date_val = coerce_date(row[date_col]) if date_col is not None and date_col < len(row) else None
        title = row[title_col] if title_col is not None and title_col < len(row) else ""
        title = title if isinstance(title, str) else ""
        addr = row[addr_col] if addr_col is not None and addr_col < len(row) else ""
        addr = addr if isinstance(addr, str) else ""

        state = extract_state(addr)
        if state is None:
            warnings.append(
                f"Note: row for {current_name} has an unusual address "
                f"({addr!r}); treated as non-DE."
            )
            is_de = False
            state = ""
        else:
            is_de = state == DE_STATE

        is_piecework = _floats_equal(amt, owed)

        work_rows.append(
            WorkRow(
                employee_name=current_name,
                employee_number=current_number,
                work_performed=str(wp),
                date_worked=date_val or date.min,
                title=title,
                lot_address=addr,
                amount=amt,
                amount_owed=owed,
                state=state,
                is_de=is_de,
                is_piecework=is_piecework,
                source_index=idx,
            )
        )
        source_total_owed += owed

    return work_rows, warnings, source_total_owed


def _floats_equal(a: float, b: float) -> bool:
    return abs(a - b) < 1e-9


# -----------------------------------------------------------------------------
# OT + aggregation
# -----------------------------------------------------------------------------

def _aggregate_employee(
    name: str, number: str, rows: list[WorkRow]
) -> EmployeeBuckets:
    bucket = EmployeeBuckets(name=name, number=number)

    # Piecework: simple sum, split DE / non-DE.
    for r in rows:
        if r.is_piecework:
            if r.is_de:
                bucket.piecework_de += r.amount
            else:
                bucket.piecework_non_de += r.amount

    # Hourly: chronological accumulation, respect 40hr OT threshold.
    hourly = [r for r in rows if not r.is_piecework]
    hourly.sort(key=lambda r: (r.date_worked, r.source_index))

    running = 0.0
    for r in hourly:
        hours = r.amount
        if hours <= 0:
            continue
        if running >= OT_THRESHOLD:
            reg = 0.0
            ot = hours
        elif running + hours <= OT_THRESHOLD:
            reg = hours
            ot = 0.0
        else:
            reg = OT_THRESHOLD - running
            ot = hours - reg
        running += hours
        if r.is_de:
            bucket.hourly_de_reg += reg
            bucket.hourly_de_ot += ot
        else:
            bucket.hourly_non_de_reg += reg
            bucket.hourly_non_de_ot += ot

    return bucket


# -----------------------------------------------------------------------------
# Output row generation
# -----------------------------------------------------------------------------

def _round2(x: float) -> float:
    return round(x + 1e-9, 2) if x else 0.0


def _fmt_num(x: float) -> str:
    rounded = _round2(x)
    if rounded == 0:
        return ""
    return f"{rounded:.2f}"


def _file_number(emp_number: str) -> str:
    digits = emp_number.lstrip("0") or "0"
    return f"{int(digits):06d}"


def build_output_rows(
    employees: Iterable[EmployeeBuckets], batch_id: str
) -> list[list[str]]:
    out: list[list[str]] = []
    for emp in employees:
        file_no = _file_number(emp.number)

        non_de_reg = emp.hourly_non_de_reg
        non_de_ot = emp.hourly_non_de_ot
        non_de_pw = emp.piecework_non_de
        de_reg = emp.hourly_de_reg
        de_ot = emp.hourly_de_ot
        de_pw = emp.piecework_de

        has_non_de = (non_de_reg or non_de_ot or non_de_pw)
        has_de = (de_reg or de_ot or de_pw)

        if not has_non_de and not has_de:
            continue

        if has_non_de:
            out.append(_make_row(batch_id, file_no, non_de_reg, non_de_ot, non_de_pw))
        if has_de:
            out.append(_make_row(batch_id, file_no, de_reg, de_ot, de_pw))

    return out


def _make_row(
    batch_id: str,
    file_no: str,
    reg_hours: float,
    ot_hours: float,
    reg_earnings: float,
) -> list[str]:
    row = [""] * len(OUTPUT_HEADER)
    row[0] = CO_CODE
    row[1] = batch_id
    row[2] = file_no
    # Tax Frequency, Temp Dept, Temp Rate left empty
    row[6] = _fmt_num(reg_hours)
    row[7] = _fmt_num(ot_hours)
    # Hours 3 / Hours 4 columns empty
    row[24] = _fmt_num(reg_earnings)
    return row


def _output_sort_key(row: list[str]) -> tuple[int, int]:
    """Sort by File # ascending; non-DE before DE for the same File # is
    already preserved because we generate them in that order, but we keep
    a stable tiebreaker by appending the row index later if needed.
    """
    file_no = row[2]
    digits = re.sub(r"\D", "", file_no) or "0"
    return (int(digits), 0)


# -----------------------------------------------------------------------------
# Save
# -----------------------------------------------------------------------------

def write_output_csv(path: str, output_rows: list[list[str]]) -> None:
    """Write the CSV to `path`. Caller is responsible for forcing
    PRJISEPI.csv as the basename. Output has no trailing newline — ADP
    treats a final empty line as an extra blank record and rejects it.
    """
    import io
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(OUTPUT_HEADER)
    for row in output_rows:
        writer.writerow(row)
    content = buf.getvalue().rstrip("\r\n")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
