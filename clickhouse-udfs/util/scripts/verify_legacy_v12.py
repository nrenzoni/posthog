#!/usr/bin/env python3
"""Run on Linux against a native v12 rebuild; optionally compare the original executable.

This deliberately preserves historical quirks. It must reject the newer util/cmd
implementation, even when that implementation removes the requested keys correctly.
No third-party dependencies or database services are needed.
"""

import json
import argparse
import subprocess
from pathlib import Path

# The empty-filter dotted-key expansion and array-filter behavior distinguish
# the source embedded in v12 from the newer, incompatible implementation.
CASES = (
    ("expand_dotted_key", "[]", b'{"a.b":1}\n', [{"a": {"b": 1}}]),
    (
        "filter_nested_and_flat_paths",
        "['a.b']",
        b'{"a.b":1,"keep":2}\n{"a":{"b":3,"c":4}}\n',
        [{"a": {}, "keep": 2}, {"a": {"c": 4}}],
    ),
    (
        "legacy_array_filter_is_not_propagated",
        "['events.a']",
        b'{"events":[{"a":1,"b":2}]}\n',
        [{"events": [{"a": 1, "b": 2}]}],
    ),
    (
        "multiple_rows_reuse_pools_without_leaking_values",
        "['secret']",
        b'{"secret":1,"value":"first"}\n{"value":null}\n{}\n{"value":[1,true,"last"]}\n',
        [{"value": "first"}, {"value": None}, {}, {"value": [1, True, "last"]}],
    ),
    ("final_row_without_newline", "[]", b'{"a.b":2}', [{"a": {"b": 2}}]),
)


def invoke(binary: Path, keys: str, payload: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(binary.resolve()), keys], input=payload, capture_output=True, timeout=10, check=False)


def verify(binary: Path, baseline: Path | None) -> None:
    for name, keys, payload, expected in CASES:
        result = invoke(binary, keys, payload)
        if result.returncode != 0 or result.stderr:
            raise AssertionError(f"{name}: executable failed: {result.returncode}, {result.stderr!r}")
        actual = [json.loads(line) for line in result.stdout.splitlines()]
        if actual != expected:
            raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")
        if result.stdout.endswith(b"\n") != payload.endswith(b"\n"):
            raise AssertionError(f"{name}: changed final newline behavior")
        if baseline is not None:
            original = invoke(baseline, keys, payload)
            if (result.returncode, result.stdout, result.stderr) != (
                original.returncode,
                original.stdout,
                original.stderr,
            ):
                raise AssertionError(f"{name}: rebuilt executable differs from original v12")

    invalid = invoke(binary, "[]", b"{not-json}\n")
    if invalid.returncode == 0 or b"line processing error:" not in invalid.stderr or invalid.stdout:
        raise AssertionError("malformed JSON did not fail closed with the legacy diagnostic")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    verify(args.binary, args.baseline)
    print(f"v12 consumer regressions passed: {args.binary}")  # noqa: T201 -- CLI verification result
