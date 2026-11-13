"""内蔵の埋め込み（CPU。src/vector_db/local_embeddings.py・[vector_db] local_embedding_model）のテスト。

設定の切り替え・embed_texts の分岐・プーリングはモデル無しで確かめる。実際のモデル
（models/embedding/granite-embedding-97m-multilingual-r2。tools/download_embedding_model.py で取得）があるときだけ、
ベクターの性質（次元・正規化・バッチと1件ずつの一致・意味の近さ）も確かめる。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from src.config import DEFAULT_LOCAL_EMBEDDING_MODEL, LOCAL_EMBEDDING_PROVIDER, PROJECT_ROOT, load_config, uses_local_embeddings
from src.embedding_usage import EmbeddingMeter
from src.vector_db import embeddings, local_embeddings
from src.vector_db.embeddings import EmbeddingError, embed_images, embed_texts, embedding_model

_EMBED = '[{"base_url": "http://localhost:12440/v1", "api_key": "x", "model": "bge-m3"}]'
REAL_MODEL = PROJECT_ROOT / DEFAULT_LOCAL_EMBEDDING_MODEL


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data" / "${instance}"))
    for key in ("VECTOR_DB_ENABLED", "VECTOR_DB_EMBEDDING_URL", "VECTOR_DB_IMAGE_INPUT", "VECTOR_DB_QUERY_PREFIX", "VECTOR_DB_DOCUMENT_PREFIX"):
        monkeypatch.delenv(key, raising=False)
    local_embeddings.clear_cache()
    yield
    local_embeddings.clear_cache()


def _fake_model_dir(root: Path, name: str = "my-model", onnx: str = "onnx/model.onnx") -> Path:
    folder = root / name
    (folder / onnx).parent.mkdir(parents=True, exist_ok=True)
    (folder / onnx).write_bytes(b"not a real model")
    (folder / "tokenizer.json").write_text("{}", encoding="utf-8")
    return folder


def _load(tmp_path: Path, vector_db: dict, monkeypatch, model_dir: Path | str | None = None):
    if model_dir is not None:
        monkeypatch.setenv("VECTOR_DB_LOCAL_EMBEDDING_MODEL", str(model_dir))
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"vector_db": vector_db}), encoding="utf-8")
    return load_config(overrides_path=path, instance_name="inst1")


# --- 設定 ---


def test_empty_embedding_url_uses_the_local_model(tmp_path, monkeypatch):
    folder = _fake_model_dir(tmp_path)
    cfg = _load(tmp_path, {"enabled": "true"}, monkeypatch, folder)
    assert uses_local_embeddings(cfg)
    (endpoint,) = cfg.vector_db_embedding_endpoints
    assert endpoint.provider == LOCAL_EMBEDDING_PROVIDER
    assert endpoint.model == "my-model"
    assert embedding_model(cfg) == "my-model"
    assert cfg.vector_db_local_embedding_model == folder
    assert cfg.vector_db_local_embedding_threads == 0
    assert cfg.vector_db_local_embedding_max_tokens == 8192


def test_embedding_url_takes_precedence_over_the_local_model(tmp_path, monkeypatch):
    folder = _fake_model_dir(tmp_path)
    cfg = _load(tmp_path, {"enabled": "true", "embedding_url": _EMBED}, monkeypatch, folder)
    assert not uses_local_embeddings(cfg)
    assert cfg.vector_db_embedding_endpoints[0].model == "bge-m3"


def test_relative_local_model_path_is_based_on_the_project_root(tmp_path, monkeypatch):
    cfg = _load(tmp_path, {}, monkeypatch, "models/embedding/none-such-model")
    assert cfg.vector_db_local_embedding_model == (PROJECT_ROOT / "models" / "embedding" / "none-such-model").resolve()
    # フォルダが無ければ内蔵は使わない（無効のときはエラーにもしない）。
    assert cfg.vector_db_embedding_endpoints == ()


def test_enabled_without_any_embeddings_mentions_the_local_model(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match=r"embedding_url.*download_embedding_model"):
        _load(tmp_path, {"enabled": "true"}, monkeypatch, tmp_path / "missing")
    # 空にすると内蔵を使わない（置き場所は案内しない）。
    with pytest.raises(ValueError, match="embedding_url") as info:
        _load(tmp_path, {"enabled": "true"}, monkeypatch, "")
    assert "built-in embedding model in" not in str(info.value)


def test_local_model_needs_both_onnx_and_tokenizer(tmp_path, monkeypatch):
    folder = _fake_model_dir(tmp_path)
    (folder / "tokenizer.json").unlink()
    assert _load(tmp_path, {}, monkeypatch, folder).vector_db_embedding_endpoints == ()


def test_image_input_cannot_be_used_with_the_local_model(tmp_path, monkeypatch):
    folder = _fake_model_dir(tmp_path)
    with pytest.raises(ValueError, match="cannot embed images"):
        _load(tmp_path, {"enabled": "true", "image_input": "true"}, monkeypatch, folder)
    # 無効なら組み合わせは確かめない。
    assert uses_local_embeddings(_load(tmp_path, {"image_input": "true"}, monkeypatch, folder))


def test_auto_prefix_follows_the_folder_name(tmp_path, monkeypatch):
    folder = _fake_model_dir(tmp_path, name="multilingual-e5-small")
    cfg = _load(tmp_path, {"enabled": "true"}, monkeypatch, folder)
    assert cfg.vector_db_query_prefix == "query: "
    assert cfg.vector_db_document_prefix == "passage: "


# --- embed_texts の分岐 ---


def test_embed_texts_uses_the_local_model_without_http(tmp_path, monkeypatch):
    folder = _fake_model_dir(tmp_path)
    cfg = _load(tmp_path, {"enabled": "true", "embedding_batch_size": 2, "query_prefix": '"q: "'}, monkeypatch, folder)
    calls = []

    def fake_embed(model_dir, texts, **options):
        calls.append((model_dir, list(texts), options))
        return [[float(len(t)), 0.0] for t in texts], 10 * len(texts)

    def no_http(*args, **kwargs):
        raise AssertionError("the built-in embeddings must not send HTTP requests")

    monkeypatch.setattr(local_embeddings, "embed", fake_embed)
    monkeypatch.setattr(embeddings, "_embed_batch", no_http)
    meter = EmbeddingMeter()
    vectors = embed_texts(cfg, ["a", "bb", "ccc"], kind="query", meter=meter)
    assert vectors == [[4.0, 0.0], [5.0, 0.0], [6.0, 0.0]]  # 接頭辞 "q: " の3文字を含む
    assert [c[1] for c in calls] == [["q: a", "q: bb"], ["q: ccc"]]
    assert calls[0][0] == folder
    assert calls[0][2] == {"threads": 0, "max_tokens": 8192, "device": "cpu", "device_id": 0, "ram_limit_mb": 4096.0, "vram_limit_mb": 4096.0}
    # 料金がかからないため「利用量・料金」の台帳には数えない。
    assert meter.take() == (0, 0)


def test_embed_texts_reports_local_errors_as_embedding_error(tmp_path, monkeypatch):
    folder = _fake_model_dir(tmp_path)
    cfg = _load(tmp_path, {"enabled": "true"}, monkeypatch, folder)
    # 本物ではない ONNX のファイルは読み込めない。
    with pytest.raises(EmbeddingError, match="Cannot use the device 'cpu' for the built-in embeddings"):
        embed_texts(cfg, ["x"], kind="document")


def test_embed_images_is_not_supported_by_the_local_model(tmp_path, monkeypatch):
    folder = _fake_model_dir(tmp_path)
    cfg = _load(tmp_path, {"enabled": "true"}, monkeypatch, folder)
    with pytest.raises(EmbeddingError, match="cannot embed images"):
        embed_images(cfg, [b"png"])


def test_device_and_memory_limits_are_read_from_config(tmp_path, monkeypatch):
    folder = _fake_model_dir(tmp_path)
    cfg = _load(
        tmp_path,
        {"local_embedding_device": "CUDA", "local_embedding_device_id": 1, "local_embedding_ram_limit_mb": 0, "local_embedding_vram_limit_mb": 8000},
        monkeypatch,
        folder,
    )
    assert cfg.vector_db_local_embedding_device == "cuda"
    assert cfg.vector_db_local_embedding_device_id == 1
    assert cfg.vector_db_local_embedding_ram_limit_mb == 0.0
    assert cfg.vector_db_local_embedding_vram_limit_mb == 8000.0
    with pytest.raises(ValueError, match="local_embedding_device"):
        _load(tmp_path, {"local_embedding_device": "tpu"}, monkeypatch, folder)


# --- メモリーの見積もりと分け方 ---


def _estimate():
    return local_embeddings.MemoryEstimate(base_mb=500.0, per_token2_mb=12 * 10.5 / 2**20, per_token_mb=0.0)


def test_memory_estimate_matches_the_measured_cpu_peak(tmp_path):
    """granite-embedding-97m（12ヘッド・384・1536、重み約 372MB）の CPU で実測したピークより少し大きく見積もる。"""
    (tmp_path / "config.json").write_text(json.dumps({"num_attention_heads": 12, "hidden_size": 384, "intermediate_size": 1536}), encoding="utf-8")
    model_file = tmp_path / "model.onnx"
    model_file.write_bytes(b"\0" * (372 * 2**20))
    estimate = local_embeddings.estimate_memory(tmp_path, model_file, "cpu")
    measured = {(1, 8192): 8090, (16, 1024): 2923, (4, 2048): 2591, (16, 512): 1306}
    for (count, length), peak in measured.items():
        assert peak <= estimate.batch_mb(count, length) <= peak * 1.35, (count, length)


def test_max_tokens_and_max_count_fit_the_limit():
    estimate = _estimate()
    tokens = estimate.max_tokens(4096)
    assert estimate.batch_mb(1, tokens) <= 4096 < estimate.batch_mb(1, tokens + 1)
    assert estimate.max_tokens(400) == 0
    count = estimate.max_count(700, 4096)
    assert estimate.batch_mb(count, 700) <= 4096 < estimate.batch_mb(count + 1, 700)


def test_plan_batches_groups_long_texts_separately_and_keeps_every_index():
    estimate = _estimate()
    lengths = [700] * 40 + [4000, 50, 3000]
    batches = local_embeddings.plan_batches(lengths, estimate, 4096)
    assert sorted(i for b in batches for i in b) == list(range(len(lengths)))
    for batch in batches:
        assert estimate.batch_mb(len(batch), max(lengths[i] for i in batch)) <= 4096
    # 長い文は短い文と同じ組にしない（パディングが長い文にそろって、短い文の分も大きくなるため）。
    assert batches[0] == [40]
    # 上限なしなら分けない。
    assert local_embeddings.plan_batches(lengths, None, 0) == [list(range(len(lengths)))]
    assert local_embeddings.plan_batches([], estimate, 4096) == []


def test_too_small_memory_limit_is_an_error(tmp_path):
    folder = _fake_model_dir(tmp_path)
    with pytest.raises(local_embeddings.LocalEmbeddingError, match="local_embedding_ram_limit_mb = 100 is too small"):
        local_embeddings.embed(folder, ["x"], ram_limit_mb=100)


# --- モデルのフォルダ・プーリング ---


def test_find_model_file_prefers_fp32_onnx(tmp_path):
    folder = _fake_model_dir(tmp_path, onnx="onnx/model_quint8_avx2.onnx")
    assert local_embeddings.find_model_file(folder) == folder / "onnx" / "model_quint8_avx2.onnx"
    (folder / "onnx" / "model.onnx").write_bytes(b"x")
    assert local_embeddings.find_model_file(folder) == folder / "onnx" / "model.onnx"
    assert local_embeddings.find_model_file(tmp_path / "missing") is None


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (None, "mean"),
        ({"pooling_mode_cls_token": True, "pooling_mode_mean_tokens": False}, "cls"),
        ({"pooling_mode_cls_token": False, "pooling_mode_mean_tokens": True}, "mean"),
        ({"pooling_mode_mean_tokens": False, "pooling_mode_lasttoken": True}, "lasttoken"),
    ],
)
def test_read_pooling(tmp_path, config, expected):
    if config is not None:
        (tmp_path / "1_Pooling").mkdir()
        (tmp_path / "1_Pooling" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    assert local_embeddings.read_pooling(tmp_path) == expected


def test_read_pooling_rejects_unsupported_modes(tmp_path):
    (tmp_path / "1_Pooling").mkdir()
    (tmp_path / "1_Pooling" / "config.json").write_text(json.dumps({"pooling_mode_mean_tokens": False, "pooling_mode_max_tokens": True}), encoding="utf-8")
    with pytest.raises(local_embeddings.LocalEmbeddingError, match="pooling"):
        local_embeddings.read_pooling(tmp_path)


def test_pool_ignores_padding():
    hidden = np.array([[[1.0, 0.0], [3.0, 2.0], [9.0, 9.0]], [[2.0, 2.0], [4.0, 0.0], [6.0, 2.0]]], dtype=np.float32)
    mask = np.array([[1, 1, 0], [1, 1, 1]])
    assert local_embeddings._pool(hidden, mask, "cls").tolist() == [[1.0, 0.0], [2.0, 2.0]]
    assert np.allclose(local_embeddings._pool(hidden, mask, "mean"), [[2.0, 1.0], [4.0, 4.0 / 3.0]])
    assert local_embeddings._pool(hidden, mask, "lasttoken").tolist() == [[3.0, 2.0], [6.0, 2.0]]


# --- 実際のモデル（あるときだけ） ---

needs_real_model = pytest.mark.skipif(
    not local_embeddings.is_model_dir(REAL_MODEL), reason="the built-in embedding model is not downloaded (tools/download_embedding_model.py)"
)


@needs_real_model
def test_real_model_vectors_are_normalized_and_batch_independent():
    texts = ["年次有給休暇は入社6か月後に10日付与する。", "The server room must be kept below 25 degrees.", "短い"]
    batch, tokens = local_embeddings.embed(REAL_MODEL, texts)
    assert tokens > 0
    assert [len(v) for v in batch] == [384, 384, 384]
    assert np.allclose(np.linalg.norm(batch, axis=1), 1.0, atol=1e-5)
    single = [local_embeddings.embed(REAL_MODEL, [t])[0][0] for t in texts]
    # パディングの有無で結果が変わらない（attention_mask を正しく渡している）。
    assert np.allclose(batch, single, atol=1e-4)


@needs_real_model
def test_real_model_memory_limit_splits_batches_without_changing_vectors():
    texts = ["年次有給休暇は入社6か月後に10日付与する。" * n for n in (1, 5, 20, 2)]
    whole, _ = local_embeddings.embed(REAL_MODEL, texts, ram_limit_mb=0)
    split, _ = local_embeddings.embed(REAL_MODEL, texts, ram_limit_mb=900)
    assert np.allclose(whole, split, atol=1e-4)


@needs_real_model
def test_real_model_finds_related_text_across_languages():
    queries = ["有給休暇の申請方法は？", "How do I request paid leave?", "溶接部の不良の判定基準"]
    docs = [
        "年次有給休暇を取得する場合は、取得予定日の3日前までに所属長へ申請書を提出すること。",
        "溶接ビードにピットやアンダーカットが認められた場合は不合格とし、再溶接を行う。",
        "社員食堂の営業時間は11時から14時までです。",
    ]
    q = np.array(local_embeddings.embed(REAL_MODEL, queries)[0])
    d = np.array(local_embeddings.embed(REAL_MODEL, docs)[0])
    assert (q @ d.T).argmax(axis=1).tolist() == [0, 0, 1]


def test_directml_plans_with_half_the_limit_and_rounds_lengths():
    """DirectML は確保した VRAM を入力の形ごとに持ち続けるため、長さを 512 単位にそろえて上限の半分で計画する。"""
    assert local_embeddings._planning_limit("directml", 4096) == 2048
    assert local_embeddings._planning_limit("cuda", 4096) == 4096
    assert local_embeddings._planning_limit("directml", 0) == 0
    assert [local_embeddings._padded(n, 512) for n in (1, 512, 513, 700)] == [512, 512, 1024, 1024]
    assert local_embeddings._padded(700, 1) == 700


def test_default_threads_is_half_the_physical_cores(monkeypatch):
    monkeypatch.setattr(local_embeddings, "physical_cores", lambda: 10)
    assert local_embeddings.default_threads() == 5
    monkeypatch.setattr(local_embeddings, "physical_cores", lambda: 5)
    assert local_embeddings.default_threads() == 3
    monkeypatch.setattr(local_embeddings, "physical_cores", lambda: 1)
    assert local_embeddings.default_threads() == 1
    assert local_embeddings.physical_cores() >= 1
