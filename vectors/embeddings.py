"""Text -> vectors. The model, its prefixes, and its limits.

The default is `intfloat/multilingual-e5-large`, which is the same model family
the production docs-pipeline runs in Marqo (`hf/multilingual-e5-large`). Three
properties of it drive every decision in this file:

1. **1024 dimensions, cosine similarity.** Fixed by the model; the collection
   is created to match and mismatches are refused rather than coerced.

2. **It is asymmetric.** e5 was trained with two literal prefixes — `passage: `
   on the indexed side and `query: ` on the search side. They are not
   decoration: dropping them measurably degrades retrieval, and mixing them up
   degrades it further. Index and query paths are therefore separate methods,
   not one method with a flag the caller can forget.

3. **512-token window, and it truncates silently.** Text past the limit is
   dropped with no error and no warning — you simply get a vector computed from
   a prefix of your passage. Devanagari tokenizes 2-3x less efficiently than
   Latin, so the Hindi bulletins are the ones at risk. `token_report()` measures
   real token counts with the model's own tokenizer and says how many passages
   would be clipped, because a silent truncation is indistinguishable from a
   correct embedding when you only look at the output.

The `lexical` fallback exists so the pipeline runs on a laptop with nothing
installed. It matches spellings, not meanings, and every result it produces is
stamped `semantic: False` — a plausible-looking similarity score from a bag of
character n-grams is the easiest way to mislead a room.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Protocol

from ..config import (
    E5_PASSAGE_PREFIX,
    E5_QUERY_PREFIX,
    EMBEDDING_API_KEY,
    EMBEDDING_BACKEND,
    EMBEDDING_BASE_URL,
    EMBEDDING_BATCH,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
)

E5_MAX_TOKENS = 512


@dataclass(frozen=True)
class TokenReport:
    passages: int
    max_tokens: int
    mean_tokens: float
    over_limit: int
    limit: int = E5_MAX_TOKENS

    def summary(self) -> str:
        verdict = (
            f"WARNING: {self.over_limit} passage(s) exceed {self.limit} tokens and "
            "will be silently truncated by the model"
            if self.over_limit
            else f"all {self.passages} passages fit inside the {self.limit}-token window"
        )
        return (
            f"tokens: max={self.max_tokens} mean={self.mean_tokens:.0f} — {verdict}"
        )


class Embedder(Protocol):
    name: str
    dim: int
    semantic: bool

    def embed_passages(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
    def describe(self) -> str: ...
    def token_report(self, texts: list[str]) -> TokenReport | None: ...


class LocalE5Embedder:
    """sentence-transformers, running the real model on CPU or MPS."""

    semantic = True

    def __init__(
        self,
        model_name: str = EMBEDDING_MODEL,
        batch: int = EMBEDDING_BATCH,
        device: str | None = None,
    ):
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        self.name = model_name
        self._batch = batch
        self.device = device or _best_device()
        self._model = SentenceTransformer(model_name, device=self.device)
        # sentence-transformers 6.x renamed this; support both so the package
        # works on either version rather than emitting a FutureWarning per run.
        get_dim = getattr(
            self._model, "get_embedding_dimension", None
        ) or self._model.get_sentence_embedding_dimension
        self.dim = int(get_dim())
        if self.dim != EMBEDDING_DIM:
            raise ValueError(
                f"{model_name} produces {self.dim}-d vectors but EMBEDDING_DIM is "
                f"{EMBEDDING_DIM}. Fix the config rather than reshaping vectors."
            )

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        prefixed = [E5_PASSAGE_PREFIX + t for t in texts]
        vecs = self._model.encode(
            prefixed,
            batch_size=self._batch,
            normalize_embeddings=True,   # cosine distance wants unit vectors
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [v.tolist() for v in vecs]

    def embed_query(self, text: str) -> list[float]:
        vec = self._model.encode(
            [E5_QUERY_PREFIX + text],
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )[0]
        return vec.tolist()

    def token_report(self, texts: list[str]) -> TokenReport:
        tok = self._model.tokenizer
        lengths = [
            len(tok(E5_PASSAGE_PREFIX + t, add_special_tokens=True)["input_ids"])
            for t in texts
        ]
        return TokenReport(
            passages=len(lengths),
            max_tokens=max(lengths, default=0),
            mean_tokens=(sum(lengths) / len(lengths)) if lengths else 0.0,
            over_limit=sum(1 for n in lengths if n > E5_MAX_TOKENS),
        )

    def describe(self) -> str:
        return (
            f"local sentence-transformers · {self.name} · {self.dim}d · "
            f"device={self.device} · normalized · "
            f"prefixes {E5_PASSAGE_PREFIX!r}/{E5_QUERY_PREFIX!r} · semantic=True"
        )


class RemoteE5Embedder:
    """An OpenAI-compatible /v1/embeddings endpoint serving the same model."""

    semantic = True

    def __init__(
        self,
        base_url: str = EMBEDDING_BASE_URL,
        api_key: str = EMBEDDING_API_KEY,
        model_name: str = EMBEDDING_MODEL,
        dim: int = EMBEDDING_DIM,
    ):
        if not base_url:
            raise ValueError("EMBEDDING_BACKEND=remote needs EMBEDDING_BASE_URL")
        self.name = model_name
        self.dim = dim
        self._url = base_url.rstrip("/") + "/embeddings"
        self._key = api_key

    def _post(self, inputs: list[str]) -> list[list[float]]:
        import httpx  # noqa: PLC0415

        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                self._url,
                headers=headers,
                json={"model": self.name, "input": inputs},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        return [row["embedding"] for row in data]

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), EMBEDDING_BATCH):
            batch = [E5_PASSAGE_PREFIX + t for t in texts[i : i + EMBEDDING_BATCH]]
            out.extend(_unit(v) for v in self._post(batch))
        return out

    def embed_query(self, text: str) -> list[float]:
        return _unit(self._post([E5_QUERY_PREFIX + text])[0])

    def token_report(self, texts: list[str]) -> None:
        # We cannot see the server's tokenizer, so we do not guess at one.
        return None

    def describe(self) -> str:
        return f"remote · {self.name} · {self.dim}d · {self._url} · semantic=True"


class LexicalEmbedder:
    """Hashed character 3-grams. No download, no model — and no meaning.

    Present so the pipeline is runnable anywhere. It is NOT a small semantic
    model; it cannot match a paraphrase and it cannot match across languages.
    Anything it returns is stamped semantic=False.
    """

    semantic = False
    name = "lexical-char3gram"

    def __init__(self, dim: int = EMBEDDING_DIM):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        buckets = [0.0] * self.dim
        norm = " ".join((text or "").lower().split())
        for i in range(max(len(norm) - 2, 0)):
            gram = norm[i : i + 3]
            h = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=4).digest(), "big")
            buckets[h % self.dim] += 1.0
        return _unit(buckets)

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    def token_report(self, texts: list[str]) -> None:
        return None

    def describe(self) -> str:
        return (
            f"lexical hashed char-3grams · {self.dim}d · NO MODEL · "
            "semantic=False (matches spellings, not meanings)"
        )


def _best_device() -> str:
    """Prefer the GPU when there is one. Apple Silicon exposes it as `mps`.

    Worth setting explicitly rather than leaving to the default: e5-large is a
    560M-parameter model, and on CPU it dominates the whole pipeline's runtime.
    Set EMBEDDING_DEVICE to override (e.g. "cpu" to reproduce a CPU timing).
    """
    import os

    override = os.environ.get("EMBEDDING_DEVICE", "").strip()
    if override:
        return override
    try:
        import torch  # noqa: PLC0415

        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _unit(vec: list[float]) -> list[float]:
    mag = math.sqrt(sum(v * v for v in vec))
    return [v / mag for v in vec] if mag else list(vec)


def get_embedder(backend: str = EMBEDDING_BACKEND) -> Embedder:
    """Resolve the configured backend, degrading loudly rather than silently."""
    backend = (backend or "").strip().lower()
    if backend == "local":
        try:
            return LocalE5Embedder()
        except ImportError as exc:
            raise RuntimeError(
                "EMBEDDING_BACKEND=local needs sentence-transformers "
                "(`pip install sentence-transformers`, ~2.5 GB with torch). "
                "Set EMBEDDING_BACKEND=lexical to run without it — results will "
                "be non-semantic and labelled as such."
            ) from exc
    if backend == "remote":
        return RemoteE5Embedder()
    if backend == "lexical":
        return LexicalEmbedder()
    raise ValueError(f"unknown EMBEDDING_BACKEND {backend!r} (local|remote|lexical)")
