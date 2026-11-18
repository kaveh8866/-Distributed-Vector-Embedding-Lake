"""Retrieval evaluation loop.

The lake is only useful if retrieval quality is measured, not assumed. This
module scores a query set against the live collection using document-level
relevance:

* ``recall@k`` - share of expected documents present in the top ``k`` results
* ``MRR``      - mean reciprocal rank of the first relevant document

It is intentionally small: the point is a repeatable number for a corpus or
chunking change, not a full IR benchmark harness.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .config import ConfigError, Settings
from .embeddings import build_embedder
from .ingestion import build_client

LOGGER = logging.getLogger("vlake.evaluation")

DEFAULT_DATASET = "eval_queries.jsonl"


@dataclass(frozen=True, slots=True)
class EvalCase:
    query: str
    relevant_doc_ids: tuple[str, ...]
    top_k: int | None = None


def recall_at_k(retrieved_doc_ids: Sequence[str], relevant_doc_ids: Sequence[str], k: int) -> float:
    """Share of relevant documents found in the first ``k`` retrieved results."""
    if k <= 0:
        raise ValueError("k must be positive")
    relevant = set(relevant_doc_ids)
    if not relevant:
        raise ValueError("relevant_doc_ids must not be empty")
    hits = {doc_id for doc_id in retrieved_doc_ids[:k] if doc_id in relevant}
    return len(hits) / len(relevant)


def reciprocal_rank(retrieved_doc_ids: Sequence[str], relevant_doc_ids: Sequence[str]) -> float:
    """1 / rank of the first relevant result, or 0 when none is returned."""
    relevant = set(relevant_doc_ids)
    for rank, doc_id in enumerate(retrieved_doc_ids, start=1):
        if doc_id in relevant:
            return 1.0 / rank
    return 0.0


def load_cases(path: str | Path) -> list[EvalCase]:
    """Read JSONL cases: ``query`` plus ``relevant_doc_ids`` (or ``expected``)."""
    target = Path(path)
    if not target.is_file():
        raise ConfigError(f"evaluation dataset not found: {target}")

    cases: list[EvalCase] = []
    with target.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise ConfigError(f"{target}:{line_number} is not valid JSON: {error.msg}") from error
            if not isinstance(payload, dict):
                raise ConfigError(f"{target}:{line_number} must be a JSON object")

            query = payload.get("query")
            expected = payload.get("relevant_doc_ids", payload.get("expected"))
            if not isinstance(query, str) or not query.strip():
                raise ConfigError(f"{target}:{line_number} needs a non-empty 'query'")
            if not isinstance(expected, list) or not expected:
                raise ConfigError(f"{target}:{line_number} needs a non-empty 'relevant_doc_ids' list")

            top_k = payload.get("top_k")
            cases.append(
                EvalCase(
                    query=query,
                    relevant_doc_ids=tuple(str(doc_id) for doc_id in expected),
                    top_k=int(top_k) if isinstance(top_k, int) else None,
                )
            )

    if not cases:
        raise ConfigError(f"{target} contains no evaluation cases")
    return cases


def evaluate(
    client: Any,
    embedder: Any,
    collection: str,
    cases: Sequence[EvalCase],
    k: int = 5,
) -> dict[str, Any]:
    """Run every case against Qdrant and aggregate the metrics."""
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    rows: list[dict[str, Any]] = []

    for case in cases:
        limit = case.top_k or k
        result = client.query_points(
            collection_name=collection,
            query=embedder.embed_query(case.query),
            limit=limit,
            with_payload=True,
        )

        retrieved: list[str] = []
        for point in result.points:
            doc_id = str((point.payload or {}).get("doc_id", "unknown"))
            if doc_id not in retrieved:
                retrieved.append(doc_id)

        recall = recall_at_k(retrieved, case.relevant_doc_ids, limit)
        rr = reciprocal_rank(retrieved, case.relevant_doc_ids)
        recalls.append(recall)
        reciprocal_ranks.append(rr)
        rows.append(
            {
                "query": case.query,
                "k": limit,
                "recall_at_k": round(recall, 4),
                "reciprocal_rank": round(rr, 4),
                "retrieved_doc_ids": retrieved,
                "relevant_doc_ids": list(case.relevant_doc_ids),
            }
        )

    return {
        "collection": collection,
        "cases": len(cases),
        "k": k,
        "recall_at_k": round(sum(recalls) / len(recalls), 4),
        "mrr": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4),
        "results": rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate retrieval quality of the vector lake.")
    parser.add_argument(
        "--dataset",
        default=str(Path("configs") / "datasets" / DEFAULT_DATASET),
        help="JSONL evaluation cases (default: configs/datasets/eval_queries.jsonl)",
    )
    parser.add_argument("--k", type=int, default=5, help="results per query (default: 5)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        settings = Settings.from_env()
        cases = load_cases(args.dataset)
        client = build_client(settings)
        embedder = build_embedder(
            settings.embedding_provider, settings.embedding_model, settings.vector_size
        )
        report = evaluate(client, embedder, settings.collection, cases, k=args.k)
    except (ConfigError, ValueError) as error:
        print(f"evaluation error: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - CLI boundary reports and exits non-zero
        LOGGER.error("evaluation failed: %s", error)
        return 1

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
