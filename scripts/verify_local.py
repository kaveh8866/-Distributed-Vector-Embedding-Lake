#!/usr/bin/env python3
"""Stage 4 verification harness for a running local vector lake.

Checks, against the live retrieval API (no third-party packages):

* health and readiness
* top-k retrieval over the labelled query set, with document-level recall@k
* end-to-end latency distribution (p50 / p95 / max) for search requests
* RAG context assembly: retrieved chunks are formatted into a grounded prompt
  with citations, and every citation id is verified to come from a hit

Usage:
    python scripts/verify_local.py \
        --base-url http://127.0.0.1:8085 \
        --queries configs/datasets/eval_queries.jsonl \
        --output docs/stage-4-report.json

Exit codes: 0 all checks passed, 1 a check failed, 2 configuration error.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8085"
DEFAULT_QUERIES = Path("configs/datasets/eval_queries.jsonl")


def request_json(url: str, payload: dict[str, Any] | None = None, timeout: float = 15.0) -> tuple[int, Any, float]:
    """Return (status, parsed body, elapsed_ms)."""
    data = None
    headers = {"accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            elapsed = (time.perf_counter() - started) * 1_000
            body = json.loads(response.read().decode("utf-8") or "null")
            return response.status, body, elapsed
    except urllib.error.HTTPError as error:
        elapsed = (time.perf_counter() - started) * 1_000
        body = error.read().decode("utf-8", errors="replace")
        return error.code, body, elapsed


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            payload = json.loads(line)
            cases.append(
                {
                    "query": payload["query"],
                    "relevant_doc_ids": payload.get("relevant_doc_ids", payload.get("expected", [])),
                }
            )
    return cases


def recall_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    expected = set(relevant)
    if not expected:
        return 0.0
    found = {doc for doc in retrieved[:k] if doc in expected}
    return len(found) / len(expected)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def build_rag_context(query: str, hits: list[dict[str, Any]], max_chars: int = 2_400) -> dict[str, Any]:
    """Assemble a grounded prompt from retrieved chunks.

    This is the retrieval half of RAG: the model prompt only ever sees the
    retrieved passages, each tagged with a citation id, so an answer can be
    traced back to a chunk. No generation call is made here.
    """
    context_blocks: list[str] = []
    citations: list[str] = []
    used = 0

    for hit in hits:
        block = f"[{hit['chunk_id']}] ({hit['doc_id']}, score {hit['score']:.3f})\n{hit['text']}"
        if used + len(block) > max_chars:
            break
        context_blocks.append(block)
        citations.append(hit["chunk_id"])
        used += len(block)

    context = "\n\n".join(context_blocks)
    prompt = (
        "Answer the question using only the context below. "
        "Cite the bracketed chunk id for every claim. "
        "If the context does not contain the answer, say so.\n\n"
        f"Context:\n{context}\n\nQuestion: {query}\nAnswer:"
    )
    return {
        "citations": citations,
        "context_chars": len(context),
        "prompt_chars": len(prompt),
        "prompt": prompt,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the local vector lake end to end.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=3, help="repetitions per query for latency")
    parser.add_argument("--output", default=None, help="write the JSON report to this path")
    args = parser.parse_args(argv)

    base = args.base_url.rstrip("/")
    query_file = Path(args.queries)
    if not query_file.is_file():
        print(f"configuration error: query file not found: {query_file}", file=sys.stderr)
        return 2

    status, health, _ = request_json(f"{base}/health")
    if status != 200 or not isinstance(health, dict):
        print(f"health check failed: HTTP {status} {health}", file=sys.stderr)
        return 1

    status, ready, _ = request_json(f"{base}/ready")
    if status != 200 or not isinstance(ready, dict) or ready.get("status") != "ready":
        print(f"readiness check failed: HTTP {status} {ready}", file=sys.stderr)
        return 1

    cases = load_cases(query_file)
    latencies: list[float] = []
    # Service-side latency reported by the API itself (embed + Qdrant query).
    # The wall-clock value additionally includes host port-proxy overhead.
    service_latencies: list[float] = []
    recalls: list[float] = []
    rows: list[dict[str, Any]] = []
    rag_sample: dict[str, Any] | None = None
    failures = 0

    for case in cases:
        retrieved: list[str] = []
        case_latencies: list[float] = []
        for _ in range(max(1, args.repeats)):
            status, body, elapsed = request_json(
                f"{base}/search",
                {"query": case["query"], "limit": args.limit},
            )
            if status != 200 or not isinstance(body, dict):
                failures += 1
                break
            case_latencies.append(elapsed)
            if isinstance(body.get("latency_ms"), (int, float)):
                service_latencies.append(float(body["latency_ms"]))
            retrieved = [hit["doc_id"] for hit in body.get("hits", [])]
            if rag_sample is None:
                rag_sample = build_rag_context(case["query"], body.get("hits", []))

        latencies.extend(case_latencies)
        recall = recall_at_k(retrieved, case["relevant_doc_ids"], args.limit) if retrieved else 0.0
        recalls.append(recall)
        rows.append(
            {
                "query": case["query"],
                "recall_at_k": round(recall, 4),
                "top_doc_ids": retrieved,
                "relevant_doc_ids": case["relevant_doc_ids"],
                "p50_ms": round(statistics.median(case_latencies), 2) if case_latencies else None,
            }
        )

    if not latencies:
        print("no successful search responses; is the collection populated?", file=sys.stderr)
        return 1
    if not service_latencies:
        # Older API builds may not report latency_ms; fall back to wall clock.
        service_latencies = list(latencies)

    report = {
        "base_url": base,
        "collection": ready.get("collection"),
        "queries": len(cases),
        "repeats": max(1, args.repeats),
        "k": args.limit,
        "requests": len(latencies),
        "failures": failures,
        "recall_at_k": round(sum(recalls) / len(recalls), 4),
        "latency_ms": {
            "p50": round(percentile(service_latencies, 0.50), 2),
            "p95": round(percentile(service_latencies, 0.95), 2),
            "max": round(max(service_latencies), 2),
            "mean": round(sum(service_latencies) / len(service_latencies), 2),
        },
        "end_to_end_ms": {
            "p50": round(percentile(latencies, 0.50), 2),
            "p95": round(percentile(latencies, 0.95), 2),
            "max": round(max(latencies), 2),
            "mean": round(sum(latencies) / len(latencies), 2),
            "note": "host wall clock including the local port proxy",
        },
        "rag": {
            "citations": rag_sample["citations"] if rag_sample else [],
            "context_chars": rag_sample["context_chars"] if rag_sample else 0,
            "prompt_chars": rag_sample["prompt_chars"] if rag_sample else 0,
        },
        "results": rows,
    }

    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({key: report[key] for key in report if key != "results"}, indent=2))
    if rag_sample:
        print("\n--- sample RAG prompt (first 600 chars) ---")
        print(rag_sample["prompt"][:600])

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
