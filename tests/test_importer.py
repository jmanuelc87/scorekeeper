"""Tests for the conversation ``.xlsx`` parser (``scorekeeper.importer``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from scorekeeper.importer import parse_conversation, parse_conversation_json


def _write_xlsx(path: Path, header: list[str], rows: list[list[object]]) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def test_happy_path_alternating_messages(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path / "conv.xlsx",
        ["turn", "role", "content"],
        [
            [1, "user", "Hola, ¿cómo estás?"],
            [1, "model", "Bien, ¿en qué puedo ayudarte?"],
            [2, "user", "Necesito una reseña."],
            [2, "model", "Con gusto."],
        ],
    )

    result = parse_conversation(path)

    assert result == [
        {"turn": 1, "role": "user", "content": "Hola, ¿cómo estás?"},
        {"turn": 1, "role": "model", "content": "Bien, ¿en qué puedo ayudarte?"},
        {"turn": 2, "role": "user", "content": "Necesito una reseña."},
        {"turn": 2, "role": "model", "content": "Con gusto."},
    ]


def test_spanish_headers_and_role_aliases(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path / "conv_es.xlsx",
        ["turno", "rol", "contenido"],
        [
            [1, "usuario", "Pregunta uno"],
            [1, "asistente", "Respuesta uno"],
        ],
    )

    result = parse_conversation(path)

    assert result == [
        {"turn": 1, "role": "user", "content": "Pregunta uno"},
        {"turn": 1, "role": "model", "content": "Respuesta uno"},
    ]


def test_derived_turn_numbering_without_turn_column(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path / "no_turn.xlsx",
        ["role", "content"],
        [
            ["user", "P1"],
            ["model", "R1"],
            ["user", "P2"],
            ["model", "R2"],
            ["user", "P3"],
        ],
    )

    result = parse_conversation(path)

    assert [m["turn"] for m in result] == [1, 1, 2, 2, 3]


def test_explicit_turn_column_is_respected(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path / "explicit.xlsx",
        ["turn", "role", "content"],
        [
            [5, "user", "P"],
            [5, "model", "R"],
        ],
    )

    result = parse_conversation(path)

    assert [m["turn"] for m in result] == [5, 5]


def test_empty_rows_are_skipped(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path / "gaps.xlsx",
        ["role", "content"],
        [
            ["user", "P1"],
            [None, None],
            ["model", "R1"],
        ],
    )

    result = parse_conversation(path)

    assert [m["content"] for m in result] == ["P1", "R1"]


def test_empty_sheet_returns_empty_list(tmp_path: Path) -> None:
    path = tmp_path / "empty.xlsx"
    Workbook().save(path)

    assert parse_conversation(path) == []


def test_columns_override(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path / "custom.xlsx",
        ["Speaker", "Message"],
        [
            ["user", "Hola"],
            ["model", "Adiós"],
        ],
    )

    result = parse_conversation(
        path, columns={"role": "Speaker", "content": "Message"}
    )

    assert [m["role"] for m in result] == ["user", "model"]
    assert [m["content"] for m in result] == ["Hola", "Adiós"]


def test_retrieved_context_column_is_parsed(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path / "with_context.xlsx",
        ["turn", "role", "content", "contexto recuperado"],
        [
            [1, "user", "¿Política de devoluciones?", ""],
            [1, "model", "30 días con recibo.", "Devoluciones en 30 días. Requiere recibo."],
        ],
    )

    result = parse_conversation(path)

    assert result == [
        {"turn": 1, "role": "user", "content": "¿Política de devoluciones?", "retrieved_context": ""},
        {
            "turn": 1,
            "role": "model",
            "content": "30 días con recibo.",
            "retrieved_context": "Devoluciones en 30 días. Requiere recibo.",
        },
    ]


def test_retrieved_context_absent_omits_key(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path / "no_context.xlsx",
        ["role", "content"],
        [["user", "Hola"]],
    )

    result = parse_conversation(path)

    assert "retrieved_context" not in result[0]


def test_expected_output_column_is_parsed(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path / "with_expected.xlsx",
        ["turn", "role", "content", "respuesta esperada"],
        [
            [1, "user", "¿Política de devoluciones?", ""],
            [1, "model", "30 días.", "Se aceptan devoluciones dentro de 30 días con recibo."],
        ],
    )

    result = parse_conversation(path)

    assert result == [
        {"turn": 1, "role": "user", "content": "¿Política de devoluciones?", "expected_output": ""},
        {
            "turn": 1,
            "role": "model",
            "content": "30 días.",
            "expected_output": "Se aceptan devoluciones dentro de 30 días con recibo.",
        },
    ]


def test_retrieved_context_and_expected_output_together(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path / "both.xlsx",
        ["role", "content", "contexto recuperado", "expected output"],
        [["model", "R", "ctx", "ref"]],
    )

    result = parse_conversation(path)

    assert result[0]["retrieved_context"] == "ctx"
    assert result[0]["expected_output"] == "ref"


def test_expected_output_absent_omits_key(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path / "no_expected.xlsx",
        ["role", "content"],
        [["user", "Hola"]],
    )

    result = parse_conversation(path)

    assert "expected_output" not in result[0]


def test_missing_required_column_raises(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path / "bad.xlsx",
        ["turn", "content"],
        [[1, "Hola"]],
    )

    with pytest.raises(ValueError, match="role"):
        parse_conversation(path)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_conversation(tmp_path / "nope.xlsx")


def test_output_round_trips_through_json(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path / "json.xlsx",
        ["role", "content"],
        [["user", "Café con acentós"]],
    )

    payload = parse_conversation_json(path)

    assert json.loads(payload) == [{"turn": 1, "role": "user", "content": "Café con acentós"}]
    assert "Café" in payload  # ensure_ascii=False preserves accents.
