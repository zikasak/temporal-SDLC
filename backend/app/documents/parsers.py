"""Small parser registry for the document types accepted by the API."""

from pathlib import Path
from typing import Callable
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from app.documents.errors import DocumentParseError

MAX_EXTRACTED_CHARACTERS = 2_000_000


def _check_length(text: str) -> str:
    if len(text) > MAX_EXTRACTED_CHARACTERS:
        raise DocumentParseError("The document contains too much text to process.")
    if not text.strip():
        raise DocumentParseError("The document contains no extractable text.")
    return text


def parse_pdf(path: Path) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise DocumentParseError("Password-protected PDFs are not supported.")
        parts: list[str] = []
        length = 0
        for number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                part = f"[Page {number}]\n{text.strip()}"
                length += len(part) + 2
                if length > MAX_EXTRACTED_CHARACTERS:
                    raise DocumentParseError("The document contains too much text to process.")
                parts.append(part)
        return _check_length("\n\n".join(parts))
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError("The PDF could not be read.") from exc


def parse_docx(path: Path) -> str:
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with ZipFile(path) as archive:
            if archive.getinfo("word/document.xml").file_size > 32 * 1024 * 1024:
                raise DocumentParseError("The DOCX document is too large to process.")
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        parts: list[str] = []
        length = 0
        for paragraph in root.iter(f"{namespace}p"):
            pieces: list[str] = []
            for element in paragraph.iter():
                if element.tag == f"{namespace}t" and element.text:
                    pieces.append(element.text)
                elif element.tag == f"{namespace}tab":
                    pieces.append("\t")
                elif element.tag == f"{namespace}br":
                    pieces.append("\n")
            text = "".join(pieces).strip()
            if text:
                length += len(text) + 1
                if length > MAX_EXTRACTED_CHARACTERS:
                    raise DocumentParseError("The document contains too much text to process.")
                parts.append(text)
        return _check_length("\n".join(parts))
    except DocumentParseError:
        raise
    except (BadZipFile, KeyError, ElementTree.ParseError, OSError) as exc:
        raise DocumentParseError("The DOCX file could not be read.") from exc


def parse_txt(path: Path) -> str:
    try:
        return _check_length(path.read_text(encoding="utf-8-sig"))
    except UnicodeError as exc:
        raise DocumentParseError("The text file must use UTF-8 encoding.") from exc


PARSERS: dict[str, tuple[str, Callable[[Path], str]]] = {
    ".pdf": ("application/pdf", parse_pdf),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        parse_docx,
    ),
    ".txt": ("text/plain", parse_txt),
}


def document_type(filename: str) -> tuple[str, Callable[[Path], str]]:
    try:
        return PARSERS[Path(filename).suffix.lower()]
    except KeyError as exc:
        raise DocumentParseError("Supported file types are PDF, DOCX, and TXT.") from exc
