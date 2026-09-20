"""The local ONNX embedding hook: what it reports, and when it refuses (B-11).

A local ONNX model is a platform model revision like any other, so the hook that
embeds for it has to be exact about two things: the descriptor it reports has to
be the declaration the published revision carries (the knowledge service compares
them on every use), and it has to fail closed whenever the local runtime or the
downloaded model is not there. No test here touches a real model: the personal
edition's ONNX service is stubbed, and the point is what the hook does with its
answers — including the answers it must not trust.
"""

from __future__ import annotations

from typing import Any

import pytest

from octop.infra.agents.providers import onnx_service
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy.knowledge import VECTOR_DIMENSIONS, EmbeddingHook
from octop.infra.workbuddy.onnx_embedder import OnnxEmbeddingHook

MODEL_ID = "BAAI/bge-small-zh-v1.5"


def _vector(value: float = 0.5) -> list[float]:
    return [value] * VECTOR_DIMENSIONS


def _runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    deps: bool = True,
    downloaded: bool = True,
    embed: Any = None,
) -> list[tuple[str, Any]]:
    """Stub the personal-edition ONNX service; return the recorded embed calls."""
    calls: list[tuple[str, Any]] = []

    def fake_embed(model: str, texts: list[str]) -> Any:
        calls.append((model, texts))
        if callable(embed):
            return embed(model, texts)
        return [list(_vector()) for _ in texts]

    monkeypatch.setattr(onnx_service, "local_embedding_deps_available", lambda: deps)
    monkeypatch.setattr(onnx_service, "is_model_downloaded", lambda model: bool(downloaded))
    monkeypatch.setattr(onnx_service, "embed_texts", fake_embed)
    return calls


def test_the_hook_is_an_embedding_hook() -> None:
    """The service only accepts a real ``EmbeddingHook``."""
    assert isinstance(OnnxEmbeddingHook(model_id=MODEL_ID), EmbeddingHook)


def test_available_needs_both_the_runtime_and_the_downloaded_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half a local embedding stack is not an embedding stack."""
    _runtime(monkeypatch, deps=True, downloaded=True)
    assert OnnxEmbeddingHook(model_id=MODEL_ID).available() is True

    _runtime(monkeypatch, deps=False, downloaded=True)
    assert OnnxEmbeddingHook(model_id=MODEL_ID).available() is False

    _runtime(monkeypatch, deps=True, downloaded=False)
    assert OnnxEmbeddingHook(model_id=MODEL_ID).available() is False

    _runtime(monkeypatch, deps=True, downloaded=True)
    assert OnnxEmbeddingHook(model_id="").available() is False


def test_available_treats_a_raising_probe_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that cannot answer has not proved availability."""

    def boom() -> bool:
        raise RuntimeError("import exploded")

    monkeypatch.setattr(onnx_service, "local_embedding_deps_available", boom)
    assert OnnxEmbeddingHook(model_id=MODEL_ID).available() is False


def test_describe_reports_the_published_declaration(monkeypatch: pytest.MonkeyPatch) -> None:
    """The descriptor is the revision's declaration, not whatever is on disk."""
    _runtime(monkeypatch, deps=True, downloaded=True)
    hook = OnnxEmbeddingHook(model_id=MODEL_ID, revision=4, dimensions=VECTOR_DIMENSIONS)
    descriptor = hook.describe()
    assert descriptor is not None
    assert descriptor.adapter_key == "onnx"
    assert descriptor.model_key == MODEL_ID
    assert descriptor.revision == 4
    assert descriptor.dimensions == VECTOR_DIMENSIONS


def test_describe_reports_nothing_while_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """No local model, no descriptor — the service then refuses to embed."""
    _runtime(monkeypatch, deps=False)
    assert OnnxEmbeddingHook(model_id=MODEL_ID).describe() is None


def test_embed_goes_through_the_local_onnx_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Embedding is the local service's call, with the catalogued model id."""
    calls = _runtime(monkeypatch, deps=True, downloaded=True)
    hook = OnnxEmbeddingHook(model_id=MODEL_ID, revision=2)
    vectors = hook.embed(["a", "b"])
    assert calls == [(MODEL_ID, ["a", "b"])]
    assert len(vectors) == 2
    assert all(len(vector) == VECTOR_DIMENSIONS for vector in vectors)
    assert all(isinstance(value, float) for value in vectors[0])


def test_embed_refuses_without_downloading(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing model fails closed: no install attempt, no download, no vector."""
    calls = _runtime(monkeypatch, deps=True, downloaded=False)
    with pytest.raises(OctopError) as exc:
        OnnxEmbeddingHook(model_id=MODEL_ID).embed(["a"])
    assert exc.value.code is ErrorCode.MODEL_NOT_CONFIGURED
    assert calls == []


def test_embed_refuses_without_the_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing fastembed is the same refusal, never a silent fallback."""
    calls = _runtime(monkeypatch, deps=False)
    with pytest.raises(OctopError) as exc:
        OnnxEmbeddingHook(model_id=MODEL_ID).embed(["a"])
    assert exc.value.code is ErrorCode.MODEL_NOT_CONFIGURED
    assert calls == []


def test_embed_reports_a_runtime_failure_as_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken local runtime is a configuration failure, not an internal error."""

    def fail(model: str, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("onnx session died")

    _runtime(monkeypatch, deps=True, downloaded=True, embed=fail)
    with pytest.raises(OctopError) as exc:
        OnnxEmbeddingHook(model_id=MODEL_ID).embed(["a"])
    assert exc.value.code is ErrorCode.MODEL_NOT_CONFIGURED


def test_embed_rejects_vectors_that_do_not_match_the_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Width, count and finiteness are re-checked here, not trusted downstream."""
    _runtime(
        monkeypatch, deps=True, downloaded=True, embed=lambda model, texts: [[0.5] * 8] * len(texts)
    )
    with pytest.raises(OctopError) as narrow:
        OnnxEmbeddingHook(model_id=MODEL_ID).embed(["a"])
    assert narrow.value.code is ErrorCode.MODEL_NOT_CONFIGURED

    _runtime(monkeypatch, deps=True, downloaded=True, embed=lambda model, texts: [_vector()])
    with pytest.raises(OctopError) as short:
        OnnxEmbeddingHook(model_id=MODEL_ID).embed(["a", "b"])
    assert short.value.code is ErrorCode.MODEL_NOT_CONFIGURED

    non_finite = _vector()
    non_finite[0] = float("nan")
    _runtime(monkeypatch, deps=True, downloaded=True, embed=lambda model, texts: [non_finite])
    with pytest.raises(OctopError) as broken:
        OnnxEmbeddingHook(model_id=MODEL_ID).embed(["a"])
    assert broken.value.code is ErrorCode.MODEL_NOT_CONFIGURED
