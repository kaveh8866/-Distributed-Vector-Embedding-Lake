"""Collection schema initialisation.

Separates "create the index" from "load the data": an operator (or CI) can
assert the collection exists with the expected dimension, distance and
quantization before any ingestion runs, and inspect it without writing.

    python -m vlake.schema --recreate     # drop, then create from configs/
    python -m vlake.schema --inspect      # read-only status and shape

The collection definition comes from `configs/vector_db.yaml`; environment
variables provide the endpoint and credentials. No credential is ever printed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Sequence

from .config import CollectionConfig, ConfigError, Settings
from .ingestion import build_client, ensure_collection

LOGGER = logging.getLogger("vlake.schema")


def collection_spec(config: CollectionConfig) -> dict[str, Any]:
    """Provider-neutral description of the collection the worker will create.

    Pure function so the schema contract can be tested without Qdrant.
    """
    return {
        "name": config.name,
        "vector_size": config.vector_size,
        "distance": config.distance,
        "hnsw": {
            "m": config.hnsw_m,
            "ef_construct": config.hnsw_ef_construct,
            "full_scan_threshold_kb": config.full_scan_threshold_kb,
        },
        "quantization": config.quantization,
        "on_disk_payload": config.on_disk_payload,
        "replication_factor": config.replication_factor,
        "shard_number": config.shard_number,
    }


def describe_collection(client: Any, name: str) -> dict[str, Any]:
    """Read back the live collection shape and point count."""
    info = client.get_collection(name)
    vectors = info.config.params.vectors

    # A single unnamed vector space is the supported layout. Named vectors would
    # arrive as a dict and are reported without guessing a dimension.
    if hasattr(vectors, "size"):
        vector_size = int(vectors.size)
        distance = str(vectors.distance).split(".")[-1]
    else:
        vector_size = None
        distance = None

    quantization = getattr(info.config, "quantization_config", None)
    quantization_type = getattr(getattr(quantization, "scalar", None), "type", None)
    if quantization_type is not None:
        quantization_type = str(quantization_type).split(".")[-1]

    return {
        "collection": name,
        "status": str(info.status).split(".")[-1].lower(),
        "points_count": int(client.count(collection_name=name, exact=True).count),
        "vector_size": vector_size,
        "distance": distance,
        "quantization": quantization_type,
        "indexed_vectors_count": getattr(info, "indexed_vectors_count", None),
        "segments_count": getattr(info, "segments_count", None),
        "optimizer_status": str(getattr(info, "optimizer_status", "") or ""),
    }


def initialise(
    settings: Settings,
    config: CollectionConfig,
    *,
    recreate: bool = False,
) -> dict[str, Any]:
    """Create the collection if needed and return its live description."""
    if config.vector_size != settings.vector_size:
        raise ConfigError(
            f"VECTOR_SIZE={settings.vector_size} does not match collection vector_size={config.vector_size}"
        )

    client = build_client(settings)
    created = ensure_collection(client, config, recreate=recreate)
    description = describe_collection(client, config.name)
    description["created"] = created
    description["spec"] = collection_spec(config)
    return description


def inspect(settings: Settings, name: str) -> dict[str, Any]:
    """Read-only inspection; fails clearly when the collection is absent."""
    client = build_client(settings)
    if not client.collection_exists(name):
        raise ConfigError(f"collection {name!r} does not exist; run this command without --inspect first")
    return describe_collection(client, name)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or inspect the vector lake collection.")
    parser.add_argument("--recreate", action="store_true", help="drop the collection before creating it")
    parser.add_argument("--inspect", action="store_true", help="read-only: do not create anything")
    parser.add_argument("--log-level", default=None, help="override LOG_LEVEL")
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
        config = CollectionConfig.from_file()
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, (args.log_level or settings.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        report = (
            inspect(settings, config.name)
            if args.inspect
            else initialise(settings, config, recreate=args.recreate)
        )
    except ConfigError as error:
        print(f"schema error: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - CLI boundary reports and exits non-zero
        LOGGER.error("schema operation failed: %s", error)
        return 1

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
