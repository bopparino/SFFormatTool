"""Tests for converter.py.

Covers classification, state extraction, file # padding, ISO week,
chronological OT, aggregation, row generation, and end-to-end pipeline.
"""

from __future__ import annotations

import csv
import os
from datetime import date

import pytest

import converter as C
from converter import (
    EmployeeBuckets,
    WorkRow,
    _aggregate_employee,
    _file_number,
    build_output_rows,
    convert_workbook,
    extract_state,
    parse_employee_resource,
    write_output_csv,
)


# -----------------------------------------------------------------------------
# Classification (Piecework vs Hourly)
# -----------------------------------------------------------------------------

def _row(
    name="Smith, Bob",
    number="1234",
    work="X",
    d=date(2026, 5, 1),
    addr="123 Main St, Lewes, DE, 19958, US",
    amount=1.0,
    owed=1.0,
    idx=0,
):
    state = extract_state(addr) or ""
    return WorkRow(
        employee_name=name,
        employee_number=number,
        work_performed=work,
        date_worked=d,
        title="",
        lot_address=addr,
        amount=amount,
        amount_owed=owed,
        state=state,
        is_de=(state == "DE"),
        is_piecework=(amount == owed),
        source_index=idx,
    )


class TestClassification:
    def test_piecework_when_amount_equals_owed(self):
        r = _row(work="RD1 - Run Drain Zone 1", amount=50.0, owed=50.0)
        assert r.is_piecework is True

    def test_hourly_when_amount_differs_from_owed(self):
        r = _row(work="SMH - Hourly work", amount=2.5, owed=45.0)
        assert r.is_piecework is False

    def test_piecework_marker_no_marker_classified_by_amount_match(self):
        # No (P) / (H) marker; classified purely by amount == owed.
        r_pw = _row(work="RD1 - Run Drain Zone 1", amount=10.0, owed=10.0)
        r_hr = _row(work="RD1 - Run Drain Zone 1", amount=10.0, owed=20.0)
        assert r_pw.is_piecework is True
        assert r_hr.is_piecework is False


# -----------------------------------------------------------------------------
# State extraction
# -----------------------------------------------------------------------------

class TestStateExtraction:
    def test_normal_address(self):
        assert extract_state("123 Main St, Lewes, DE, 19958, US") == "DE"

    def test_all_caps(self):
        assert extract_state("PROJECT LOT, CLINTON, MD, 20735, US") == "MD"

    def test_extra_whitespace(self):
        assert extract_state("123 Main,  Town ,  VA  , 22112,  US") == "VA"

    def test_missing_state_returns_none(self):
        assert extract_state("Just a street with no comma") is None

    def test_address_with_no_two_letter_token(self):
        assert extract_state("123 Main, Some Long City Name, 19999, US") is None

    def test_dc_recognized_as_state(self):
        assert extract_state("100 K St, Washington, DC, 20001, US") == "DC"


# -----------------------------------------------------------------------------
# Employee resource parsing
# -----------------------------------------------------------------------------

class TestEmployeeParsing:
    def test_normal(self):
        assert parse_employee_resource("Aguilar, Luis - 3100") == (
            "Aguilar, Luis",
            "3100",
        )

    def test_leading_zero_preserved_in_source(self):
        # parse keeps the digits as written; padding happens at file # build time.
        assert parse_employee_resource("Bennett, James - 0563") == (
            "Bennett, James",
            "0563",
        )

    def test_subtotal_is_not_employee(self):
        assert parse_employee_resource("Subtotal") is None

    def test_total_is_not_employee(self):
        assert parse_employee_resource("Total") is None

    def test_empty_returns_none(self):
        assert parse_employee_resource("") is None
        assert parse_employee_resource(None) is None


# -----------------------------------------------------------------------------
# File # padding
# -----------------------------------------------------------------------------

class TestFileNumberPadding:
    def test_four_digit(self):
        assert _file_number("3100") == "JIS003100"

    def test_three_digit(self):
        assert _file_number("563") == "JIS000563"

    def test_leading_zero_input(self):
        assert _file_number("0563") == "JIS000563"

    def test_five_digit(self):
        assert _file_number("12345") == "JIS012345"


# -----------------------------------------------------------------------------
# ISO week (used for Batch ID)
# -----------------------------------------------------------------------------

class TestISOWeek:
    def test_pay_period_end_5_5_2026_is_week_19(self):
        end = date(2026, 5, 5)
        assert f"{end.isocalendar()[1]:02d}" == "19"

    def test_zero_padding_for_single_digit_weeks(self):
        # Jan 6 2026 is in ISO week 02
        assert f"{date(2026, 1, 6).isocalendar()[1]:02d}" == "02"


# -----------------------------------------------------------------------------
# OT chronological accumulation
# -----------------------------------------------------------------------------

