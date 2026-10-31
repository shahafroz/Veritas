"""Hybrid retrieval: BM25 + dense vectors fused with Reciprocal Rank Fusion, security-trimmed by clearance."""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from . import db
from .config import settings

# --- text normalisation ------------------------------------------------------------------

STOPWORDS = frozenset(
    "a an and are as at be by can do does for from has have how i if in is it its of on or our "
    "should that the their there these this to was we what when where which who why will with "
    "you your my me us they them than then so not no any all may must".split()
)
_TOKEN = re.compile(r"[a-z0-9]+(?:[.\-'][a-z0-9]+)*")
# Words that shape a question but carry no topic. A small corpus makes them look rare (high IDF), so the
# relevance check ignores them: an answer to "how many hours..." needn't contain "many".
QUESTION_FILLER = frozenset("get got need many much soon long often happen make take give let know tell "
                            "allow exact current".split())  # already-stemmed forms, as tokenize() emits


def _stem(tok: str) -> str:
    for suffix in ("ations", "ation", "ings", "ing", "ies", "ied", "es", "ed", "ly", "s"):
        if tok.endswith(suffix) and len(tok) - len(suffix) >= 3:
            return tok[: -len(suffix)] + ("y" if suffix in ("ies", "ied") else "")
    return tok


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


# --- embedders ----------------------------------------------------------------------------


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str], kind: str) -> np.ndarray: ...


