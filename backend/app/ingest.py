"""Document parsing and structure-aware chunking."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass
class Chunk:
    ord: int
    heading: str
    text: str


class UnsupportedFile(ValueError):
    pass


# --- parsing --------------------------------------------------------------------------


class _HTMLText(HTMLParser):
    _BLOCK = {"p", "div", "li", "br", "tr", "section", "article"}
    _SKIP = {"script", "style", "noscript"}

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in self._BLOCK:
            self.out.append("\n\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def extract_text(filename: str, data: bytes) -> str:
    """Normalise every supported format to markdown-ish text (headings as '#' lines)."""
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    if name.endswith((".html", ".htm")):
        parser = _HTMLText()
        parser.feed(data.decode("utf-8", errors="replace"))
        return "".join(parser.out)
    if name.endswith((".md", ".markdown", ".txt")):
        return data.decode("utf-8", errors="replace")
    raise UnsupportedFile(f"Unsupported file type: {filename} (use .pdf, .md, .txt or .html)")


# --- chunking -------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


def _words(text: str) -> int:
    return len(text.split())


def _sections(text: str, title: str) -> list[tuple[str, str]]:
    """Split on markdown headings, tracking the full heading path (e.g. 'Policy > Travel')."""
    stack: list[tuple[int, str]] = []  # (level, heading) of the open headings
    sections: list[tuple[str, str]] = []
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            sections.append((" > ".join([title] + [h for _, h in stack]), body))
        buf.clear()

    for line in text.splitlines():
        m = _HEADING.match(line.strip())
        if m:
            flush()
            level, heading = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            if not (level == 1 and heading.lower() == title.lower()):  # the doc's own H1 is the root
                stack.append((level, heading))
        else:
            buf.append(line)
    flush()
    return sections


def chunk_document(text: str, title: str, max_words: int = 180, overlap_words: int = 30) -> list[Chunk]:
    """Pack paragraphs (then sentences, for oversized paragraphs) into ~max_words chunks.

    Chunks never cross a heading boundary, and each chunk carries a sentence-aligned tail of the
    previous chunk so facts that straddle a boundary stay retrievable.
    """
    chunks: list[Chunk] = []
    for heading, body in _sections(text, title):
        units: list[str] = []
        for para in re.split(r"\n\s*\n", body):
            para = " ".join(para.split())
            if not para:
                continue
            if _words(para) <= max_words:
                units.append(para)
            else:
                units.extend(s for s in _SENTENCE.split(para) if s)

        current: list[str] = []
        for unit in units:
            if current and _words(" ".join(current + [unit])) > max_words:
                chunks.append(Chunk(len(chunks), heading, " ".join(current)))
                tail: list[str] = []
                for sent in reversed(_SENTENCE.split(" ".join(current))):
                    if _words(" ".join([sent] + tail)) > overlap_words:
                        break
                    tail.insert(0, sent)
                current = tail
            current.append(unit)
        if current:
            chunks.append(Chunk(len(chunks), heading, " ".join(current)))
    return chunks
