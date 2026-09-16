"""Deterministic AGY stream-json fixture for CI; never calls a provider."""

from __future__ import annotations

import argparse
import json
import os
import sys


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--model", default="fixture-model")
    parser.add_argument("--effort", default=None)
    parser.add_argument("--input-format", default=None)
    parser.add_argument("--output-format", default=None)
    parser.add_argument("--print-timeout", default=None)
    parser.add_argument("--mode", default=None)
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--add-dir", default=None)
    parser.add_argument("--json-schema", default=None)
    return parser.parse_known_args()[0]


def main() -> int:
    if "--version" in sys.argv:
        print("agy 0.0.0-fixture")
        return 0
    if "--help" in sys.argv:
        print(
            "usage: fake-agy [--model MODEL] [--effort EFFORT] "
            "[--input-format stream-json] [--output-format stream-json] "
            "[--sandbox] [--mode MODE] [--print-timeout TIMEOUT]"
        )
        return 0

    args = _arguments()
    permission_mode = args.mode or ("sandbox" if args.sandbox else "request-review")
    print(
        json.dumps(
            {
                "event": "init",
                "init": {
                    "cwd": os.getcwd(),
                    "model": args.model,
                    "effort": args.effort,
                    "permission_mode": permission_mode,
                    "conversation_id": "fixture-conversation",
                },
            }
        ),
        flush=True,
    )
    for line in sys.stdin:
        message = json.loads(line)
        prompt = message.get("message", {}).get("content", "")
        print(
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "status": "SUCCESS",
                        "response": f"fixture:{len(str(prompt))}",
                        "conversation_id": "fixture-conversation",
                        "structured_output": {"fixture": True},
                    },
                }
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
