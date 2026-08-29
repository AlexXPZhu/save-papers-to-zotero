#!/usr/bin/env python3
"""Render and verify AI-generated Zotero summary notes."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

import zotero_connector_import as single


SUMMARY_MARKER = "AI 论文导读"
SUMMARY_HEADINGS = (
    "概述",
    "背景与动机",
    "解决的核心问题",
    "方法与架构",
    "成果与评估",
    "重点阅读",
)
SUMMARY_FIELDS = ("summary", "background", "problems", "methodology", "results")
EXPECTED_KEYS = {*SUMMARY_FIELDS, "focus"}
ITEM_KEY_PATTERN = re.compile(r"^[A-Za-z0-9]{8,32}$")


class SummaryFailure(Exception):
    def __init__(self, status: str, message: str, exit_code: int = 2):
        super().__init__(message)
        self.status = status
        self.message = message
        self.exit_code = exit_code


def load_summary(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SummaryFailure("invalid_summary_json", f"Cannot read summary JSON: {error}") from error


def normalized_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SummaryFailure("invalid_summary_json", f"Summary field {field!r} must be a non-empty string")
    return " ".join(value.split())


def validate_summary(value: object) -> dict:
    if not isinstance(value, dict):
        raise SummaryFailure("invalid_summary_json", "Summary JSON must contain one object")
    keys = set(value)
    if keys != EXPECTED_KEYS:
        missing = sorted(EXPECTED_KEYS - keys)
        unexpected = sorted(keys - EXPECTED_KEYS)
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        raise SummaryFailure(
            "invalid_summary_json",
            "Summary JSON fields do not match the schema (" + "; ".join(details) + ")",
        )

    validated = {field: normalized_text(value[field], field) for field in SUMMARY_FIELDS}
    focus = value["focus"]
    if not isinstance(focus, list) or not 2 <= len(focus) <= 4:
        raise SummaryFailure("invalid_summary_json", "Summary field 'focus' must contain 2 to 4 entries")

    validated_focus = []
    for index, entry in enumerate(focus):
        field = f"focus[{index}]"
        if not isinstance(entry, dict) or set(entry) != {"location", "reason"}:
            raise SummaryFailure(
                "invalid_summary_json",
                f"Summary field {field!r} must contain exactly location and reason",
            )
        validated_focus.append(
            {
                "location": normalized_text(entry["location"], field + ".location"),
                "reason": normalized_text(entry["reason"], field + ".reason"),
            }
        )
    validated["focus"] = validated_focus
    return validated


def escaped(value: str) -> str:
    return html.escape(value, quote=True)


def render_summary_note(summary: dict) -> str:
    summary = validate_summary(summary)
    sections = (
        ("概述", summary["summary"]),
        ("背景与动机", summary["background"]),
        ("解决的核心问题", summary["problems"]),
        ("方法与架构", summary["methodology"]),
        ("成果与评估", summary["results"]),
    )
    parts = [f"<h1>{SUMMARY_MARKER}</h1>"]
    for heading, value in sections:
        parts.append(f"<h2>{heading}</h2><p>{escaped(value)}</p>")
    parts.append("<h2>重点阅读</h2><ul>")
    for entry in summary["focus"]:
        parts.append(
            f"<li><strong>{escaped(entry['location'])}</strong>：{escaped(entry['reason'])}</li>"
        )
    parts.append("</ul>")
    parts.append("<p><strong>说明：</strong>AI 生成，基于全文，请结合原文核验。</p>")
    return "".join(parts)


def write_note(summary_path: Path, output_path: Path) -> dict:
    summary = validate_summary(load_summary(summary_path))
    note = render_summary_note(summary)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(note, encoding="utf-8")
    except OSError as error:
        raise SummaryFailure("summary_note_write_failed", f"Cannot write summary note: {error}") from error
    text_length = sum(len(summary[field]) for field in SUMMARY_FIELDS)
    text_length += sum(
        len(entry["location"]) + len(entry["reason"])
        for entry in summary["focus"]
    )
    return {
        "status": "summary_rendered",
        "summary_json": str(summary_path.resolve()),
        "note_file": str(output_path.resolve()),
        "focus_count": len(summary["focus"]),
        "text_characters": text_length,
    }


def note_values(base_url: str, item_key: str) -> list[str]:
    encoded_key = urllib.parse.quote(item_key, safe="")
    children = single.api_get(
        base_url,
        f"/api/users/0/items/{encoded_key}/children?itemType=note&format=json&include=data&limit=100",
    )
    if not isinstance(children, list):
        raise SummaryFailure("invalid_api_response", "Zotero local API returned a non-list child-note response")
    values = []
    for child in children:
        if not isinstance(child, dict):
            continue
        data = single.data_object(child)
        if data.get("itemType") == "note" and isinstance(data.get("note"), str):
            values.append(data["note"])
    return values


def note_has_summary(note: str) -> bool:
    return f"<h1>{SUMMARY_MARKER}</h1>" in note and all(
        f"<h2>{heading}</h2>" in note for heading in SUMMARY_HEADINGS
    )


def verify_summary_note(base_url: str, item_key: str, timeout: float) -> dict:
    if not ITEM_KEY_PATTERN.fullmatch(item_key):
        raise SummaryFailure("invalid_item_key", "Item key must contain 8 to 32 ASCII letters or digits")
    if timeout <= 0:
        raise SummaryFailure("invalid_timeout", "Verification timeout must be positive")

    deadline = time.monotonic() + timeout
    notes: list[str] = []
    while True:
        notes = note_values(base_url, item_key)
        if any(note_has_summary(note) for note in notes):
            return {
                "status": "note_verified",
                "item_key": item_key,
                "note_count": len(notes),
                "marker": SUMMARY_MARKER,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "status": "note_verification_failed",
                "item_key": item_key,
                "note_count": len(notes),
                "marker": SUMMARY_MARKER,
                "message": "No child note with the required summary marker and headings was found",
            }
        time.sleep(min(0.5, remaining))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    render = subparsers.add_parser("render", help="Validate summary JSON and render safe Zotero note HTML")
    render.add_argument("--summary-json", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser("verify", help="Verify a generated summary child note by item key")
    verify.add_argument("--item-key", required=True)
    verify.add_argument("--timeout", type=float, default=15)
    verify.add_argument("--base-url", default=single.DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "render":
            result = write_note(args.summary_json, args.output)
        else:
            result = verify_summary_note(args.base_url, args.item_key, args.timeout)
    except (SummaryFailure, single.ImportFailure) as error:
        result = {"status": error.status, "message": error.message}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return getattr(error, "exit_code", 2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] != "note_verification_failed" else 3


if __name__ == "__main__":
    single.configure_utf8_stdio()
    sys.exit(main())