class TestOTAccumulation:
    def test_under_40_no_ot(self):
        rows = [
            _row(amount=8.0, owed=160.0, d=date(2026, 5, 1), idx=0),
            _row(amount=8.0, owed=160.0, d=date(2026, 5, 2), idx=1),
            _row(amount=8.0, owed=160.0, d=date(2026, 5, 3), idx=2),
            _row(amount=8.0, owed=160.0, d=date(2026, 5, 4), idx=3),
        ]
        b = _aggregate_employee("X", "1", rows)
        assert b.hourly_de_reg == 32.0
        assert b.hourly_de_ot == 0.0

    def test_50_hours_yields_40_reg_10_ot(self):
        rows = [
            _row(amount=10.0, owed=200.0, d=date(2026, 5, 1), idx=0),
            _row(amount=10.0, owed=200.0, d=date(2026, 5, 2), idx=1),
            _row(amount=10.0, owed=200.0, d=date(2026, 5, 3), idx=2),
            _row(amount=10.0, owed=200.0, d=date(2026, 5, 4), idx=3),
            _row(amount=10.0, owed=200.0, d=date(2026, 5, 5), idx=4),
        ]
        b = _aggregate_employee("X", "1", rows)
        assert b.hourly_de_reg == 40.0
        assert b.hourly_de_ot == 10.0

    def test_row_straddling_40_is_split(self):
        # 35 + 8 = 43; second row should split: 5 reg, 3 OT.
        rows = [
            _row(amount=35.0, owed=700.0, d=date(2026, 5, 1), idx=0),
            _row(amount=8.0, owed=160.0, d=date(2026, 5, 2), idx=1),
        ]
        b = _aggregate_employee("X", "1", rows)
        assert b.hourly_de_reg == pytest.approx(40.0)
        assert b.hourly_de_ot == pytest.approx(3.0)

    def test_de_and_non_de_ot_buckets_preserved(self):
        # Non-DE first 35h, then DE 8h straddles -> 5h DE reg, 3h DE OT.
        rows = [
            _row(amount=35.0, addr="1 St, Town, VA, 22112, US",
                 d=date(2026, 5, 1), idx=0),
            _row(amount=8.0, addr="1 St, Lewes, DE, 19958, US",
                 d=date(2026, 5, 2), idx=1),
        ]
        b = _aggregate_employee("X", "1", rows)
        assert b.hourly_non_de_reg == pytest.approx(35.0)
        assert b.hourly_non_de_ot == pytest.approx(0.0)
        assert b.hourly_de_reg == pytest.approx(5.0)
        assert b.hourly_de_ot == pytest.approx(3.0)

    def test_chronological_order_used_not_source_order(self):
        # Source order: 35h on 5/2, 10h on 5/1. Sorted by date, 5/1 comes
        # first; 10h all reg, then 30 reg + 5 OT for 5/2 -> 40 reg, 5 OT.
        rows = [
            _row(amount=35.0, d=date(2026, 5, 2), idx=0),
            _row(amount=10.0, d=date(2026, 5, 1), idx=1),
        ]
        b = _aggregate_employee("X", "1", rows)
        assert b.hourly_de_reg == pytest.approx(40.0)
        assert b.hourly_de_ot == pytest.approx(5.0)

    def test_piecework_does_not_count_toward_ot(self):
        # 30 hourly + 30 piecework dollars + 15 hourly. No OT triggered
        # because hourly total is only 45 — wait, that's > 40. Let's use
        # 30 hourly + piecework + 5 hourly = 35 hourly, no OT.
        rows = [
            _row(amount=30.0, owed=600.0, d=date(2026, 5, 1), idx=0),
            _row(amount=100.0, owed=100.0, d=date(2026, 5, 2), idx=1),  # PW
            _row(amount=5.0, owed=100.0, d=date(2026, 5, 3), idx=2),
        ]
        b = _aggregate_employee("X", "1", rows)
        assert b.hourly_de_reg == pytest.approx(35.0)
        assert b.hourly_de_ot == pytest.approx(0.0)
        assert b.piecework_de == pytest.approx(100.0)


# -----------------------------------------------------------------------------
# Aggregation 4-bucket hourly + 2-bucket piecework
# -----------------------------------------------------------------------------

class TestAggregation:
    def test_buckets_split_by_state_and_kind(self):
        rows = [
            # Non-DE hourly 35h
            _row(amount=35.0, owed=700.0,
                 addr="1, Town, VA, 22112, US",
                 d=date(2026, 5, 1), idx=0),
            # DE hourly 10h (straddles 40 -> 5 reg, 5 OT DE)
            _row(amount=10.0, owed=200.0,
                 addr="1, Lewes, DE, 19958, US",
                 d=date(2026, 5, 2), idx=1),
            # Non-DE piecework $200
            _row(amount=200.0, owed=200.0,
                 addr="1, Town, MD, 20735, US",
                 d=date(2026, 5, 3), idx=2),
            # DE piecework $50
            _row(amount=50.0, owed=50.0,
                 addr="1, Lewes, DE, 19958, US",
                 d=date(2026, 5, 4), idx=3),
        ]
        b = _aggregate_employee("X", "1", rows)
        assert b.hourly_non_de_reg == pytest.approx(35.0)
        assert b.hourly_non_de_ot == pytest.approx(0.0)
        assert b.hourly_de_reg == pytest.approx(5.0)
        assert b.hourly_de_ot == pytest.approx(5.0)
        assert b.piecework_non_de == pytest.approx(200.0)
        assert b.piecework_de == pytest.approx(50.0)


