"""Command-line interface for the durable local AGY queue."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import QueueConfig
from .models import JobKind, JobRequest, JobStatus, normalize_job_kind
from .repository import RepositoryError
from .service import QueuePolicyError, QueueService, job_to_dict
from .worker import QueueWorker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agy-queue")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser("submit", help="persist a job and return its job_id")
    submit.add_argument("--prompt-file", required=True, help="prompt file, or - for stdin")
    submit.add_argument("--kind", default=JobKind.READ_ONLY.value)
    submit.add_argument("--repo", required=True, help="absolute git repository root")
    submit.add_argument("--model")
    submit.add_argument("--effort")
    submit.add_argument("--session-key")
    submit.add_argument("--scope-key")
    submit.add_argument("--max-attempts", type=int)
    submit.add_argument("--timeout-sec", type=int)
    submit.add_argument("--expected-output-schema-file")
    submit.add_argument("--authorization-ref")
    submit.add_argument("--verification-job-id")

    get = subparsers.add_parser("get", help="retrieve a job and its result")
    get.add_argument("job_id")

    listing = subparsers.add_parser("list", help="list jobs")
    listing.add_argument("--status", choices=[status.value for status in JobStatus])
    listing.add_argument("--kind")
    listing.add_argument("--limit", type=int, default=100)

    watch = subparsers.add_parser("watch", help="stream meaningful status changes")
    watch.add_argument("job_id")
    watch.add_argument("--poll-sec", type=float)
    watch.add_argument("--timeout-sec", type=float)

    retry = subparsers.add_parser("retry", help="bounded retry of an eligible failed job")
    retry.add_argument("job_id")

    worker = subparsers.add_parser("worker", help="process queued local AGY jobs")
    worker.add_argument("--wait", action="store_true")

    return parser


def _read_prompt(path_value: str) -> str:
    if path_value == "-":
        return sys.stdin.read()
    return Path(path_value).read_text(encoding="utf-8")


def _read_schema(path_value: str | None) -> dict[str, Any] | None:
    if not path_value:
        return None
    value = json.loads(Path(path_value).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise QueuePolicyError("expected output schema file must contain a JSON object")
    return value


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = QueueConfig.from_env()
    service = QueueService(config)
    try:
        if args.command == "submit":
            prompt = _read_prompt(args.prompt_file)
            request = JobRequest(
                prompt=prompt,
                repo=args.repo,
                kind=args.kind,
                model=args.model or config.default_model,
                effort=args.effort,
                session_key=args.session_key,
                scope_key=args.scope_key,
                max_attempts=(
                    args.max_attempts
                    if args.max_attempts is not None
                    else config.max_attempts
                ),
                timeout_sec=(
                    args.timeout_sec
                    if args.timeout_sec is not None
                    else config.job_timeout_sec
                ),
                expected_output_schema=_read_schema(args.expected_output_schema_file),
                authorization_ref=args.authorization_ref,
                verification_job_id=args.verification_job_id,
            )
            job = service.submit(request)
            _print({"job_id": job.job_id, "status": job.status.value})
            return 0
        if args.command == "get":
            _print(job_to_dict(service.get(args.job_id)))
            return 0
        if args.command == "list":
            status = JobStatus(args.status) if args.status else None
            kind = normalize_job_kind(args.kind) if args.kind else None
            _print([job_to_dict(job) for job in service.list(status=status, job_kind=kind, limit=args.limit)])
            return 0
        if args.command == "watch":
            for job in service.watch(
                args.job_id,
                poll_interval_sec=args.poll_sec,
                timeout_sec=args.timeout_sec,
            ):
                _print(job_to_dict(job))
            return 0
        if args.command == "retry":
            _print(job_to_dict(service.retry(args.job_id)))
            return 0
        if args.command == "worker":
            QueueWorker(service).run(wait=args.wait)
            return 0
        raise QueuePolicyError(f"Unsupported command: {args.command}")
    except (KeyError, QueuePolicyError, RepositoryError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
