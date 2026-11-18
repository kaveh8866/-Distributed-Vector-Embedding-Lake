"""Unit tests for the parts that do not require a live Qdrant instance.

Run from the repository root:

    python -m unittest discover -s tests -v

The chunker, deterministic embedder and evaluation metrics are pure functions,
so these tests need no third-party package. YAML parsing is the only test that
depends on ``pyyaml`` and is skipped when it is absent.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from vlake.embeddings import HashingEmbedder, build_embedder, tokenize  # noqa: E402
from vlake.evaluation import load_cases, recall_at_k, reciprocal_rank  # noqa: E402
from vlake.ingestion import chunk_document, chunk_text, load_documents  # noqa: E402

DATASETS = REPO_ROOT / "configs" / "datasets"

try:
    import yaml  # noqa: F401

    HAVE_YAML = True
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    HAVE_YAML = False


class ChunkingTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self) -> None:
        self.assertEqual(chunk_text("short document", chunk_size=100, overlap=10), ["short document"])

    def test_empty_text_yields_no_chunks(self) -> None:
        self.assertEqual(chunk_text("   \n\t ", chunk_size=50, overlap=5), [])

    def test_chunks_respect_size_and_cover_text(self) -> None:
        words = [f"token{i}" for i in range(400)]
        text = " ".join(words)
        chunks = chunk_text(text, chunk_size=200, overlap=40)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 200)
        self.assertTrue(chunks[0].startswith("token0"))
        self.assertTrue(chunks[-1].endswith("token399"))

    def test_overlap_is_rejected_when_not_smaller_than_window(self) -> None:
        with self.assertRaises(ValueError):
            chunk_text("text", chunk_size=100, overlap=100)
        with self.assertRaises(ValueError):
            chunk_text("text", chunk_size=100, overlap=-1)

    def test_chunk_ids_are_stable_and_indexed(self) -> None:
        document = load_documents(DATASETS / "sample_documents.jsonl")[0]
        first = chunk_document(document, chunk_size=400, overlap=60)
        second = chunk_document(document, chunk_size=400, overlap=60)

        self.assertEqual([chunk.chunk_id for chunk in first], [chunk.chunk_id for chunk in second])
        self.assertEqual(first[0].chunk_id, f"{document.doc_id}::0000")
        self.assertEqual(first[0].metadata["chunk_index"], 0)
        self.assertEqual(first[0].metadata["chunk_count"], len(first))


class EmbeddingTests(unittest.TestCase):
    def test_hashing_embedder_is_deterministic(self) -> None:
        embedder = HashingEmbedder(64)
        first = embedder.embed_documents(["vector search over documents"])
        second = HashingEmbedder(64).embed_documents(["vector search over documents"])

        self.assertEqual(first, second)
        self.assertEqual(len(first[0]), 64)

    def test_vectors_are_l2_normalised(self) -> None:
        vector = HashingEmbedder(128).embed_query("quantization reduces memory")
        norm = sum(value * value for value in vector) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=6)

    def test_different_texts_produce_different_vectors(self) -> None:
        embedder = HashingEmbedder(128)
        left, right = embedder.embed_documents(["qdrant segments", "oci block volumes"])
        self.assertNotEqual(left, right)

    def test_query_embedding_matches_document_embedding(self) -> None:
        embedder = build_embedder("hashing", "unused", 96)
        self.assertEqual(embedder.embed_query("same text"), embedder.embed_documents(["same text"])[0])

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            build_embedder("not-a-provider", "unused", 32)

    def test_tokenizer_lowercases_and_splits(self) -> None:
        self.assertEqual(tokenize("Qdrant, HNSW-Index!"), ["qdrant", "hnsw", "index"])


class EvaluationTests(unittest.TestCase):
    def test_recall_counts_unique_relevant_hits(self) -> None:
        self.assertEqual(recall_at_k(["a", "b", "c"], ["a", "c"], 3), 1.0)
        self.assertEqual(recall_at_k(["a", "b", "c"], ["a", "c"], 2), 0.5)
        self.assertEqual(recall_at_k(["a", "b", "c"], ["z"], 3), 0.0)

    def test_reciprocal_rank_uses_first_relevant_hit(self) -> None:
        self.assertEqual(reciprocal_rank(["x", "a", "b"], ["a"]), 0.5)
        self.assertEqual(reciprocal_rank(["a"], ["a"]), 1.0)
        self.assertEqual(reciprocal_rank(["x"], ["a"]), 0.0)

    def test_invalid_arguments_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            recall_at_k(["a"], ["a"], 0)
        with self.assertRaises(ValueError):
            recall_at_k(["a"], [], 1)

    def test_eval_dataset_parses(self) -> None:
        cases = load_cases(DATASETS / "eval_queries.jsonl")
        self.assertGreaterEqual(len(cases), 5)
        self.assertTrue(all(case.relevant_doc_ids for case in cases))


class DatasetTests(unittest.TestCase):
    def test_sample_corpus_is_valid_jsonl(self) -> None:
        documents = load_documents(DATASETS / "sample_documents.jsonl")
        self.assertGreaterEqual(len(documents), 10)
        self.assertEqual(len({document.doc_id for document in documents}), len(documents))
        for document in documents:
            self.assertTrue(document.text.strip())
            json.dumps(document.metadata)

    def test_every_eval_case_points_at_a_known_document(self) -> None:
        documents = {document.doc_id for document in load_documents(DATASETS / "sample_documents.jsonl")}
        for case in load_cases(DATASETS / "eval_queries.jsonl"):
            self.assertTrue(set(case.relevant_doc_ids) <= documents, case.query)


class SettingsTests(unittest.TestCase):
    def test_defaults_load_with_empty_environment(self) -> None:
        from vlake.config import Settings

        with mock.patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.embedding_provider, "hashing")
        self.assertEqual(settings.vector_size, 384)
        self.assertEqual(settings.collection, "rag_documents")
        self.assertEqual(settings.chunk_overlap, 120)

    def test_inconsistent_chunking_is_rejected(self) -> None:
        from vlake.config import ConfigError, Settings

        with mock.patch.dict(os.environ, {"CHUNK_SIZE": "100", "CHUNK_OVERLAP": "100"}, clear=True):
            with self.assertRaises(ConfigError):
                Settings.from_env()


@unittest.skipUnless(HAVE_YAML, "pyyaml is not installed")
class SchemaTests(unittest.TestCase):
    def test_collection_spec_matches_config(self) -> None:
        from vlake.config import CollectionConfig
        from vlake.schema import collection_spec

        config = CollectionConfig.from_file()
        spec = collection_spec(config)

        self.assertEqual(spec["name"], config.name)
        self.assertEqual(spec["vector_size"], config.vector_size)
        self.assertEqual(spec["distance"], "Cosine")
        self.assertEqual(spec["hnsw"]["m"], config.hnsw_m)
        self.assertEqual(spec["quantization"], config.quantization)


@unittest.skipUnless(HAVE_YAML, "pyyaml is not installed")
class CollectionConfigTests(unittest.TestCase):
    def test_vector_db_config_loads(self) -> None:
        from vlake.config import CollectionConfig

        config = CollectionConfig.from_file()
        self.assertEqual(config.vector_size, 384)
        self.assertEqual(config.distance, "Cosine")
        self.assertGreater(config.hnsw_m, 0)
        self.assertTrue(config.on_disk_payload)


if __name__ == "__main__":
    unittest.main()