# -----------------------------------------------------------------------------
# Row generation: 1 row vs 2 rows
# -----------------------------------------------------------------------------

class TestRowGeneration:
    def test_only_non_de_yields_one_row(self):
        b = EmployeeBuckets(name="X", number="100",
                            hourly_non_de_reg=8.0, hourly_non_de_ot=0.0)
        rows = build_output_rows([b], "19")
        assert len(rows) == 1
        assert rows[0][2] == "JIS000100"
        assert rows[0][6] == "8.00"
        assert rows[0][7] == ""

    def test_only_de_yields_one_row(self):
        b = EmployeeBuckets(name="X", number="100",
                            hourly_de_reg=8.0, hourly_de_ot=0.0)
        rows = build_output_rows([b], "19")
        assert len(rows) == 1

    def test_mixed_de_and_non_de_yields_two_rows_non_de_first(self):
        b = EmployeeBuckets(
            name="X",
            number="100",
            hourly_non_de_reg=20.0,
            hourly_de_reg=10.0,
        )
        rows = build_output_rows([b], "19")
        assert len(rows) == 2
        assert rows[0][6] == "20.00"  # non-DE first
        assert rows[1][6] == "10.00"  # DE second

    def test_mixed_hourly_and_piecework_in_one_row(self):
        b = EmployeeBuckets(name="X", number="100",
                            hourly_non_de_reg=8.0,
                            piecework_non_de=100.0)
        rows = build_output_rows([b], "19")
        assert len(rows) == 1
        assert rows[0][6] == "8.00"
        assert rows[0][24] == "100.00"

    def test_zero_buckets_emit_nothing(self):
        b = EmployeeBuckets(name="X", number="100")
        rows = build_output_rows([b], "19")
        assert rows == []

    def test_empty_string_for_zero_values(self):
        b = EmployeeBuckets(name="X", number="100", hourly_non_de_reg=8.0)
        rows = build_output_rows([b], "19")
        assert rows[0][7] == ""  # OT empty
        assert rows[0][24] == ""  # Reg Earnings empty


# -----------------------------------------------------------------------------
# End-to-end against bundled sample
# -----------------------------------------------------------------------------

SAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "samples",
)


@pytest.fixture
def sample_path():
    p = os.path.join(SAMPLES_DIR, "sample_input.xlsx")
    if not os.path.exists(p):
        pytest.skip(f"Sample not found at {p}")
    return p


class TestEndToEnd:
    def test_pipeline_parses_sample(self, sample_path):
        result = convert_workbook(sample_path)
        assert result.batch_id == "19"
        assert result.pay_period_start == date(2026, 4, 28)
        assert result.pay_period_end == date(2026, 5, 5)
        # Expect ~40 employees (the source file has 40)
        assert len(result.employees) >= 30
        # Source totals: per the source totals row
        assert result.source_total_amount_owed == pytest.approx(35232.36, abs=0.01)

    def test_anderson_2979_has_40_reg_and_1_ot(self, sample_path):
        result = convert_workbook(sample_path)
        anderson = next(e for e in result.employees if e.number == "2979")
        assert anderson.hourly_non_de_reg == pytest.approx(40.0)
        assert anderson.hourly_non_de_ot == pytest.approx(1.0)
        assert anderson.hourly_de_reg == 0.0
        assert anderson.hourly_de_ot == 0.0

    def test_aguilar_3100_all_de(self, sample_path):
        result = convert_workbook(sample_path)
        aguilar = next(e for e in result.employees if e.number == "3100")
        assert aguilar.hourly_de_reg == pytest.approx(16.0)
        assert aguilar.hourly_non_de_reg == 0.0

    def test_output_sorted_by_file_number(self, sample_path):
        result = convert_workbook(sample_path)
        prev = -1
        for r in result.output_rows:
            digits = int(r[2].replace("JIS", "").lstrip("0") or "0")
            assert digits >= prev
            prev = digits

    def test_csv_round_trip(self, sample_path, tmp_path):
        result = convert_workbook(sample_path)
        out = tmp_path / "PRJISEPI.csv"
        write_output_csv(str(out), result.output_rows)
        with open(out, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            assert header == C.OUTPUT_HEADER
            data = list(reader)
            assert len(data) == len(result.output_rows)
            # Every row has exactly 25 columns
            assert all(len(row) == 25 for row in data)
            # Co Code is JIS in every data row
            assert all(row[0] == "JIS" for row in data)
            # Batch ID is "19" in every data row
            assert all(row[1] == "19" for row in data)
