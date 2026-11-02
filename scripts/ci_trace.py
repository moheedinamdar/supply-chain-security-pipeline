#!/usr/bin/env python3
"""Export a GitHub Actions workflow run as an OpenTelemetry trace.

Run -> job -> step spans, using the OpenTelemetry CI/CD semantic conventions.
Standard library only, so it runs on a bare runner and can be read in one sitting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

API = "https://api.github.com"
RESULTS = {
    "success": "success",
    "failure": "failure",
    "cancelled": "cancellation",
    "skipped": "skip",
    "timed_out": "timeout",
}


def github(path: str, token: str | None) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ci-trace"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(API + path, headers=headers)) as resp:
        return json.load(resp)


def nanos(ts: str) -> int:
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1_000_000_000)


def span_id(*parts: object) -> str:
    return hashlib.sha256("/".join(map(str, parts)).encode()).hexdigest()[:16]


def attr(key: str, value: object) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def status(conclusion: str | None) -> dict:
    if conclusion in ("failure", "timed_out"):
        return {"code": 2, "message": conclusion}
    return {"code": 1} if conclusion == "success" else {"code": 0}


def collect(repo: str, run_id: int, attempt: int, token: str | None) -> tuple[dict, list[dict]]:
    run = github(f"/repos/{repo}/actions/runs/{run_id}", token)
    jobs = github(f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100", token)["jobs"]
    # The job running this export is still in progress and has no end time yet.
    return run, [j for j in jobs if j.get("completed_at")]


def build_trace(repo: str, run: dict, jobs: list[dict], attempt: int) -> tuple[str, dict]:
    trace_id = hashlib.sha256(f"{repo}/{run['id']}/{attempt}".encode()).hexdigest()[:32]
    root = span_id("run", run["id"], attempt)
    start = nanos(run["run_started_at"])
    end = max((nanos(j["completed_at"]) for j in jobs), default=start)
    failed = any(j["conclusion"] == "failure" for j in jobs)

    spans = [{
        "traceId": trace_id,
        "spanId": root,
        "name": f"{run['name']} #{run['run_number']}",
        "kind": 1,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": [
            attr("cicd.pipeline.name", run["name"]),
            attr("cicd.pipeline.run.id", run["id"]),
            attr("cicd.pipeline.run.url.full", run["html_url"]),
            attr("cicd.pipeline.result", "failure" if failed else "success"),
            attr("vcs.repository.url.full", f"https://github.com/{repo}"),
            attr("vcs.ref.head.name", run["head_branch"]),
            attr("vcs.ref.head.revision", run["head_sha"]),
        ],
        "status": status("failure" if failed else "success"),
    }]

    for job in jobs:
        job_span = span_id("job", job["id"])
        spans.append({
            "traceId": trace_id,
            "spanId": job_span,
            "parentSpanId": root,
            "name": job["name"],
            "kind": 1,
            "startTimeUnixNano": str(nanos(job["started_at"])),
            "endTimeUnixNano": str(nanos(job["completed_at"])),
            "attributes": [
                attr("cicd.pipeline.task.name", job["name"]),
                attr("cicd.pipeline.task.run.id", job["id"]),
                attr("cicd.pipeline.task.run.url.full", job["html_url"]),
                attr("cicd.pipeline.task.run.result", RESULTS.get(job["conclusion"], "error")),
                attr("cicd.worker.name", job.get("runner_name") or "unknown"),
            ],
            "status": status(job["conclusion"]),
        })
        for step in job.get("steps", []):
            if not step.get("started_at") or not step.get("completed_at"):
                continue
            spans.append({
                "traceId": trace_id,
                "spanId": span_id("step", job["id"], step["number"]),
                "parentSpanId": job_span,
                "name": step["name"],
                "kind": 1,
                "startTimeUnixNano": str(nanos(step["started_at"])),
                "endTimeUnixNano": str(nanos(step["completed_at"])),
                "attributes": [
                    attr("cicd.pipeline.task.name", job["name"]),
                    attr("cicd.pipeline.task.run.result", RESULTS.get(step["conclusion"], "error")),
                    attr("github.step.number", step["number"]),
                ],
                "status": status(step["conclusion"]),
            })

    payload = {"resourceSpans": [{
        "resource": {"attributes": [
            attr("service.name", "supply-chain-pipeline"),
            attr("service.namespace", repo),
        ]},
        "scopeSpans": [{"scope": {"name": "ci_trace", "version": "1"}, "spans": spans}],
    }]}
    return trace_id, payload


def post(endpoint: str, payload: dict, wait: int) -> None:
    body = json.dumps(payload).encode()
    deadline = time.monotonic() + wait
    while True:
        try:
            req = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as resp:
                print(f"exported {len(payload['resourceSpans'][0]['scopeSpans'][0]['spans'])} spans: HTTP {resp.status}")
                return
        except (urllib.error.URLError, ConnectionError) as exc:
            if time.monotonic() > deadline:
                raise SystemExit(f"collector not reachable at {endpoint}: {exc}")
            time.sleep(2)


def verify_jaeger(base: str, trace_id: str, wait: int) -> int:
    deadline = time.monotonic() + wait
    while True:
        try:
            with urllib.request.urlopen(f"{base}/api/traces/{trace_id}") as resp:
                spans = sum(len(t["spans"]) for t in json.load(resp).get("data", []))
                if spans:
                    print(f"Jaeger stored trace {trace_id} with {spans} spans")
                    return spans
        except urllib.error.URLError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit(f"trace {trace_id} never appeared in Jaeger")
        time.sleep(2)


def gantt(run: dict, jobs: list[dict]) -> str:
    lines = [
        "```mermaid",
        "gantt",
        f"  title {run['name']} #{run['run_number']} ({run['head_sha'][:7]})",
        "  dateFormat x",
        "  axisFormat %M:%S",
    ]
    for job in jobs:
        lines.append(f"  section {job['name'].replace(':', ' ')}")
        for step in job.get("steps", []):
            if step.get("conclusion") == "skipped" or not step.get("completed_at"):
                continue
            begin, finish = nanos(step["started_at"]) // 1_000_000, nanos(step["completed_at"]) // 1_000_000
            if finish <= begin:
                continue
            tag = "crit" if step["conclusion"] == "failure" else "done"
            name = step["name"].replace(":", " ").replace("#", "")
            lines.append(f"  {name} :{tag}, {begin}, {finish}")
    lines.append("```")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo", required=True)
    p.add_argument("--run-id", type=int, required=True)
    p.add_argument("--attempt", type=int, default=1)
    p.add_argument("--endpoint", help="OTLP/HTTP traces URL, e.g. http://localhost:4318/v1/traces")
    p.add_argument("--jaeger", help="Jaeger query base URL to confirm the trace was stored")
    p.add_argument("--summary", help="file to append a Markdown summary with a Mermaid timeline")
    p.add_argument("--out", help="write the OTLP JSON payload to this file")
    p.add_argument("--wait", type=int, default=60, help="seconds to wait for the backends")
    args = p.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    run, jobs = collect(args.repo, args.run_id, args.attempt, token)
    trace_id, payload = build_trace(args.repo, run, jobs, args.attempt)
    print(f"trace {trace_id}: {len(jobs)} completed jobs")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    if args.endpoint:
        post(args.endpoint, payload, args.wait)
    stored = verify_jaeger(args.jaeger, trace_id, args.wait) if args.jaeger else None
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(f"### Pipeline trace `{trace_id}`\n\n")
            if stored:
                fh.write(f"Stored in Jaeger with {stored} spans; raw OTLP export is in the `otel-trace` artifact.\n\n")
            fh.write(gantt(run, jobs) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
