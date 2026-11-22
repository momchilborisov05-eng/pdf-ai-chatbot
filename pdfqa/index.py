"""
BM25F retrieval over structure-aware chunks.

Two fields per chunk: the text itself (weight 1.0) and its context, the
section path plus the table header (weight 0.5). Term frequencies are combined
per field before saturation (BM25F), so a word in the heading helps but can't
outweigh the passage. Lucene-style IDF, k1 = 1.2, b = 0.75.

A small bigram bonus rewards chunks that contain adjacent query words in order
("notice period", "maintenance interval").
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass

from pdfqa.chunks import Chunk
from pdfqa.text import tokens

FIELD_WEIGHTS = {"text": 1.0, "context": 0.5}


@dataclass
class Hit:
    chunk: Chunk
    score: float
    rank: int


class BM25Index:
    def __init__(self, chunks: list[Chunk], k1: float = 1.2, b: float = 0.75, bigram_bonus: float = 0.15):
        self.chunks = chunks
        self.k1, self.b, self.bigram_bonus = k1, b, bigram_bonus
        self.postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        self.bigrams: list[set[tuple[str, str]]] = []
        self.lengths: list[float] = []
        for chunk in chunks:
            text_tokens = tokens(chunk.text)
            ctx_tokens = tokens(chunk.context)
            tf: Counter = Counter()
            for t in text_tokens:
                tf[t] += FIELD_WEIGHTS["text"]
            for t in ctx_tokens:
                tf[t] += FIELD_WEIGHTS["context"]
            for term, weight in tf.items():
                self.postings[term].append((chunk.id, weight))
            self.lengths.append(len(text_tokens) + FIELD_WEIGHTS["context"] * len(ctx_tokens))
            self.bigrams.append(set(zip(text_tokens, text_tokens[1:])))
        n = len(chunks)
        self.avg_len = (sum(self.lengths) / n) if n else 1.0
        self.idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}

    def term_idf(self, term: str) -> float:
        return self.idf.get(term, 0.0)

    def search(self, query: str, k: int = 5) -> list[Hit]:
        q = tokens(query)
        if not q or not self.chunks:
            return []
        scores: dict[int, float] = defaultdict(float)
        for term in set(q):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for doc, tf in self.postings[term]:
                norm = 1 - self.b + self.b * self.lengths[doc] / self.avg_len
                scores[doc] += idf * tf * (self.k1 + 1) / (tf + self.k1 * norm)
        if self.bigram_bonus:
            pairs = set(zip(q, q[1:]))
            if pairs:
                for doc in scores:
                    shared = pairs & self.bigrams[doc]
                    scores[doc] += self.bigram_bonus * sum(self.idf.get(a, 0) + self.idf.get(b, 0)
                                                           for a, b in shared)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        return [Hit(self.chunks[doc], score, rank) for rank, (doc, score) in enumerate(ranked, start=1)]
