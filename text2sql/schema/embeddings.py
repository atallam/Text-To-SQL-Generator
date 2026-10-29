"""Embedding providers behind one small interface.

* ``SentenceTransformerEmbedder``: local BAAI/bge-small-en-v1.5 (the project default; needs the
  ``embeddings`` extra and a one-time model download, then runs fully offline).
* ``LexicalEmbedder``: dependency-free hashed bag of words and character n-grams. Deterministic,
  so CI and unit tests need no model download. Weaker than a real model; used as the offline fallback.

Switching providers is a config change (``EMBEDDING_PROVIDER`` / ``EMBEDDING_MODEL``).
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

import numpy as np

from text2sql.config.settings import Settings, get_settings

_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


def cosine_matrix(queries: np.ndarray, docs: np.ndarray) -> np.ndarray:
    """Cosine similarity for L2-normalised rows: shape (len(queries), len(docs))."""
    return np.atleast_2d(queries) @ np.atleast_2d(docs).T


_STOPWORDS = frozenset(
    "a an and are as at be by did do does for from had has have how i if in is it its me my of on or "
    "our show tell that the their there these this to was were what when where which who whom why will "
    "with you your all any each per over under last this next many much list give get".split()
)
_TOKEN = re.compile(r"[a-z0-9]+")


def _stem(word: str) -> str:
    """Tiny plural stemmer: categories -> category, prices -> price, boxes -> box, address stays."""
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "shes", "ches", "xes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    return [_stem(w) for w in _TOKEN.findall(text.lower().replace("_", " ")) if w not in _STOPWORDS]


class LexicalEmbedder:
    """Hashed word + character-trigram counts, L2-normalised. Deterministic across runs and platforms."""

    name = "lexical"

    def __init__(self, dim: int = 4096, char_weight: float = 0.35):
        self.dim = dim
        self.char_weight = char_weight

    def _bucket(self, feature: str) -> int:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=4).digest()
        return int.from_bytes(digest, "little") % self.dim

    def _vector(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        for tok in _tokens(text):
            vec[self._bucket("w:" + tok)] += 1.0
            padded = f"#{tok}#"
            for i in range(len(padded) - 2):
                vec[self._bucket("c:" + padded[i : i + 3])] += self.char_weight
        norm = np.linalg.norm(vec)
        return vec / norm if norm else vec

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return np.vstack([self._vector(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return self._vector(text)


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                'sentence-transformers is not installed. Run: pip install -e ".[embeddings]" '
                "(or set EMBEDDING_PROVIDER=lexical for the lightweight offline fallback)."
            ) from exc
        self.name = f"sentence-transformers:{model_name}"
        self._model = SentenceTransformer(model_name)
        # bge models are trained with an instruction on the QUERY side only.
        self._query_prefix = _BGE_QUERY_PREFIX if "bge" in model_name.lower() else ""

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._model.get_sentence_embedding_dimension()), np.float32)
        return np.asarray(self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False))

    def embed_query(self, text: str) -> np.ndarray:
        return np.asarray(
            self._model.encode(
                [self._query_prefix + text], normalize_embeddings=True, show_progress_bar=False
            )[0]
        )


def make_embedder(settings: Settings | None = None) -> Embedder:
    settings = settings or get_settings()
    if settings.embedding_provider == "lexical":
        return LexicalEmbedder()
    return SentenceTransformerEmbedder(settings.embedding_model)
