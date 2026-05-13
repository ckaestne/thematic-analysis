"""Document loaders for various input formats.

Supports loading text from:
- PDF files
- Text files (.txt, .md)
- Directories of documents
- Direct text input

Each document is split into segments suitable for thematic analysis.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DataSegment:
    """A segment of data to be coded."""

    segment_id: str
    text: str


@dataclass
class DocumentMetadata:
    """Metadata about a loaded document."""

    filename: str
    filepath: str
    page_count: int = 0
    word_count: int = 0
    char_count: int = 0


@dataclass
class LoadedDocument:
    """A document with its text content and metadata."""

    text: str
    metadata: DocumentMetadata
    segments: list[DataSegment] = field(default_factory=list)

    def segment(
        self,
        method: str = "paragraph",
        min_words: int = 20,
        max_words: int = 500,
    ) -> list[DataSegment]:
        """Split document into segments for analysis.

        Args:
            method: Segmentation method ('paragraph', 'sentence', 'fixed')
            min_words: Minimum words per segment
            max_words: Maximum words per segment (for fixed method)

        Returns:
            List of DataSegment objects
        """
        if method == "paragraph":
            self.segments = _segment_by_paragraph(
                self.text, self.metadata.filename, min_words
            )
        elif method == "sentence":
            self.segments = _segment_by_sentence(
                self.text, self.metadata.filename, min_words
            )
        elif method == "fixed":
            self.segments = _segment_fixed_size(
                self.text, self.metadata.filename, max_words
            )
        else:
            raise ValueError(f"Unknown segmentation method: {method}")

        return self.segments


def _segment_by_paragraph(
    text: str, doc_id: str, min_words: int = 20
) -> list[DataSegment]:
    """Split text by paragraphs (double newlines)."""
    # Split on double newlines or multiple newlines
    paragraphs = re.split(r"\n\s*\n", text)
    segments = []

    for i, para in enumerate(paragraphs):
        para = para.strip()
        word_count = len(para.split())

        # Skip very short paragraphs
        if word_count < min_words:
            continue

        segment_id = f"{doc_id}_p{i + 1}"
        segments.append(DataSegment(segment_id=segment_id, text=para))

    return segments


def _segment_by_sentence(
    text: str, doc_id: str, min_words: int = 20
) -> list[DataSegment]:
    """Split text by sentences, grouping short ones together."""
    # Simple sentence splitting (handles ., !, ?)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    segments = []
    current_segment = []
    current_words = 0
    segment_num = 1

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        word_count = len(sentence.split())
        current_segment.append(sentence)
        current_words += word_count

        if current_words >= min_words:
            segment_text = " ".join(current_segment)
            segment_id = f"{doc_id}_s{segment_num}"
            segments.append(DataSegment(segment_id=segment_id, text=segment_text))
            current_segment = []
            current_words = 0
            segment_num += 1

    # Don't forget leftover
    if current_segment and current_words >= min_words // 2:
        segment_text = " ".join(current_segment)
        segment_id = f"{doc_id}_s{segment_num}"
        segments.append(DataSegment(segment_id=segment_id, text=segment_text))

    return segments


def _segment_fixed_size(
    text: str, doc_id: str, max_words: int = 500
) -> list[DataSegment]:
    """Split text into fixed-size chunks by word count."""
    words = text.split()
    segments = []
    segment_num = 1

    for i in range(0, len(words), max_words):
        chunk_words = words[i : i + max_words]
        segment_text = " ".join(chunk_words)
        segment_id = f"{doc_id}_c{segment_num}"
        segments.append(DataSegment(segment_id=segment_id, text=segment_text))
        segment_num += 1

    return segments


def load_pdf(path: str | Path) -> LoadedDocument:
    """Load text content from a PDF file.

    Args:
        path: Path to PDF file

    Returns:
        LoadedDocument with extracted text

    Raises:
        FileNotFoundError: If file doesn't exist
        ImportError: If pymupdf is not installed
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        raise ImportError(
            "pymupdf is required for PDF support. Install with: pip install pymupdf"
        )

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    doc = fitz.open(path)
    text_parts = []

    for page in doc:
        text_parts.append(page.get_text())

    text = "\n\n".join(text_parts)

    # Clean up common PDF artifacts
    text = _clean_pdf_text(text)

    metadata = DocumentMetadata(
        filename=path.stem,
        filepath=str(path),
        page_count=len(doc),
        word_count=len(text.split()),
        char_count=len(text),
    )

    doc.close()
    return LoadedDocument(text=text, metadata=metadata)


def _clean_pdf_text(text: str) -> str:
    """Clean common PDF extraction artifacts."""
    # Remove excessive whitespace
    text = re.sub(r" +", " ", text)
    # Remove page numbers (common patterns)
    text = re.sub(r"\n\s*\d+\s*\n", "\n", text)
    # Fix hyphenation at line breaks
    text = re.sub(r"-\n", "", text)
    # Normalize newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_text_file(path: str | Path) -> LoadedDocument:
    """Load text from a plain text or markdown file.

    Args:
        path: Path to text file

    Returns:
        LoadedDocument with text content
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    text = path.read_text(encoding="utf-8")

    metadata = DocumentMetadata(
        filename=path.stem,
        filepath=str(path),
        page_count=1,
        word_count=len(text.split()),
        char_count=len(text),
    )

    return LoadedDocument(text=text, metadata=metadata)


def load_document(path: str | Path) -> LoadedDocument:
    """Load a document, auto-detecting file type.

    Args:
        path: Path to document file

    Returns:
        LoadedDocument with text content
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return load_pdf(path)
    elif suffix in (".txt", ".md", ".text"):
        return load_text_file(path)
    else:
        # Try as text file
        return load_text_file(path)


def load_directory(
    directory: str | Path,
    pattern: str = "*.pdf",
    recursive: bool = False,
) -> list[LoadedDocument]:
    """Load all matching documents from a directory.

    Args:
        directory: Path to directory
        pattern: Glob pattern for files (default: "*.pdf")
        recursive: Whether to search subdirectories

    Returns:
        List of LoadedDocument objects
    """
    directory = Path(directory)
    if not directory.exists():
        raise FileNotFoundError(f"Directory not found: {directory}")

    if not directory.is_dir():
        raise ValueError(f"Not a directory: {directory}")

    if recursive:
        files = sorted(directory.rglob(pattern))
    else:
        files = sorted(directory.glob(pattern))

    documents = []
    for filepath in files:
        try:
            doc = load_document(filepath)
            documents.append(doc)
        except Exception as e:
            print(f"Warning: Failed to load {filepath}: {e}")

    return documents


def documents_to_segments(
    documents: list[LoadedDocument],
    method: str = "paragraph",
    min_words: int = 20,
    max_words: int = 500,
) -> list[DataSegment]:
    """Convert loaded documents to segments for analysis.

    Args:
        documents: List of loaded documents
        method: Segmentation method
        min_words: Minimum words per segment
        max_words: Maximum words for fixed-size segments

    Returns:
        Combined list of DataSegment objects from all documents
    """
    all_segments = []

    for doc in documents:
        segments = doc.segment(method=method, min_words=min_words, max_words=max_words)
        all_segments.extend(segments)

    return all_segments
