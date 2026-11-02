"""Qdrant: collection kb_<lab>_<model>_<dim> gồm vector `dense` + sparse `bm25` (modifier IDF).

Truy cập qua alias kb_<lab> để đổi collection (đổi model embedding) không gián đoạn.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

PAYLOAD_INDEXES: dict[str, qm.PayloadSchemaType] = {
    "access": qm.PayloadSchemaType.KEYWORD,
    "source": qm.PayloadSchemaType.KEYWORD,
    "doc_type": qm.PayloadSchemaType.KEYWORD,
    "doc_id": qm.PayloadSchemaType.KEYWORD,
    "lang": qm.PayloadSchemaType.KEYWORD,
    "content_hash": qm.PayloadSchemaType.KEYWORD,
    "doc_day": qm.PayloadSchemaType.INTEGER,
}


@dataclass
class ChunkPoint:
    doc_id: str
    chunk_index: int
    content_hash: str
    dense: list[float]
    sparse: tuple[list[int], list[float]]
    payload: dict[str, Any]

    @property
    def point_id(self) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.doc_id}:{self.content_hash}:{self.chunk_index}"))


@dataclass
class Hit:
    id: str
    score: float
    payload: dict[str, Any]

    @property
    def text(self) -> str:
        return str(self.payload.get("text", ""))


@dataclass
class SearchFilter:
    access_in: list[str]
    doc_type: str | None = None
    day_from: int | None = None
    day_to: int | None = None
    lang: str | None = None
    source: str | None = None

    def to_qdrant(self) -> qm.Filter:
        must: list[Any] = [qm.FieldCondition(key="access", match=qm.MatchAny(any=self.access_in))]
        if self.doc_type:
            must.append(qm.FieldCondition(key="doc_type", match=qm.MatchValue(value=self.doc_type)))
        if self.source:
            must.append(qm.FieldCondition(key="source", match=qm.MatchValue(value=self.source)))
        if self.lang:
            must.append(qm.FieldCondition(key="lang", match=qm.MatchAny(any=[self.lang, "und"])))
        if self.day_from is not None or self.day_to is not None:
            must.append(qm.FieldCondition(key="doc_day", range=qm.Range(gte=self.day_from, lte=self.day_to)))
        return qm.Filter(must=must)


class VectorStore(Protocol):
    async def ensure(self) -> None: ...
    async def upsert(self, points: list[ChunkPoint]) -> None: ...
    async def delete_doc(self, doc_id: str, keep_hash: str | None = None) -> None: ...
    async def hybrid(
        self,
        dense: list[float],
        sparse: tuple[list[int], list[float]],
        flt: SearchFilter,
        prefetch: int,
        limit: int,
    ) -> list[Hit]: ...
    async def count(self) -> int: ...


class QdrantStore:
    def __init__(
        self, url: str, api_key: str | None, collection: str, alias: str, dimensions: int, timeout_s: int = 30
    ) -> None:
        self.client = AsyncQdrantClient(
            url=url, api_key=api_key, timeout=timeout_s, check_compatibility=False
        )
        self.collection = collection
        self.alias = alias
        self.dimensions = dimensions

    async def ensure(self, collection: str | None = None, *, set_alias_if_missing: bool = True) -> None:
        name = collection or self.collection
        if not await self.client.collection_exists(name):
            await self.client.create_collection(
                collection_name=name,
                vectors_config={"dense": qm.VectorParams(size=self.dimensions, distance=qm.Distance.COSINE)},
                sparse_vectors_config={"bm25": qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
            )
            for field, schema in PAYLOAD_INDEXES.items():
                await self.client.create_payload_index(name, field_name=field, field_schema=schema)
        if set_alias_if_missing and await self.alias_target() is None:
            await self.switch_alias(name)

    async def alias_target(self) -> str | None:
        res = await self.client.get_aliases()
        return next((a.collection_name for a in res.aliases if a.alias_name == self.alias), None)

    async def switch_alias(self, collection: str) -> None:
        ops: list[Any] = []
        if await self.alias_target() is not None:
            ops.append(qm.DeleteAliasOperation(delete_alias=qm.DeleteAlias(alias_name=self.alias)))
        ops.append(
            qm.CreateAliasOperation(
                create_alias=qm.CreateAlias(collection_name=collection, alias_name=self.alias)
            )
        )
        await self.client.update_collection_aliases(change_aliases_operations=ops)

    async def upsert(self, points: list[ChunkPoint], collection: str | None = None) -> None:
        if not points:
            return
        target = collection or self.alias
        for i in range(0, len(points), 64):
            batch = points[i : i + 64]
            await self.client.upsert(
                collection_name=target,
                wait=True,
                points=[
                    qm.PointStruct(
                        id=p.point_id,
                        vector={
                            "dense": p.dense,
                            "bm25": qm.SparseVector(indices=p.sparse[0], values=p.sparse[1]),
                        },
                        payload={
                            **p.payload,
                            "doc_id": p.doc_id,
                            "chunk_index": p.chunk_index,
                            "content_hash": p.content_hash,
                        },
                    )
                    for p in batch
                ],
            )

    async def delete_doc(
        self, doc_id: str, keep_hash: str | None = None, collection: str | None = None
    ) -> None:
        """Xoá chunk của doc_id; `keep_hash` giữ lại chunk của phiên bản mới (đổi nội dung không có khoảng trống)."""
        cond: dict[str, Any] = {"must": [qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id))]}
        if keep_hash:
            cond["must_not"] = [qm.FieldCondition(key="content_hash", match=qm.MatchValue(value=keep_hash))]
        await self.client.delete(
            collection_name=collection or self.alias,
            wait=True,
            points_selector=qm.FilterSelector(filter=qm.Filter(**cond)),
        )

    async def hybrid(
        self,
        dense: list[float],
        sparse: tuple[list[int], list[float]],
        flt: SearchFilter,
        prefetch: int,
        limit: int,
    ) -> list[Hit]:
        qf = flt.to_qdrant()
        res = await self.client.query_points(
            collection_name=self.alias,
            prefetch=[
                qm.Prefetch(query=dense, using="dense", limit=prefetch, filter=qf),
                qm.Prefetch(
                    query=qm.SparseVector(indices=sparse[0], values=sparse[1]),
                    using="bm25",
                    limit=prefetch,
                    filter=qf,
                ),
            ],
            query=qm.FusionQuery(fusion=qm.Fusion.RRF),
            query_filter=qf,
            limit=limit,
            with_payload=True,
        )
        return [Hit(str(p.id), float(p.score), dict(p.payload or {})) for p in res.points]

    async def count(self) -> int:
        res = await self.client.count(collection_name=self.alias, exact=True)
        return int(res.count)

    async def health(self) -> bool:
        await self.client.get_collections()
        return True
