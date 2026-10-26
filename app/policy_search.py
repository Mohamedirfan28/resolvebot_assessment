"""Policy retrieval: split policies.md into documents and paragraphs, filter by
manifest status/effective date, then rank paragraphs with a small BM25 index.

Only current documents that are effective on APP_TODAY are returned by
`search()`. Future documents are available separately for "this changes on ..."
notes. Superseded documents are never returned.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .util import parse_date

TOP_LEVEL_RE = re.compile(r"^#\s+(?!#)(.*)$")
SECTION_RE = re.compile(r"^###\s+§\s*([0-9]+(?:\.[0-9]+)*)\s*(.*)$")
SUBSECTION_RE = re.compile(r"^##\s+§\s*([0-9]+(?:\.[0-9]+)*)\s*(.*)$")
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
    "please", "tell", "want", "need", "know", "shopsphere",
}


def _stem(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ss"):
        return word
    if word.endswith("s") and len(word) > 3:
        return word[:-1]
    if word.endswith("ing") and len(word) > 5:
        return word[:-3]
    return word


def tokenize(text: str) -> list[str]:
    return [
        _stem(word)
        for word in re.findall(r"[a-z0-9]+", (text or "").lower())
        if word not in STOPWORDS and len(word) > 1
    ]


@dataclass
class PolicyChunk:
    doc_id: str
    title: str
    section: str
    heading: str
    text: str
    effective_date: date
    status: str

    @property
    def label(self) -> str:
        return f"{self.title} {self.section}".strip()

    def citation(self) -> dict:
        return {"doc_id": self.doc_id, "section": self.section}


@dataclass
class ScoredChunk:
    chunk: PolicyChunk
    score: float


class PolicyIndex:
    def __init__(self, policies_path: Path, manifest_path: Path, app_today: date):
        self.app_today = app_today
        self.manifest = self._load_manifest(manifest_path)
        self.chunks = self._parse(policies_path)
        self._tokens: list[list[str]] = []
        self._term_freqs: list[Counter] = []
        self._build_index()

    # ------------------------------------------------------------------ parse
    @staticmethod
    def _load_manifest(path: Path) -> dict[str, dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read manifest {path}: {exc}") from exc
        return {entry["doc_id"]: entry for entry in data if "doc_id" in entry}

    def _parse(self, path: Path) -> list[PolicyChunk]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Could not read policies {path}: {exc}") from exc

        documents = self._split_documents(raw)
        chunks: list[PolicyChunk] = []
        for doc in documents:
            body = "\n".join(doc["lines"])
            doc_id_match = DOC_ID_RE.search(body)
            if not doc_id_match:
                continue
            doc_id = doc_id_match.group(1)

            meta = self.manifest.get(doc_id, {})
            effective = parse_date(meta.get("effective_date"))
            if effective is None:
                match = EFFECTIVE_RE.search(body)
                effective = parse_date(match.group(1)) if match else None
            status = str(meta.get("status") or "").lower()
            if not status:
                match = STATUS_RE.search(body)
                status = match.group(1).lower() if match else "current"
            if effective is None:
                continue

            title = meta.get("title") or doc["title"]
            chunks.extend(self._split_sections(doc_id, title, body, effective, status))
        return chunks

    @staticmethod
    def _split_documents(raw: str) -> list[dict]:
        documents: list[dict] = []
        current: dict | None = None
        for line in raw.splitlines():
            match = TOP_LEVEL_RE.match(line)
            if match:
                if current:
                    documents.append(current)
                current = {"title": match.group(1).strip(), "lines": []}
            elif current is not None:
                current["lines"].append(line)
        if current:
            documents.append(current)
        return documents

    @staticmethod
    def _split_sections(
        doc_id: str, title: str, body: str, effective: date, status: str
    ) -> list[PolicyChunk]:
        lines = body.splitlines()
        chunks: list[PolicyChunk] = []
        current_header: tuple[str, str] | None = None
        buffer: list[str] = []

        def flush() -> None:
            text = "\n".join(buffer).strip()
            if not text:
                return
            section, heading = current_header or ("", "Introduction")
            chunks.append(
                PolicyChunk(
                    doc_id=doc_id,
                    title=title,
                    section=section,
                    heading=heading,
                    text=text,
                    effective_date=effective,
                    status=status,
                )
            )

        for line in lines:
            match = SECTION_RE.match(line) or SUBSECTION_RE.match(line)
            if match:
                flush()
                buffer = []
                current_header = (f"§{match.group(1)}", match.group(2).strip())
            else:
                buffer.append(line)
        flush()
        # The preamble is metadata, not policy text. Keep it only when a
        # document has no numbered sections at all.
        sectioned = [chunk for chunk in chunks if chunk.section]
        return sectioned or chunks

    # ----------------------------------------------------------------- search
    def _build_index(self) -> None:
        for chunk in self.chunks:
            # Title and heading terms count extra so short headings rank well.
            tokens = (
                tokenize(chunk.title) * 2
                + tokenize(chunk.heading) * 2
                + tokenize(chunk.text)
            )
            self._tokens.append(tokens)
            self._term_freqs.append(Counter(tokens))

        self._doc_freq: Counter = Counter()
        for tokens in self._tokens:
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
        return score

    def _search(
        self, query: str, limit: int, predicate
    ) -> list[ScoredChunk]:
        query_terms = tokenize(query)
        if not query_terms:
            return []
        scored: list[ScoredChunk] = []
        for index, chunk in enumerate(self.chunks):
            if not predicate(chunk):
                continue
            score = self._score(query_terms, index, query)
            if self.manifest.get(chunk.doc_id, {}).get("doc_type") == "faq":
                # The FAQ is a short, direct summary; a small nudge helps it win
                # simple "do you..." questions without outranking a specific
                # policy paragraph on a detailed question.
                score *= 1.05
            if score > 0:
                scored.append(ScoredChunk(chunk, round(score, 4)))
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:limit]

    def search(self, query: str, limit: int = 3) -> list[ScoredChunk]:
        """Only documents that are current and effective today."""
        return self._search(query, limit, self._is_active)

    def search_future(self, query: str, limit: int = 2) -> list[ScoredChunk]:
        """Documents that exist but start after APP_TODAY."""
        return self._search(query, limit, self._is_future)
