"""The vector DB itself: collection schema, payload, filters and ids.

Qdrant, in **embedded** mode by default — `QdrantClient(path=...)` runs the
engine inside this Python process against a local directory. No server, no
Docker, no port. Set `QDRANT_URL` to a URL to talk to a real one instead; the
rest of this module does not change.

What is stored, and why each field is there:

| field              | why it exists                                              |
|--------------------|------------------------------------------------------------|
| vector (1024d)     | the passage text, embedded — what similarity runs on        |
| `text`             | returned verbatim as the answer at follow-up time           |
| `document`, `page` | provenance; a follow-up answer cites "file p.14"            |
| `resource_id`      | **the join to branch 2a** — see below                       |
| `area_code`, `also_area_codes` | narrow by district/state                        |
| `language`         | serve a farmer in the language they asked in                |
| `category`         | crop vs weather vs livestock                                |
| `subject_uris`, `topics`, `weather_parameters` | the same facets 2a published |

`resource_id` is the load-bearing one. Discovery (through the network layer)
can only match on `resourceAttributes`, so what it hands back is a set of
resource ids. The follow-up question then arrives at the provider node directly,
and the only way to search *inside exactly what was advertised* is to filter
these vectors by those ids. If 2a and 2b disagreed about the id, discovery would
point at a resource whose passages cannot be retrieved.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import COLLECTION, EMBEDDING_DIM, QDRANT_PATH, QDRANT_URL, VECTOR_DISTANCE
from ..ingest.passages import Passage
from .embeddings import Embedder

# Fields that get a payload index. Qdrant can filter without one, by scanning;
# an index makes it O(log n) instead. See `describe()` for the caveat about
# embedded mode.
_INDEXED_FIELDS = ("resource_id", "area_code", "language", "category", "document")

_UPSERT_BATCH = 64


@dataclass(frozen=True)
class Hit:
    score: float
    text: str
    document: str
    page: int
    resource_id: str
    language: str
    semantic: bool

    @property
    def citation(self) -> str:
        return f"{self.document} p.{self.page}"


@dataclass(frozen=True)
class IndexResult:
    points: int
    batches: int
    dim: int
    collection: str


class VectorIndex:
    """Branch 2b's store. One collection, one vector per passage."""

    def __init__(
        self,
        embedder: Embedder,
        *,
        collection: str = COLLECTION,
        path: str | None = None,
        url: str | None = None,
    ):
        """`path`/`url` override the configured location.

        Embedded Qdrant takes an exclusive lock on its storage folder, so two
        live indexes in one process must be given different paths. Taking them
        as arguments (rather than only reading module-level config) is what lets
        a test suite hold more than one at a time.
        """
        from qdrant_client import QdrantClient  # noqa: PLC0415

        self.embedder = embedder
        self.collection = collection
        self.url = url or QDRANT_URL
        self.path = path or QDRANT_PATH
        self._embedded = self.url == "local"
        self._client = (
            QdrantClient(path=self.path) if self._embedded else QdrantClient(url=self.url)
        )

    # -- lifecycle ----------------------------------------------------------
    def ensure_collection(self, *, recreate: bool = False) -> None:
        from qdrant_client import models as qm  # noqa: PLC0415

        exists = self._client.collection_exists(self.collection)
        if exists and recreate:
            self._client.delete_collection(self.collection)
            exists = False

        if exists:
            info = self._client.get_collection(self.collection)
            live_dim = info.config.params.vectors.size
            if live_dim != self.embedder.dim:
                raise ValueError(
                    f"collection {self.collection!r} holds {live_dim}-d vectors but "
                    f"the embedder produces {self.embedder.dim}-d. Cosine distance "
                    "between different models is meaningless — recreate the "
                    "collection (--fresh) rather than mixing them."
                )
            return

        self._client.create_collection(
            collection_name=self.collection,
            vectors_config=qm.VectorParams(
                size=self.embedder.dim,
                distance=qm.Distance.COSINE,
            ),
        )
        # Embedded Qdrant ignores payload indexes and evaluates filters by
        # scanning, so creating them there only emits a warning per field.
        # Filters remain correct either way; see describe().
        if not self._embedded:
            for field in _INDEXED_FIELDS:
                self._client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field,
                    field_schema=qm.PayloadSchemaType.KEYWORD,
                )

    # -- write --------------------------------------------------------------
    def index_passages(self, passages: list[Passage]) -> IndexResult:
        """Embed and upsert. Deterministic ids make this idempotent.

        Re-onboarding the same PDF replaces each point rather than appending a
        second copy, because the id is a hash of
        (provider, document, page, ordinal) — see taxonomy/ids.py.
        """
        from qdrant_client import models as qm  # noqa: PLC0415

        if not passages:
            return IndexResult(0, 0, self.embedder.dim, self.collection)

        self.ensure_collection()
        batches = 0
        written = 0

        for start in range(0, len(passages), _UPSERT_BATCH):
            chunk = passages[start : start + _UPSERT_BATCH]
            vectors = self.embedder.embed_passages([p.text for p in chunk])
            points = [
                qm.PointStruct(
                    id=p.point_id,
                    vector=vec,
                    # The facets come from Passage.facets(), the SAME method
                    # branch 2a reads. Parity is structural, not maintained by
                    # hand — tests/test_v4.py::test_facet_parity asserts it.
                    payload={
                        "text": p.text,
                        "document": p.document,
                        "page": p.page,
                        **p.facets(),
                    },
                )
                for p, vec in zip(chunk, vectors)
            ]
            self._client.upsert(collection_name=self.collection, points=points, wait=True)
            batches += 1
            written += len(points)

        return IndexResult(written, batches, self.embedder.dim, self.collection)

    # -- read ---------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        resource_ids: list[str] | None = None,
        area_code: str | None = None,
        language: str | None = None,
        limit: int = 5,
    ) -> list[Hit]:
        """Semantic search, optionally scoped to what discovery advertised.

        `resource_ids` is how the follow-up leg stays inside the resources the
        consumer was actually given. NOTE: it scopes the search, it does not
        authenticate the caller — resource ids are public. See README
        "Known limits".
        """
        from qdrant_client import models as qm  # noqa: PLC0415

        must: list = []
        if resource_ids:
            must.append(
                qm.FieldCondition(
                    key="resource_id", match=qm.MatchAny(any=list(resource_ids))
                )
            )
        if area_code:
            must.append(qm.FieldCondition(key="area_code", match=qm.MatchValue(value=area_code)))
        if language:
            must.append(qm.FieldCondition(key="language", match=qm.MatchValue(value=language)))

        result = self._client.query_points(
            collection_name=self.collection,
            query=self.embedder.embed_query(query),
            query_filter=qm.Filter(must=must) if must else None,
            limit=limit,
            with_payload=True,
        )
        return [
            Hit(
                score=pt.score,
                text=pt.payload.get("text", ""),
                document=pt.payload.get("document", ""),
                page=int(pt.payload.get("page", 0)),
                resource_id=pt.payload.get("resource_id", ""),
                language=pt.payload.get("language", ""),
                semantic=self.embedder.semantic,
            )
            for pt in result.points
        ]

    def count(self) -> int:
        return int(self._client.count(self.collection, exact=True).count)

    def get_payload(self, point_id: str) -> dict | None:
        found = self._client.retrieve(self.collection, ids=[point_id], with_payload=True)
        return found[0].payload if found else None

    def vector_of(self, point_id: str) -> list[float] | None:
        found = self._client.retrieve(self.collection, ids=[point_id], with_vectors=True)
        return list(found[0].vector) if found else None

    def close(self) -> None:
        self._client.close()

    # -- honesty ------------------------------------------------------------
    def describe(self) -> str:
        mode = "embedded (in-process)" if self._embedded else f"server {self.url}"
        caveat = ""
        if self._embedded:
            # Worth stating out loud: embedded Qdrant does not use payload
            # indexes, it scans. Filters still return the right rows, so results
            # are correct — but a latency figure from this mode must not be
            # quoted as a production one.
            caveat = (
                "\n              note: embedded mode evaluates filters by scanning "
                "(payload indexes are ignored). Correct results, but do not quote "
                "its latency as production."
            )
        return (
            f"vector store  Qdrant {mode}\n"
            f"              collection={self.collection} dim={self.embedder.dim} "
            f"distance={VECTOR_DISTANCE}\n"
            f"              payload indexes: {', '.join(_INDEXED_FIELDS)}\n"
            f"              embedder: {self.embedder.describe()}{caveat}"
        )
