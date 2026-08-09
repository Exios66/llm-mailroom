"""Vision-capable model helpers for the mailroom pipeline.

Some input agents (e.g. Qwen via OpenRouter) can read a PDF page-by-page as
images, so the pipeline renders the first `vision.max_pages` pages of a PDF to
image data-URIs and lets the classified/extraction prompt include them directly
— instead of relying solely on text transcription. The transcription still runs
and is stored as `doc_text` for text-only agents, the judge, reports, and
auditability; vision only changes what the LLM sees.

Everything here is config-driven (`config/taxonomy.yaml` -> `vision:`). If the
render backend (PyMuPDF) is unavailable, we degrade gracefully to empty page
lists and the pipeline just behaves like today (text-only).
"""
import base64
import structlog
from pathlib import Path

from pipeline.config import load_config, get_agent_config

logger = structlog.get_logger(__name__)

IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}


def _vision_config() -> dict:
    import os

    cfg = load_config().get("vision", {}) or {}
    # Env overrides let pilots sweep configs without editing taxonomy.yaml:
    #   MAILROOM_VISION_ENABLED=0/1
    #   MAILROOM_VISION_MAX_PAGES=0..N   (0 == render ALL pages)
    #   MAILROOM_VISION_DPI=150
    enabled = cfg.get("enabled", True)
    max_pages = int(cfg.get("max_pages", 10))
    dpi = int(cfg.get("dpi", 150))
    if os.environ.get("MAILROOM_VISION_ENABLED") is not None:
        enabled = os.environ["MAILROOM_VISION_ENABLED"].lower() in ("1", "true", "yes", "on")
    if os.environ.get("MAILROOM_VISION_MAX_PAGES") is not None:
        max_pages = int(os.environ["MAILROOM_VISION_MAX_PAGES"])
    if os.environ.get("MAILROOM_VISION_DPI") is not None:
        dpi = int(os.environ["MAILROOM_VISION_DPI"])
    return {
        "enabled": bool(enabled),
        "max_pages": max_pages,
        "dpi": dpi,
        "models": list(cfg.get("models", []) or []),
    }


def vision_enabled() -> bool:
    return _vision_config()["enabled"]


def max_pages() -> int:
    """Page-image budget per document. 0 means "render ALL pages" (no cap)."""
    return _vision_config()["max_pages"]


def is_vision_capable(model: str) -> bool:
    """True when `model` matches any configured `vision.models` substring.

    Called with an agent's resolved model string (e.g. `qwen/qwen3.7-flash`).
    Case-insensitive substring match so `Qwen/Qwen3.7-flash` and
    `ollama/qwen2.5vl` both match `qwen/`/`qwen-vl` patterns.
    """
    if not vision_enabled():
        return False
    if not model:
        return False
    lowered = model.lower()
    return any(pat.lower() in lowered for pat in _vision_config()["models"])


def agent_uses_vision(agent_name: str) -> bool:
    """Whether a given agent's configured model is vision-capable."""
    try:
        cfg = get_agent_config(agent_name)
        return is_vision_capable(cfg.get("model", ""))
    except Exception:
        logger.warning("vision_agent_config_unavailable", agent=agent_name)
        return False


def pipeline_uses_vision() -> bool:
    """True when the pipeline's document-typing agents are vision-capable.

    Used to skip redundant LLM transcription passes for scanned PDFs: when the
    sorter and specialists can read page images directly, an LLM "reformat the
    raw text to markdown" pass adds no signal (the page images carry the
    content) and just burns tokens. The direct raw-text extraction is still
    stored for text-only paths/audit.
    """
    return agent_uses_vision("sorter") or _any_specialist_uses_vision()


def _any_specialist_uses_vision() -> bool:
    from pipeline.config import get_agent_config, load_config

    cfg = load_config().get("doc_classes", [])
    for cls in cfg:
        spec = cls.get("specialist")
        if not spec:
            continue
        try:
            if is_vision_capable(get_agent_config(spec).get("model", "")):
                return True
        except Exception:
            continue
    return False


def render_pdf_pages(file_path: Path, cap: int | None = None, dpi: int | None = None) -> list[str]:
    """Render pages of a PDF to a list of PNG image data-URIs.

    `cap` is the page budget: 0 or None renders **all** pages (no content is
    ever dropped by the page cap — the document's later pages stay available to
    vision models). Pass an explicit positive cap to limit the image budget
    (e.g. a sweep comparing cost/accuracy tradeoffs).

    Returns [] when PyMuPDF is unavailable or the PDF can't be rendered (the
    pipeline then falls back to text-only for both vision and non-vision agents).
    """
    if dpi is None:
        dpi = _vision_config()["dpi"]

    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("pymupdf_missing", hint="pip install pymupdf for vision ingestion")
        return []

    try:
        doc = fitz.open(str(file_path))
    except Exception:
        logger.exception("pdf_open_failed_for_vision", file=str(file_path))
        return []
    try:
        page_count = doc.page_count
        # cap == 0 (or None) means "ALL pages" — never truncate document content.
        limit = page_count if cap is None or cap <= 0 else min(cap, page_count)
        pages: list[str] = []
        for idx in range(limit):
            try:
                page = doc.load_page(idx)
                # Render at the configured DPI (scaled to ~1.5x); text density
                # on legal docs is high, so a crisp raster helps the vision
                # model read clauses, headings and tables. PNG compresses text
                # pages well (JPEG is smaller only for photo-like scans, and
                # adds compression artifacts that hurt OCR-style reading).
                zoom = dpi / 72.0
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
                png = pix.tobytes("png")
                uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
                pages.append(uri)
            except Exception:
                logger.exception("pdf_page_render_failed", page=idx)
        doc.close()
        logger.info(
            "pdf_pages_rendered",
            file=file_path.name,
            pages=len(pages),
            total=page_count,
            cap=(None if cap is None or cap <= 0 else cap),
        )
        return pages
    except Exception:
        logger.exception("pdf_render_failed_for_vision", file=str(file_path))
        return []


def render_image(file_path: Path) -> list[str]:
    """Encode a single image file as a data-URI for direct vision input.

    Image inputs already went through `image_extractor` to get text; when the
    downstream agents are vision-capable we ALSO hand them the raw image so they
    can read tables/layout the text pass may have lost.
    """
    ext = file_path.suffix.lower()
    mime = IMAGE_MIME.get(ext, "image/jpeg")
    try:
        b64 = base64.b64encode(file_path.read_bytes()).decode("ascii")
        return [f"data:{mime};base64,{b64}"]
    except Exception:
        logger.exception("image_encode_failed_for_vision", file=str(file_path))
        return []


def render_document_pages(file_path: Path) -> list[str]:
    """Render an input document to a list of page-image data-URIs (PDFs only;
    real image files pass through as-is). Returns [] when vision is disabled or
    rendering isn't possible.

    Applies the configured `vision.max_pages` budget (0 = ALL pages) so a
    positive cap actually limits the image payload — this is what makes the
    text-only vs vision-N vs vision-all tradeoff measurable.
    """
    if not vision_enabled():
        return []
    ext = file_path.suffix.lower()
    if ext in {".pdf"}:
        return render_pdf_pages(file_path, cap=_vision_config()["max_pages"])
    if ext in IMAGE_MIME:
        return render_image(file_path)
    return []
