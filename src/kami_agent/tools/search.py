"""Deterministic keyword search over the read-only ``reference/`` tree (SPEC P10).

Pure python, no new runtime dependency: an Okapi BM25 index built lazily
once per session from the documentation snapshot on disk (D5). Every
parameter below is a pinned artifact of the knowledge-delivery design —
changing one changes what an arm was measured on, so each is a named
constant rather than a call-site number.

Two properties this module owes the family:

- **Determinism.** The same tree and the same query produce a
  byte-identical result. Chunking is a pure function of the file bytes,
  scoring is a pure function of the chunks, and ordering breaks ties on
  ``(path, offset)`` after score — never on iteration or filesystem
  order.
- **Re-readability.** Every hit's ``offset`` and ``length`` are BYTE
  positions in the file it came from, and its ``text`` is exactly the
  decoded bytes of that span (bounded for the result). So
  ``workspace_read(path, offset, length)`` returns the passage the hit
  quoted, which is the same byte-slicing contract truncated reads
  already use (SPEC I16, P11).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# --- pinned parameters -------------------------------------------------------

# Which files are indexed. Anything else in the tree is left to
# workspace_list / workspace_read.
INDEXED_SUFFIXES = (".md", ".markdown", ".txt", ".csv")

# Chunking: paragraphs packed up to this many bytes; a single paragraph
# longer than it is split on a whitespace boundary near the limit.
CHUNK_MAX_BYTES = 1200
# CSV chunks are whole rows, so a hit's span is always re-readable as
# rows. The first chunk of a CSV starts at byte 0 and therefore carries
# the header line; later chunks are rows only.
CSV_ROWS_PER_CHUNK = 20

# How much of a chunk's text a hit carries. The full span is always
# reachable through workspace_read with the hit's offset and length.
SNIPPET_MAX_CHARS = 600

# Result size: default and the range k is clamped to.
K_DEFAULT = 5
K_MIN = 1
K_MAX = 10

# Okapi BM25.
BM25_K1 = 1.5
BM25_B = 0.75

# Lowercase word characters, digits included. No stemming, no stop-word
# list: both would be authored knowledge about the corpus.
_TOKEN = re.compile(r"[a-z0-9]+")

# A blank line, tolerating CR: the paragraph separator.
_PARAGRAPH_BREAK = re.compile(rb"\n[ \t\r]*\n")

# When a too-long paragraph must be cut, prefer a whitespace boundary
# within this many bytes of the limit; otherwise cut at the limit.
_CUT_BACKTRACK_BYTES = 200


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, in order."""
    return _TOKEN.findall(text.lower())


@dataclass(frozen=True, slots=True)
class Chunk:
    """One indexed passage: a byte span of one file, and its tokens.

    ``path`` is workspace-root-relative (``reference/...``), which is
    what ``workspace_read`` accepts.
    """

    path: str
    offset: int
    length: int
    text: str
    tf: Counter[str]
    tokens: int