class HashingEmbedder:
    """Dependency-free local embedder: signed feature hashing of stems, bigrams and char trigrams.

    It captures lexical and sub-word similarity (typos, morphology) but not true semantics; swap in
    Voyage via VOYAGE_API_KEY for semantic recall. Both share the same interface and eval harness.
    """

    name = "hashing-1024"
    dim = 1024

    def _vec(self, text: str) -> np.ndarray:
        toks = tokenize(text)
        feats: Counter[str] = Counter(toks)
        feats.update(f"{a}_{b}" for a, b in zip(toks, toks[1:]))
        for t in toks:
            padded = f"#{t}#"
            feats.update(f"c:{padded[i:i + 3]}" for i in range(len(padded) - 2))
        v = np.zeros(self.dim, dtype=np.float32)
        for feat, count in feats.items():
            h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
            weight = (1 + math.log(count)) * (0.35 if feat.startswith("c:") else 1.0)
            v[h % self.dim] += weight if (h >> 63) & 1 else -weight
        norm = np.linalg.norm(v)
        return v / norm if norm else v

    def embed(self, texts: list[str], kind: str) -> np.ndarray:
        return np.vstack([self._vec(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)


class VoyageEmbedder:
    def __init__(self, api_key: str, model: str):
        self.api_key, self.model = api_key, model
        self.name = f"voyage:{model}"

    def embed(self, texts: list[str], kind: str) -> np.ndarray:
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            body = json.dumps({"input": texts[i:i + 64], "model": self.model,
                               "input_type": "query" if kind == "query" else "document"}).encode()
            req = urllib.request.Request("https://api.voyageai.com/v1/embeddings", data=body, headers={
                "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                out.extend(d["embedding"] for d in json.load(resp)["data"])
        arr = np.asarray(out, dtype=np.float32)
        return arr / np.linalg.norm(arr, axis=1, keepdims=True).clip(min=1e-9)


def default_embedder() -> Embedder:
    if settings.voyage_api_key:
        return VoyageEmbedder(settings.voyage_api_key, settings.voyage_model)
    return HashingEmbedder()


# --- BM25 ---------------------------------------------------------------------------------


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.72):
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.len = np.array([len(d) for d in docs], dtype=np.float32)
        self.avgdl = float(self.len.mean()) if len(docs) else 1.0
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str]) -> np.ndarray:
        out = np.zeros(len(self.tf), dtype=np.float32)
        norm = self.k1 * (1 - self.b + self.b * self.len / self.avgdl)
        for term in set(query):
            idf = self.idf.get(term)
            if idf is None:
                continue
            f = np.array([tf.get(term, 0) for tf in self.tf], dtype=np.float32)
            out += idf * f * (self.k1 + 1) / (f + norm)
        return out


# --- index ----------------------------------------------------------------------------------


@dataclass
class Hit:
    chunk_id: int
    doc_id: int
    title: str
    heading: str
    text: str
    classification: str
    score: float
    bm25_rank: int | None = None
    dense_rank: int | None = None
    dense_sim: float = 0.0
    # Explainability: raw BM25 score, each retriever's RRF contribution, and the query terms
    # this chunk matched with their IDF weight (rarer term = more influence on the ranking).
    bm25_score: float = 0.0
    rrf_bm25: float = 0.0
    rrf_dense: float = 0.0
    matched_terms: list = field(default_factory=list)


@dataclass
class _View:
    """The slice of an org's corpus visible at one clearance level. Statistics (IDF, avgdl) are
    computed only over visible chunks, so restricted content cannot leak via scoring either."""
    rows: list[dict]
    tokens: list[set[str]]
    bm25: BM25
    matrix: np.ndarray


@dataclass
class _OrgIndex:
    version: int
    rows: list[dict]
    tokens: list[list[str]]
    matrix: np.ndarray
    views: dict[tuple[str, ...], _View] = field(default_factory=dict)


class IndexManager:
    def __init__(self, embedder: Embedder | None = None):
        self.embedder = embedder or default_embedder()
        self._indexes: dict[int, _OrgIndex] = {}
        self._versions: dict[int, int] = {}
        self._lock = threading.Lock()

    def invalidate(self, org_id: int) -> None:
        with self._lock:
            self._versions[org_id] = self._versions.get(org_id, 0) + 1

    def embed_chunks(self, texts: list[str]) -> list[bytes]:
        return [v.astype(np.float32).tobytes() for v in self.embedder.embed(texts, "document")]

    def _load(self, org_id: int) -> _OrgIndex:
        version = self._versions.get(org_id, 0)
        idx = self._indexes.get(org_id)
        if idx is not None and idx.version == version:
            return idx
        with db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT c.id, c.doc_id, d.title, c.heading, c.text, c.classification, c.embedder, c.embedding "
                "FROM chunks c JOIN documents d ON d.id = c.doc_id "
                "WHERE c.org_id = ? AND c.quarantined = 0 ORDER BY c.id", (org_id,))]
        stale = [r for r in rows if r["embedder"] != self.embedder.name or r["embedding"] is None]
        if stale:  # embedder changed (e.g. Voyage key added): re-embed and persist
            vecs = self.embed_chunks([f"{r['heading']}\n{r['text']}" for r in stale])
            with db.write() as conn:
                for r, v in zip(stale, vecs):
                    r["embedding"], r["embedder"] = v, self.embedder.name
                    conn.execute("UPDATE chunks SET embedding = ?, embedder = ? WHERE id = ?",
                                 (v, self.embedder.name, r["id"]))
        matrix = (np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                  if rows else np.zeros((0, 1), np.float32))
        tokens = [tokenize(f"{r['title']} {r['heading']} {r['text']}") for r in rows]
        idx = _OrgIndex(version, rows, tokens, matrix)
        with self._lock:
            self._indexes[org_id] = idx
        return idx

    def _view(self, org_id: int, classifications: list[str]) -> _View:
        idx = self._load(org_id)
        key = tuple(sorted(classifications))
        view = idx.views.get(key)
        if view is None:
            keep = [i for i, r in enumerate(idx.rows) if r["classification"] in classifications]
            view = _View(rows=[idx.rows[i] for i in keep],
                         tokens=[set(idx.tokens[i]) for i in keep],
                         bm25=BM25([idx.tokens[i] for i in keep]),
                         matrix=idx.matrix[keep] if keep else np.zeros((0, 1), np.float32))
            idx.views[key] = view
        return view

    def search(self, org_id: int, query: str, classifications: list[str], k: int = 5,
               mode: str = "hybrid", pool: int = 30, rrf_k: int = 60) -> list[Hit]:
        view = self._view(org_id, classifications)
        if not view.rows:
            return []
        n = len(view.rows)
        q_terms = set(tokenize(query))
        bm25_scores = view.bm25.scores(list(q_terms))
        q = self.embedder.embed([query], "query")[0]
        dense_scores = view.matrix @ q

        bm25_order = [int(i) for i in np.argsort(-bm25_scores)[:pool] if bm25_scores[i] > 0]
        dense_order = [int(i) for i in np.argsort(-dense_scores)[:pool]]
        bm25_rank = {i: r for r, i in enumerate(bm25_order)}
        dense_rank = {i: r for r, i in enumerate(dense_order)}

        if mode == "bm25":
            fused = {i: float(bm25_scores[i]) for i in bm25_order}
        elif mode == "dense":
            fused = {i: float(dense_scores[i]) for i in dense_order}
        else:
            fused = {}
            for ranks in (bm25_rank, dense_rank):
                for i, r in ranks.items():
                    fused[i] = fused.get(i, 0.0) + 1.0 / (rrf_k + r + 1)

        best = sorted(fused, key=fused.get, reverse=True)[: min(k, n)]
        rrf = lambda r: 1.0 / (rrf_k + r + 1) if r is not None else 0.0  # noqa: E731
        return [Hit(chunk_id=view.rows[i]["id"], doc_id=view.rows[i]["doc_id"], title=view.rows[i]["title"],
                    heading=view.rows[i]["heading"], text=view.rows[i]["text"],
                    classification=view.rows[i]["classification"], score=fused[i],
                    bm25_rank=bm25_rank.get(i), dense_rank=dense_rank.get(i),
                    dense_sim=float(dense_scores[i]), bm25_score=float(bm25_scores[i]),
                    rrf_bm25=rrf(bm25_rank.get(i)), rrf_dense=rrf(dense_rank.get(i)),
                    matched_terms=sorted(([t, round(view.bm25.idf[t], 3)] for t in q_terms & view.tokens[i]),
                                         key=lambda m: -m[1]))
                for i in best]


    def coverage(self, org_id: int, query: str, classifications: list[str], hits: list[Hit]) -> float:
        """IDF-weighted share of the query's terms present in the retrieved context (0..1).

        Weighting by IDF means ubiquitous words ("Helix", "policy") can't make an off-topic context look
        relevant; the rare, specific terms of the question have to be there. Terms unseen in the corpus get
        the maximum IDF, since they are the strongest signal that the answer isn't in it.
        """
        if not hits:
            return 0.0
        context = " ".join(f"{h.title} {h.heading} {h.text}" for h in hits)
        return self.term_coverage(org_id, query, classifications, context)[0]

    def idf(self, org_id: int, classifications: list[str], term: str) -> float:
        view = self._view(org_id, classifications)
        return view.bm25.idf.get(term, math.log(1 + (len(view.rows) + 0.5) / 0.5))

    def term_coverage(self, org_id: int, query: str, classifications: list[str],
                      text: str) -> tuple[float, list[str]]:
        """IDF-weighted share of the query's terms found in `text`, plus the missing terms (rarest first).

        Used for the evidence gate (text = retrieved context) and for answer relevance (text = the answer):
        a faithful answer that never mentions the question's most specific term is likely off-target.
        """
        terms = set(tokenize(query))
        if not terms:
            return 0.0, []
        view = self._view(org_id, classifications)
        unseen_idf = math.log(1 + (len(view.rows) + 0.5) / 0.5)
        # also index the parts of hyphenated words, so "home-office" covers "home office"
        present = set(tokenize(text)) | set(tokenize(text.replace("-", " ")))
        weights = {t: view.bm25.idf.get(t, unseen_idf) for t in terms}
        missing = sorted((t for t in terms if t not in present), key=lambda t: -weights[t])
        return sum(w for t, w in weights.items() if t in present) / sum(weights.values()), missing
