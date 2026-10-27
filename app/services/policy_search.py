"""Policy retrieval for ResolveBot.

``policies.md`` contains all documents separated by top-level ``#`` headings.
Each document is split into numbered sections. The manifest controls which
documents are current/effective and which are superseded or future-dated.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.utils import parse_date

TOP_LEVEL_RE = re.compile(r"^#\s+(?!#)(.*)$")
SECTION_RE = re.compile(
    r"^#{2,3}\s*(?:[§\u00c2]+\s*)?([0-9]+(?:\.[0-9]+)*)\s*(.*)$"
)
DOC_ID_RE = re.compile(r"\*\*Document ID:\*\*\s*([A-Za-z0-9_]+)")
EFFECTIVE_RE = re.compile(r"\*\*Effective date:\*\*\s*([0-9]{4}-[0-9]{2}-[0-9]{2})")
STATUS_RE = re.compile(r"\*\*Status:\*\*\s*([A-Za-z]+)")

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "of", "to", "in", "on", "for",
    "with", "is", "are", "was", "were", "be", "been", "being", "do", "does",
    "did", "i", "me", "my", "we", "our", "you", "your", "it", "its", "this",
    "that", "these", "those", "what", "which", "who", "whom", "how", "when",
    "where", "why", "can", "could", "would", "should", "will", "shall", "may",
    "might", "must", "have", "has", "had", "not", "no", "so", "than", "then",
    "there", "their", "them", "he", "she", "his", "her", "as", "at", "by",
    "from", "about", "into", "over", "under", "also", "any", "all", "some",
    "please", "tell", "want", "need", "know", "shopsphere", "something",
    "anything", "get", "still", "just", "much", "many", "long", "am",
}


def _stem(word: str) -> str:
    if word == "membership":
        return "member"
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ss"):
        return word
    if word.endswith("s") and len(word) > 3:
        return word[:-1]
    if word.endswith("ing") and len(word) > 5:
        return word[:-3]
    if word.endswith("ed") and len(word) > 5:
        return word[:-2]
    return word


def tokenize(text: str) -> list[str]:
    return [
        _stem(word)
        for word in re.findall(r"[a-z0-9]+", (text or "").lower())
        if word not in STOPWORDS and len(word) > 1
    ]


@dataclass(slots=True)
class PolicyChunk:
    doc_id: str
    title: str
    doc_type: str
    section: str
    heading: str
    text: str
    effective_date: date
    status: str


@dataclass(slots=True)
class ScoredChunk:
    chunk: PolicyChunk
    score: float


class PolicyIndex:
    def __init__(self, policies_path: Path | str, manifest_path: Path | str, app_today: date):
        self.app_today = app_today
        self.manifest = self._load_manifest(Path(manifest_path))
        self.chunks = self._parse(Path(policies_path))
        self._tokens: list[list[str]] = []
        self._term_freqs: list[Counter] = []
        self._build_index()

    # ------------------------------------------------------------------ parse
    @staticmethod
    def _load_manifest(path: Path) -> dict[str, dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read manifest {path}: {exc}") from exc
        if not isinstance(data, list):
            raise RuntimeError("manifest.json must contain a JSON array")
        return {
            str(entry["doc_id"]): entry
            for entry in data
            if isinstance(entry, dict) and entry.get("doc_id")
        }

    def _parse(self, path: Path) -> list[PolicyChunk]:
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise RuntimeError(f"Could not read policies {path}: {exc}") from exc

        chunks: list[PolicyChunk] = []
        for document in self._split_documents(raw):
            body = "\n".join(document["lines"])
            doc_id_match = DOC_ID_RE.search(body)
            if not doc_id_match:
                continue
            doc_id = doc_id_match.group(1)
            meta = self.manifest.get(doc_id)
            if meta is None:
                # A document that is not in the manifest is not loadable.
                continue

            effective = parse_date(meta.get("effective_date"))
            if effective is None:
                match = EFFECTIVE_RE.search(body)
                effective = parse_date(match.group(1)) if match else None
            if effective is None:
                continue

            status = str(meta.get("status") or "").strip().lower()
            if not status:
                match = STATUS_RE.search(body)
                status = match.group(1).lower() if match else "current"
            title = str(meta.get("title") or document["title"]).strip()
            doc_type = str(meta.get("doc_type") or "policy").strip().lower()

            chunks.extend(
                self._split_sections(
                    doc_id=doc_id,
                    title=title,
                    doc_type=doc_type,
                    body=body,
                    effective=effective,
                    status=status,
                )
            )
        return chunks

    @staticmethod
    def _split_documents(raw: str) -> list[dict]:
        documents: list[dict] = []
        current: dict | None = None
        for line in raw.splitlines():
            if line.startswith("# ") and not line.startswith("## "):
                match = TOP_LEVEL_RE.match(line)
                if match:
                    if current is not None:
                        documents.append(current)
                    current = {"title": match.group(1).strip(), "lines": []}
                    continue
            if current is not None:
                current["lines"].append(line)
        if current is not None:
            documents.append(current)
        return documents

    @staticmethod
    def _split_sections(
        *,
        doc_id: str,
        title: str,
        doc_type: str,
        body: str,
        effective: date,
        status: str,
    ) -> list[PolicyChunk]:
        chunks: list[PolicyChunk] = []
        current: tuple[str, str] | None = None
        buffer: list[str] = []

        def flush() -> None:
            if current is None:
                return
            text = "\n".join(buffer).strip()
            if not text:
                return
            section, heading = current
            chunks.append(
                PolicyChunk(
                    doc_id=doc_id,
                    title=title,
                    doc_type=doc_type,
                    section=section,
                    heading=heading,
                    text=text,
                    effective_date=effective,
                    status=status,
                )
            )

        for line in body.splitlines():
            match = SECTION_RE.match(line)
            if match:
                flush()
                current = (f"§{match.group(1)}", match.group(2).strip())
                buffer = []
            else:
                buffer.append(line)
        flush()
        return chunks

    # ----------------------------------------------------------------- search
    def _build_index(self) -> None:
        self._doc_freq: Counter = Counter()
        for chunk in self.chunks:
            tokens = (
                tokenize(chunk.title) * 2
                + tokenize(chunk.heading) * 3
                + tokenize(chunk.text)
            )
            self._tokens.append(tokens)
            self._term_freqs.append(Counter(tokens))
            self._doc_freq.update(set(tokens))
        self._avg_len = (
            sum(len(tokens) for tokens in self._tokens) / len(self._tokens)
            if self._tokens
            else 0.0
        )
        self._total = len(self._tokens)

    def _is_active(self, chunk: PolicyChunk) -> bool:
        return chunk.status == "current" and chunk.effective_date <= self.app_today

    def _is_future(self, chunk: PolicyChunk) -> bool:
        return chunk.status == "current" and chunk.effective_date > self.app_today

    def _idf(self, term: str) -> float:
        df = self._doc_freq.get(term, 0)
        return math.log((self._total + 1) / (df + 1)) + 1.0

    def _score(self, query_terms: list[str], index: int, query_text: str) -> float:
        tf = self._term_freqs[index]
        length = len(self._tokens[index]) or 1
        k1, b = 1.2, 0.75
        score = 0.0
        for term in query_terms:
            freq = tf.get(term, 0)
            if not freq:
                continue
            denom = freq + k1 * (1 - b + b * length / (self._avg_len or 1))
            score += self._idf(term) * (freq * (k1 + 1)) / denom
        phrase = query_text.strip().lower()
        if len(phrase) >= 6 and phrase in self.chunks[index].text.lower():
            score += 3.0
        # The brief says a policy governs when it and the FAQ differ. A small
        # preference keeps the authoritative policy on top when both cover the
        # query, while exact FAQ answers (for example payment methods, which no
        # policy covers) still win on relevance.
        if self.chunks[index].doc_type == "policy":
            score *= 1.15
        return score

    def _search(self, query: str, limit: int, predicate) -> list[ScoredChunk]:
        terms = tokenize(query)
        if not terms:
            return []
        query_set = set(terms)
        min_matches = max(1, math.ceil(0.55 * len(query_set)))
        phrase = query.strip().lower()
        scored: list[ScoredChunk] = []
        for index, chunk in enumerate(self.chunks):
            if not predicate(chunk):
                continue
            chunk_terms = set(self._tokens[index])
            matched = len(query_set & chunk_terms)
            exact = len(phrase) >= 6 and phrase in chunk.text.lower()
            # Reject weak one-word coincidences ("gift wrapping" matching a
            # gift-card paragraph). A phrase hit always counts as relevant.
            if matched < min_matches and not exact:
                continue
            score = self._score(terms, index, query)
            if score > 0:
                scored.append(ScoredChunk(chunk, round(score, 4)))
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:limit]

    def search(self, query: str, limit: int = 3) -> list[ScoredChunk]:
        """Only current documents effective on APP_TODAY."""
        return self._search(query, limit, self._is_active)

    def search_future(self, query: str, limit: int = 2) -> list[ScoredChunk]:
        """Current documents that start after APP_TODAY."""
        return self._search(query, limit, self._is_future)

    def active_doc_ids(self) -> set[str]:
        return {chunk.doc_id for chunk in self.chunks if self._is_active(chunk)}

    def __len__(self) -> int:
        return len(self.chunks)