class ReferenceIndex:
    """A BM25 index over one ``reference/`` tree, built once per session."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self.df: Counter[str] = Counter()
        for chunk in chunks:
            self.df.update(chunk.tf.keys())
        total = sum(chunk.tokens for chunk in chunks)
        self.avg_tokens = (total / len(chunks)) if chunks else 0.0

    # --- construction --------------------------------------------------------

    @classmethod
    def build(cls, reference_root: Path) -> ReferenceIndex:
        """Index every indexable file under ``reference_root``.

        Files are visited in sorted path order so chunk order — and with
        it every tie-break below — is a function of the tree, not of the
        filesystem's iteration order.
        """
        chunks: list[Chunk] = []
        if not reference_root.is_dir():
            return cls(chunks)
        for file in sorted(p for p in reference_root.rglob("*") if p.is_file()):
            if file.suffix.lower() not in INDEXED_SUFFIXES:
                continue
            try:
                data = file.read_bytes()
            except OSError:
                continue
            rel = "reference/" + file.relative_to(reference_root).as_posix()
            spans = _csv_spans(data) if file.suffix.lower() == ".csv" else _paragraph_spans(data)
            for offset, length in spans:
                text = data[offset : offset + length].decode("utf-8", errors="replace")
                tokens = tokenize(text)
                if not tokens:
                    continue
                chunks.append(
                    Chunk(
                        path=rel,
                        offset=offset,
                        length=length,
                        text=text,
                        tf=Counter(tokens),
                        tokens=len(tokens),
                    )
                )
        return cls(chunks)

    # --- query ---------------------------------------------------------------

    def search(self, query: str, k: int = K_DEFAULT) -> list[dict[str, object]]:
        """Top-``k`` hits for ``query``, highest score first.

        Ties are broken by path then offset, so the result depends on
        nothing but the tree and the query.
        """
        terms = tokenize(query)
        if not terms or not self.chunks:
            return []
        n = len(self.chunks)
        idf = {
            term: math.log(1 + (n - self.df[term] + 0.5) / (self.df[term] + 0.5))
            for term in set(terms)
            if self.df[term]
        }
        if not idf:
            return []
        scored: list[tuple[float, str, int, Chunk]] = []
        for chunk in self.chunks:
            score = 0.0
            norm = BM25_K1 * (
                1 - BM25_B + BM25_B * (chunk.tokens / self.avg_tokens if self.avg_tokens else 1.0)
            )
            for term, weight in idf.items():
                tf = chunk.tf.get(term, 0)
                if tf:
                    score += weight * (tf * (BM25_K1 + 1)) / (tf + norm)
            if score > 0:
                scored.append((score, chunk.path, chunk.offset, chunk))
        # Highest score first; ties by path, then by offset.
        scored.sort(key=lambda row: (-row[0], row[1], row[2]))
        return [
            {
                "path": chunk.path,
                "offset": chunk.offset,
                "length": chunk.length,
                "text": chunk.text[:SNIPPET_MAX_CHARS],
            }
            for _score, _path, _offset, chunk in scored[:k]
        ]


def clamp_k(k: int | None) -> int:
    """``k`` clamped to the allowed range; None → the default."""
    if k is None:
        return K_DEFAULT
    return min(max(int(k), K_MIN), K_MAX)


# --- chunking ------------------------------------------------------------------


def _paragraph_spans(data: bytes) -> list[tuple[int, int]]:
    """Byte spans of paragraphs, packed up to ``CHUNK_MAX_BYTES``.

    A packed span runs from the first paragraph's start to the last
    paragraph's end, separators included, so the span is contiguous and
    re-readable as one slice.
    """
    paragraphs = _raw_paragraphs(data)
    spans: list[tuple[int, int]] = []
    start: int | None = None
    end = 0
    for p_start, p_end in paragraphs:
        if start is None:
            start, end = p_start, p_end
            continue
        if p_end - start <= CHUNK_MAX_BYTES:
            end = p_end
            continue
        spans.append((start, end - start))
        start, end = p_start, p_end
    if start is not None:
        spans.append((start, end - start))
    return [span for span in _split_oversized(data, spans)]


def _raw_paragraphs(data: bytes) -> list[tuple[int, int]]:
    """(start, end) of each paragraph, whitespace-trimmed, in order."""
    out: list[tuple[int, int]] = []
    position = 0
    n = len(data)
    while position < n:
        match = _PARAGRAPH_BREAK.search(data, position)
        end = match.start() if match else n
        start, stop = _trim(data, position, end)
        if stop > start:
            out.append((start, stop))
        position = match.end() if match else n
    return out


def _trim(data: bytes, start: int, end: int) -> tuple[int, int]:
    while start < end and data[start : start + 1].isspace():
        start += 1
    while end > start and data[end - 1 : end].isspace():
        end -= 1
    return start, end


def _split_oversized(data: bytes, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Cut any span longer than the limit at a whitespace boundary near it."""
    out: list[tuple[int, int]] = []
    for offset, length in spans:
        while length > CHUNK_MAX_BYTES:
            cut = CHUNK_MAX_BYTES
            limit = offset + CHUNK_MAX_BYTES
            window = data[limit - _CUT_BACKTRACK_BYTES : limit]
            index = max(window.rfind(b" "), window.rfind(b"\n"))
            if index != -1:
                cut = CHUNK_MAX_BYTES - _CUT_BACKTRACK_BYTES + index + 1
            out.append((offset, cut))
            offset += cut
            length -= cut
        if length > 0:
            out.append((offset, length))
    return out


def _csv_spans(data: bytes) -> list[tuple[int, int]]:
    """Byte spans of row groups: the first group carries the header line."""
    lines: list[tuple[int, int]] = []
    position = 0
    n = len(data)
    while position < n:
        newline = data.find(b"\n", position)
        end = n if newline == -1 else newline
        if end > position:
            lines.append((position, end))
        position = end + 1
    if not lines:
        return []
    spans: list[tuple[int, int]] = []
    # The header rides with the first group: rows after it come in groups
    # of CSV_ROWS_PER_CHUNK, so every span is whole rows.
    group: list[tuple[int, int]] = [lines[0]]
    for line in lines[1:]:
        if len(group) >= CSV_ROWS_PER_CHUNK + (1 if not spans else 0):
            spans.append((group[0][0], group[-1][1] - group[0][0]))
            group = []
        group.append(line)
    if group:
        spans.append((group[0][0], group[-1][1] - group[0][0]))
    return spans
