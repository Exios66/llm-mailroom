import os
import structlog
from typing import Optional

logger = structlog.get_logger(__name__)

_langfuse_client = None


def get_langfuse_client():
    global _langfuse_client
    if _langfuse_client is not None:
        return _langfuse_client

    try:
        from langfuse import Langfuse
        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local")
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local")
        host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")

        _langfuse_client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
        logger.info("langfuse_initialized", host=host)
    except Exception:
        logger.warning("langfuse_unavailable")
        _langfuse_client = _NoopLangfuse()

    return _langfuse_client


class _NoopLangfuse:
    def trace(self, *args, **kwargs):
        return _NoopTrace()

    def flush(self):
        pass


class _NoopTrace:
    def span(self, *args, **kwargs):
        return _NoopSpan()

    def update(self, *args, **kwargs):
        pass


class _NoopSpan:
    def end(self, *args, **kwargs):
        pass

    def update(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        pass


def create_trace(name: str, trace_id: str | None = None, metadata: dict | None = None):
    client = get_langfuse_client()
    kwargs = {"name": name}
    if trace_id:
        kwargs["id"] = trace_id
    if metadata:
        kwargs["metadata"] = metadata
    return client.trace(**kwargs)


def flush_langfuse():
    client = get_langfuse_client()
    try:
        client.flush()
    except Exception:
        pass
