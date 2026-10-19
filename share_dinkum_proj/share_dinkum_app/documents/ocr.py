"""Read the text of a scanned PDF by OCR, on this machine.

RapidOCR runs its bundled models through onnxruntime on the CPU, so nothing leaves the machine. It
is the optional extra `ocr` (`uv sync --inexact --extra ocr`). OCR can misread a letter or a digit,
which is why a document read this way is marked `scanned`.

Each page is rendered at `DPI` and read into boxes of text, which are put back into lines: boxes
whose middles fall within one another's height share a line, left to right. Boxes on a line are
joined by one space, or by two where the gap between them is wide, as between columns.
"""

import functools
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Above about 250, the default models start dropping the spaces between words.
DPI = 200

# A gap wider than this share of the text's height is a column break.
COLUMN_GAP = 0.6


@dataclass(frozen=True)
class Box:
    """A piece of text OCR found, and where, in pixels."""

    left: float
    top: float
    right: float
    bottom: float
    text: str

    @property
    def middle(self) -> float:
        return (self.top + self.bottom) / 2


def available() -> bool:
    """Whether the OCR extra is installed."""
    return all(importlib.util.find_spec(name) is not None for name in ('rapidocr', 'onnxruntime', 'pypdfium2'))


def pdf_pages(path: Path) -> list[str]:
    """The text of each page of the PDF at `path`, read by OCR."""
    import pypdfium2

    pdf = pypdfium2.PdfDocument(path)
    try:
        return [page_text(_boxes(page.render(scale=DPI / 72).to_pil())) for page in pdf]
    finally:
        pdf.close()


def page_text(boxes: list[Box]) -> str:
    """The boxes put back into lines of text, top to bottom."""
    lines: list[list[Box]] = []
    for box in sorted(boxes, key=lambda box: box.middle):
        first = lines[-1][0] if lines else None
        if first is not None and first.top <= box.middle <= first.bottom:
            lines[-1].append(box)
        else:
            lines.append([box])
    return '\n'.join(_line_text(sorted(line, key=lambda box: box.left)) for line in lines)


def _line_text(line: list[Box]) -> str:
    text = line[0].text
    for before, box in zip(line, line[1:]):
        height = (before.bottom - before.top + box.bottom - box.top) / 2
        text += ('  ' if box.left - before.right > COLUMN_GAP * height else ' ') + box.text
    return text


def _boxes(image: Any) -> list[Box]:
    result = _engine()(image)
    if result.boxes is None or result.txts is None:
        return []
    boxes = []
    for corners, text in zip(result.boxes, result.txts):
        xs = [float(x) for x, _ in corners]
        ys = [float(y) for _, y in corners]
        boxes.append(Box(min(xs), min(ys), max(xs), max(ys), str(text)))
    return boxes


@functools.cache
def _engine() -> Any:
    from rapidocr import RapidOCR

    # The default limit shrinks a page rendered at DPI, which costs accuracy.
    return RapidOCR(params={'Global.max_side_len': 4000, 'Global.log_level': 'error'})
