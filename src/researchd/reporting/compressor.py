"""Report compression: model compression with deterministic fallback
(IMPLEMENTATION.md §21.4).

The compressor may ONLY shorten wording of a validated ReportSpec; it cannot
add content, drop uncertainty, change evidence refs, decide buttons, or alter
the message type. When the model fails (schema violation, timeout, or gated),
the deterministic template is used instead.
"""

from __future__ import annotations

from ..domain.report import ReportSpec
from .spec import render_text

MAX_COMPRESSED_CHARS = 4000


class CompressionResult:
    def __init__(self, body: str, source: str):
        self.body = body
        self.source = source  # "model" | "template"

    def as_dict(self) -> dict:
        return {"body": self.body, "source": self.source}


async def compress_report(
    spec: ReportSpec,
    *,
    compressor=None,  # async callable(spec_text) -> str (model compression)
    allow_model: bool = True,
) -> CompressionResult:
    """Compress a validated spec. Falls back to the deterministic template on
    any failure or when model compression is unavailable."""
    template_body = render_text(spec)
    if not allow_model or compressor is None:
        return CompressionResult(body=template_body, source="template")
    try:
        compressed = await compressor(template_body)
        if not isinstance(compressed, str) or not compressed.strip():
            raise ValueError("empty compression")
        if len(compressed) > MAX_COMPRESSED_CHARS:
            raise ValueError("compressed body too long")
        # the compressor may only shorten: never expand beyond the template
        if len(compressed) > len(template_body) * 1.5:
            raise ValueError("compression expanded the body")
        return CompressionResult(body=compressed, source="model")
    except Exception:  # noqa: BLE001
        return CompressionResult(body=template_body, source="template")
