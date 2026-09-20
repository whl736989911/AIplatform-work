"""Local ONNX embedding as a platform model revision (B-11).

A local ONNX embedding model is not a side channel: it enters the platform model
catalog as one *kind* of platform model revision — ``adapter_key='onnx'`` with the
local ONNX model id in ``model_key``, declaring ``embedding_dimensions`` and
``local_model_id`` (a downloaded model under ``~/.octop/embedding_models``, never
a free path). Administrators authorize it for a tenant, a department, or one
member through the same capability-grant path every other revision takes, and it
is auditable by construction: publishing writes a new immutable revision row,
revocation only flips ``status``/``revoked_at``, and no row is ever deleted.

This module is the embedding *runtime* behind such a revision: an
``EmbeddingHook`` that reports the declaration and embeds locally through the
personal edition's ONNX service (``octop.infra.agents.providers.onnx_service``,
reused read-only — the download/activation lifecycle stays its own admin action).

Fail closed, always:

* ``available()`` is false and ``embed()`` raises ``MODEL_NOT_CONFIGURED`` when
  the local runtime is not importable or the model is not downloaded;
* nothing is installed and nothing is downloaded here. Fetching a model remains
  the personal edition's explicit admin action; a WorkBuddy request may only use
  what is already on disk, so a missing model can never turn a request into an
  outbound download;
* a vector of another width, a non-finite value, an all-zero vector, or a count
  that does not match the request is refused rather than stored.

Only the storage layer's width (``VECTOR_DIMENSIONS``, 1024) is usable today:
stored embeddings live in a fixed-width vector column, so a revision declaring
another width could be published but never embedded into a knowledge base.
Supporting another width requires the vector column and its migrations to change
first.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from octop.infra.agents.providers import onnx_service
from octop.infra.db.repos.workbuddy_catalog import ADAPTER_KEY_ONNX
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy.knowledge import (
    VECTOR_DIMENSIONS,
    EmbeddingDescriptor,
    validate_embedding_vector,
)

__all__ = ["OnnxEmbeddingHook"]


@dataclass(frozen=True, slots=True)
class OnnxEmbeddingHook:
    """The local ONNX embedder behind one catalogued platform model revision.

    Configure it with the model id, revision and declared width of the published
    revision it serves. The knowledge service compares this descriptor with the
    knowledge base's own pin on every use, so a hook configured for a different
    local model (or a different revision) never matches and fails closed instead
    of embedding a base with the wrong model.
    """

    model_id: str
    revision: int = 1
    dimensions: int = VECTOR_DIMENSIONS

    def available(self) -> bool:
        """True only when the local runtime and this model's weights are present."""
        if not self.model_id:
            return False
        try:
            if not onnx_service.local_embedding_deps_available():
                return False
            return bool(onnx_service.is_model_downloaded(self.model_id))
        except Exception:  # a probe that raises is not proof of availability
            return False

    def describe(self) -> EmbeddingDescriptor | None:
        if not self.available():
            return None
        return EmbeddingDescriptor(
            adapter_key=ADAPTER_KEY_ONNX,
            model_key=self.model_id,
            revision=int(self.revision),
            dimensions=int(self.dimensions),
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed locally, or refuse: no fallback model, no download, no partial batch."""
        if not self.available():
            raise OctopError(
                ErrorCode.MODEL_NOT_CONFIGURED,
                f"local ONNX embedding model {self.model_id} is not downloaded or its"
                " runtime is not installed",
            )
        try:
            vectors = onnx_service.embed_texts(self.model_id, list(texts))
        except Exception as exc:
            raise OctopError(
                ErrorCode.MODEL_NOT_CONFIGURED,
                f"local ONNX embedding model {self.model_id} could not embed the request",
            ) from exc
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise OctopError(
                ErrorCode.MODEL_NOT_CONFIGURED,
                "the local ONNX embedder returned a different number of vectors than texts",
            )
        return [validate_embedding_vector(vector, dimensions=self.dimensions) for vector in vectors]
