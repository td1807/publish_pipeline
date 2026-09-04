"""Every setting in one place, and what each one degrades to.

Read this first. The pipeline is designed to run with nothing installed beyond
the four small libraries in requirements.txt; every heavier capability is
optional and announces its own absence rather than failing or, worse, silently
producing something that looks like the real thing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

# --- identity of the publishing provider -------------------------------------
# A Beckn Catalog MUST carry a provider with a stable id (beckn.yaml: Provider
# requires id + descriptor). In production this is the network-registered
# participant id; here it is configuration, because inventing a registry entry
# would be worse than admitting we do not have one.
PROVIDER_ID = os.environ.get("BECKN_PROVIDER_ID", "prov-imd-agromet")
PROVIDER_CODE = os.environ.get("BECKN_PROVIDER_CODE", "IMD-AGROMET")
PROVIDER_NAME = os.environ.get(
    "BECKN_PROVIDER_NAME", "India Meteorological Department — Agromet Advisory Services"
)
DOMAIN = os.environ.get("BECKN_DOMAIN", "agriculture")
BECKN_VERSION = "2.0.0"

# The JSON-LD context that resourceAttributes declares itself against. This URL
# does not resolve today (checked: DNS failure) — the OpenAgriNet schema
# namespace is not published yet. We emit it because message_update.json does,
# and because @context is REQUIRED by beckn.yaml's Attributes schema. Flagged in
# the README rather than quietly swapped for something that does resolve.
SCHEMA_BASE = os.environ.get(
    "OPENAGRI_SCHEMA_BASE", "https://schemas.openagrinet.global/schema"
)
TAXONOMY_BASE = os.environ.get(
    "OPENAGRI_TAXONOMY_BASE", "https://taxonomy.openagrinet.global"
)
SCHEMA_VERSION = os.environ.get("OPENAGRI_SCHEMA_VERSION", "v0.1")

# --- branch 2b: embeddings ---------------------------------------------------
# "local"   -> sentence-transformers, real 1024-d multilingual vectors
# "remote"  -> an OpenAI-compatible /v1/embeddings endpoint, same model
# "lexical" -> in-process hashed character n-grams. No download. Matches
#              SPELLINGS, NOT MEANINGS, and stamps semantic=False on every
#              result so a degraded run can never be read as a real one.
EMBEDDING_BACKEND = os.environ.get("EMBEDDING_BACKEND", "local").strip().lower()
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-large")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))
EMBEDDING_BATCH = int(os.environ.get("EMBEDDING_BATCH", "16"))
EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")

# e5 is an asymmetric model: it was trained with these exact prefixes and
# silently loses accuracy without them. Index side and query side differ.
E5_PASSAGE_PREFIX = "passage: "
E5_QUERY_PREFIX = "query: "

# --- branch 2b: vector store -------------------------------------------------
# "local" runs Qdrant embedded, in-process, against a directory. No server,
# no Docker. Anything else is treated as a URL to a running Qdrant.
QDRANT_URL = os.environ.get("QDRANT_URL", "local")
QDRANT_PATH = os.environ.get("QDRANT_PATH", str(PACKAGE_DIR / ".qdrant"))
COLLECTION = os.environ.get("QDRANT_COLLECTION", "agri_passages_v4")
VECTOR_DISTANCE = "Cosine"

# --- passage segmentation ----------------------------------------------------
# The cap is set by the EMBEDDING MODEL, not by taste. e5-large has a 512-token
# limit and silently truncates beyond it — no error, just a vector computed from
# a prefix. Devanagari tokenizes far less efficiently than Latin (roughly 2-3x
# the tokens for the same character count), so a limit that is safe for the
# Karnataka bulletin would truncate the Hindi ones. 700 chars keeps even
# Devanagari passages inside the window; embeddings.py measures real token
# lengths and reports any passage that would still truncate.
MIN_PASSAGE_CHARS = int(os.environ.get("MIN_PASSAGE_CHARS", "80"))
MAX_PASSAGE_CHARS = int(os.environ.get("MAX_PASSAGE_CHARS", "700"))

# --- language detection ------------------------------------------------------
# A page counts as non-English only when the dominant Indic script clears BOTH
# an absolute character count and a share of all letters. One stray Devanagari
# glyph in an English table must not flip a page to Hindi.
LANG_MIN_CHARS = int(os.environ.get("LANG_MIN_CHARS", "15"))
LANG_MIN_RATIO = float(os.environ.get("LANG_MIN_RATIO", "0.05"))

# --- publish -----------------------------------------------------------------
# Empty means "use the in-process NetworkNode stand-in". Set it to POST at a
# real /catalog/publish endpoint.
NETWORK_NODE_URL = os.environ.get("BECKN_NETWORK_NODE_URL", "")
VISIBLE_TO = [
    s for s in os.environ.get("BECKN_VISIBLE_TO", "local-network").split(",") if s
]
CATALOG_TYPE = os.environ.get("BECKN_CATALOG_TYPE", "REGULAR")

DATA_DIR = PACKAGE_DIR / "data"
EVIDENCE_DIR = PACKAGE_DIR / "evidence"
STATE_FILE = PACKAGE_DIR / ".v4_state.json"


@dataclass(frozen=True)
class Settings:
    """A resolved snapshot, so a run can print exactly what it used."""

    provider_id: str = PROVIDER_ID
    provider_name: str = PROVIDER_NAME
    domain: str = DOMAIN
    embedding_backend: str = EMBEDDING_BACKEND
    embedding_model: str = EMBEDDING_MODEL
    embedding_dim: int = EMBEDDING_DIM
    qdrant_url: str = QDRANT_URL
    collection: str = COLLECTION
    network_node_url: str = NETWORK_NODE_URL
    visible_to: tuple[str, ...] = field(default_factory=lambda: tuple(VISIBLE_TO))

    def describe(self) -> str:
        node = self.network_node_url or "in-process NetworkNode stand-in"
        store = "Qdrant embedded (in-process)" if self.qdrant_url == "local" else self.qdrant_url
        # EMBEDDING_MODEL is still set when the backend is `lexical`, so naming
        # it here would read as "e5 was used" when no model was loaded at all.
        # Report what the run will actually do instead.
        if self.embedding_backend == "lexical":
            embeddings = (
                f"lexical · NO MODEL · {self.embedding_dim}d · "
                "semantic=False (spellings only)"
            )
        else:
            embeddings = (
                f"{self.embedding_backend} · {self.embedding_model} · {self.embedding_dim}d"
            )
        return (
            f"provider      {self.provider_id} ({self.provider_name})\n"
            f"domain        {self.domain}\n"
            f"embeddings    {embeddings}\n"
            f"vector store  {store} · collection={self.collection}\n"
            f"network node  {node}"
        )


def schema_context_url(capability_type: str) -> str:
    """The JSON-LD context URL for one capability type."""
    return f"{SCHEMA_BASE}/{capability_type}/{SCHEMA_VERSION}/context.jsonld"


def schema_context_ref(capability_type: str) -> str:
    """The fragment-qualified form used in context.schemaContext[]."""
    return f"{schema_context_url(capability_type)}#openagrinet:{capability_type}"
