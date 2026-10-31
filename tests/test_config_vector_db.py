"""config.ini [vector_db] の読み込み・検証のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import load_config


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """実プロジェクトの data/ にテスト用ディレクトリを作らないよう、データ保存先を tmp へ逃がす。"""
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data" / "${instance}"))
    for key in ("VECTOR_DB_ENABLED", "VECTOR_DB_BACKEND", "VECTOR_DB_EMBEDDING_URL", "VECTOR_DB_QDRANT_URL", "VECTOR_DB_DISTANCE", "VECTOR_DB_QDRANT_PRESET", "VECTOR_DB_QUERY_PREFIX", "VECTOR_DB_DOCUMENT_PREFIX", "QDRANT_API_KEY"):
        monkeypatch.delenv(key, raising=False)


_EMBED = '[{"base_url": "http://localhost:12440/v1", "api_key": "x", "model": "bge-m3"}]'


def _load(tmp_path: Path, vector_db: dict, instance: str = "inst1"):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"vector_db": vector_db}), encoding="utf-8")
    return load_config(overrides_path=path, instance_name=instance)


def test_defaults_are_disabled_sqlite_vec(tmp_path):
    cfg = load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")
    assert cfg.vector_db_enabled is False
    assert cfg.vector_db_backend == "sqlite_vec"
    assert cfg.vector_db_dir == tmp_path / "data" / "inst1" / "vector_db"
    assert cfg.vector_db_embedding_endpoints == ()
    assert cfg.vector_db_qdrant_collection_prefix == "locohane_inst1_"
    assert cfg.vector_db_distance == "Cosine"
    assert ".pdf" in cfg.vector_db_include_extensions
    assert cfg.vector_db_text_extensions == ()
    # 無効のときはディレクトリを作らない
    assert not cfg.vector_db_dir.exists()


def test_ingest_temp_dir_defaults_to_system_temp_and_resolves_paths(tmp_path, monkeypatch):
    from src.config import PROJECT_ROOT

    monkeypatch.delenv("VECTOR_DB_INGEST_TEMP_DIR", raising=False)
    monkeypatch.setenv("ADMIN_SETTINGS_PATH", str(tmp_path / "admin_settings.json"))
    assert _load(tmp_path, {}).vector_db_ingest_temp_dir == ""
    # 全インスタンス共通の設定（管理者設定）のため、インスタンス別の config_overrides.json の値は使わない。
    assert _load(tmp_path, {"ingest_temp_dir": str(tmp_path / "x")}).vector_db_ingest_temp_dir == ""
    (tmp_path / "admin_settings.json").write_text(json.dumps({"vector_db": {"ingest_temp_dir": "work/tmp"}}), encoding="utf-8")
    assert _load(tmp_path, {}).vector_db_ingest_temp_dir == str((PROJECT_ROOT / "work" / "tmp").resolve())
    monkeypatch.setenv("VECTOR_DB_INGEST_TEMP_DIR", str(tmp_path / "env_tmp"))
    assert _load(tmp_path, {}).vector_db_ingest_temp_dir == str(tmp_path / "env_tmp")


def test_ingest_temp_dir_rejects_placeholders_and_files(tmp_path, monkeypatch, caplog):
    """${…} は置換しない設定のため、その文字列どおりのフォルダを作らずシステムの一時フォルダにする（既存のファイルも同じ）。"""
    monkeypatch.setenv("ADMIN_SETTINGS_PATH", str(tmp_path / "admin_settings.json"))
    monkeypatch.setenv("VECTOR_DB_INGEST_TEMP_DIR", "${common_data_dir}/tmp")
    with caplog.at_level("WARNING", logger="src.config"):
        assert _load(tmp_path, {}).vector_db_ingest_temp_dir == ""
    assert "cannot be used" in caplog.text
    a_file = tmp_path / "a_file.txt"
    a_file.write_text("x", encoding="utf-8")
    monkeypatch.setenv("VECTOR_DB_INGEST_TEMP_DIR", str(a_file))
    assert _load(tmp_path, {}).vector_db_ingest_temp_dir == ""


def test_enabled_with_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("QDRANT_API_KEY", "secret")
    cfg = _load(
        tmp_path,
        {
            "enabled": "true",
            "backend": "qdrant",
            "qdrant_url": "http://localhost:6333",
            "embedding_url": _EMBED,
            "query_prefix": '"query: "',
            "include_extensions": "PDF, .md, md",
            "text_extensions": "STEP, .igs, mat",
        },
    )
    assert cfg.vector_db_enabled and cfg.vector_db_backend == "qdrant"
    assert cfg.vector_db_embedding_endpoints[0].model == "bge-m3"
    assert cfg.vector_db_query_prefix == "query: "
    assert cfg.vector_db_include_extensions == (".pdf", ".md")
    assert cfg.vector_db_text_extensions == (".step", ".igs", ".mat")
    assert cfg.vector_db_qdrant_api_key == "secret"
    assert cfg.vector_db_dir.is_dir()


@pytest.mark.parametrize(
    "model, query_prefix, document_prefix",
    [
        ("bge-m3", "", ""),
        ("multilingual-e5-large", "query: ", "passage: "),
        ("intfloat/multilingual-e5-base-Q8_0.gguf", "query: ", "passage: "),
        ("multilingual-e5-large-instruct", "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: ", ""),
        ("ruri-v3-310m", "検索クエリ: ", "検索文書: "),
        ("ruri-base-v2", "クエリ: ", "文章: "),
        ("nomic-embed-text-v1.5", "search_query: ", "search_document: "),
        ("Nemotron-3.5-Embed-1B-Q8_0", "query: ", "passage: "),
        ("llama-nemotron-embed-1b-v2", "query: ", "passage: "),
        ("nemotron-nano-9b", "", ""),
        ("qwen3-embedding", "", ""),
    ],
)
def test_prefix_auto_follows_embedding_model(tmp_path, model, query_prefix, document_prefix):
    embed = f'[{{"base_url": "http://localhost:12440/v1", "api_key": "x", "model": "{model}"}},]'
    cfg = _load(tmp_path, {"enabled": "true", "embedding_url": embed})
    assert (cfg.vector_db_query_prefix, cfg.vector_db_document_prefix) == (query_prefix, document_prefix)


def test_prefix_explicit_value_and_blank_override_auto(tmp_path):
    embed = '[{"base_url": "http://localhost:12440/v1", "api_key": "x", "model": "multilingual-e5-large"},]'
    cfg = _load(tmp_path, {"enabled": "true", "embedding_url": embed, "query_prefix": '"q: "', "document_prefix": ""})
    assert (cfg.vector_db_query_prefix, cfg.vector_db_document_prefix) == ("q: ", "")
    # embedding_url 未設定なら auto は空
    (tmp_path / "none").mkdir()
    assert _load(tmp_path / "none", {}).vector_db_query_prefix == ""


@pytest.mark.parametrize(
    "vector_db, message",
    [
        ({"enabled": "true"}, "embedding_url"),
        ({"enabled": "true", "embedding_url": _EMBED, "backend": "qdrant"}, "qdrant_url"),
        ({"backend": "chroma"}, "backend"),
        ({"chunk_size": "100", "chunk_overlap": "100"}, "chunk_overlap"),
        ({"min_score": "1.5"}, "min_score"),
        ({"top_k": "0"}, "top_k"),
        (
            {"embedding_url": '[{"base_url": "http://a/v1", "model": "m1"}, {"base_url": "http://b/v1", "model": "m2"}]'},
            "must be the same",
        ),
        ({"embedding_url": '[{"base_url": "http://a/v1", "model": "m1", "start": 9, "end": 18}]'}, "neither start nor end"),
        ({"embedding_url": '[{"base_url": "http://a/v1"}]'}, r"\[vector_db\]\.embedding_url"),
    ],
)
def test_invalid_settings_are_rejected(tmp_path, vector_db, message):
    with pytest.raises(ValueError, match=message):
        _load(tmp_path, vector_db)


def test_admin_catalog_offers_embedding_url_editor():
    from admin.ini_catalog import parse_file

    catalog = parse_file(Path(__file__).resolve().parent.parent / "config.ini")
    info = {(k.section, k.key): k for k in catalog.keys}[("vector_db", "embedding_url")]
    assert info.ui_kind == "list"
    assert [f["name"] for f in info.schema["item"]["fields"]] == ["base_url", "api_key", "model", "provider"]


@pytest.mark.parametrize(("value", "expected"), [("euclid", "Euclid"), (" DOT ", "Dot"), ("Manhattan", "Manhattan")])
def test_distance_is_normalized(tmp_path, value, expected):
    assert _load(tmp_path, {"backend": "qdrant", "distance": value}).vector_db_distance == expected


def test_distance_rejects_unknown_value(tmp_path):
    with pytest.raises(ValueError, match="distance"):
        _load(tmp_path, {"distance": "l2"})


def test_sqlite_vec_rejects_dot(tmp_path):
    with pytest.raises(ValueError, match="sqlite-vec does not support Dot"):
        _load(tmp_path, {"backend": "sqlite_vec", "distance": "dot"})


def test_qdrant_preset_default_and_validation(tmp_path):
    assert load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1").vector_db_qdrant_preset == "none"
    assert _load(tmp_path, {"backend": "qdrant", "qdrant_preset": " 3M "}).vector_db_qdrant_preset == "3m"
    assert _load(tmp_path, {"backend": "qdrant", "qdrant_preset": "10m"}).vector_db_qdrant_preset == "10m"
    with pytest.raises(ValueError, match="qdrant_preset"):
        _load(tmp_path, {"qdrant_preset": "11m"})


def test_admin_catalog_offers_qdrant_preset_choices():
    from admin.ini_catalog import parse_file
    from src.config import VECTOR_DB_QDRANT_PRESETS

    catalog = parse_file(Path(__file__).resolve().parent.parent / "config.ini")
    info = {(k.section, k.key): k for k in catalog.keys}[("vector_db", "qdrant_preset")]
    assert tuple(info.choices) == VECTOR_DB_QDRANT_PRESETS


def test_embedding_retry_and_routing_defaults_and_validation(tmp_path):
    cfg = load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")
    assert cfg.vector_db_embedding_routing_strategy == "round_robin"
    assert cfg.vector_db_embedding_max_retries == 3
    assert cfg.vector_db_search_embedding_max_retries == 2
    assert cfg.vector_db_embedding_retry_interval_seconds == 10.0
    cfg = _load(
        tmp_path,
        {
            "embedding_routing_strategy": "priority_failover",
            "embedding_max_retries": "0",
            "search_embedding_max_retries": "0",
            "embedding_retry_interval_seconds": "2.5",
        },
    )
    assert cfg.vector_db_embedding_routing_strategy == "priority_failover"
    assert cfg.vector_db_embedding_max_retries == 0
    assert cfg.vector_db_search_embedding_max_retries == 0
    assert cfg.vector_db_embedding_retry_interval_seconds == 2.5
    with pytest.raises(ValueError, match=r"\[vector_db\]\.embedding_routing_strategy"):
        _load(tmp_path, {"embedding_routing_strategy": "sticky"})
    with pytest.raises(ValueError, match="embedding_max_retries"):
        _load(tmp_path, {"embedding_max_retries": "-1"})
    with pytest.raises(ValueError, match="search_embedding_max_retries"):
        _load(tmp_path, {"search_embedding_max_retries": "-1"})
    with pytest.raises(ValueError, match="embedding_retry_interval_seconds"):
        _load(tmp_path, {"embedding_retry_interval_seconds": "0"})


def test_ingest_workers_default_and_validation(tmp_path, monkeypatch):
    assert load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1").vector_db_ingest_workers == 1
    monkeypatch.setenv("VECTOR_DB_INGEST_WORKERS", "4")
    assert load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1").vector_db_ingest_workers == 4
    monkeypatch.setenv("VECTOR_DB_INGEST_WORKERS", "0")
    with pytest.raises(ValueError, match="ingest_workers"):
        load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")


def test_ingest_embed_requests_default_and_validation(tmp_path, monkeypatch):
    assert load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1").vector_db_ingest_embed_requests == 0
    monkeypatch.setenv("VECTOR_DB_INGEST_EMBED_REQUESTS", "16")
    assert load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1").vector_db_ingest_embed_requests == 16
    monkeypatch.setenv("VECTOR_DB_INGEST_EMBED_REQUESTS", "-1")
    with pytest.raises(ValueError, match="ingest_embed_requests"):
        load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")


def test_qdrant_retries_and_timeout_defaults_and_validation(tmp_path, monkeypatch):
    for name in ("VECTOR_DB_QDRANT_MAX_RETRIES", "VECTOR_DB_QDRANT_RETRY_INTERVAL_SECONDS", "VECTOR_DB_QDRANT_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    cfg = load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")
    assert (cfg.vector_db_qdrant_max_retries, cfg.vector_db_qdrant_retry_interval_seconds, cfg.vector_db_qdrant_timeout_seconds) == (5, 10, 120)
    monkeypatch.setenv("VECTOR_DB_QDRANT_MAX_RETRIES", "-1")
    assert load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1").vector_db_qdrant_max_retries == -1
    monkeypatch.setenv("VECTOR_DB_QDRANT_MAX_RETRIES", "-2")
    with pytest.raises(ValueError, match="qdrant_max_retries"):
        load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")
    monkeypatch.delenv("VECTOR_DB_QDRANT_MAX_RETRIES")
    # 全インスタンス共通のため、config_overrides.json の値は使わない（送り直しの間隔も同じ）。
    cfg = _load(tmp_path, {"qdrant_max_retries": "-1", "qdrant_retry_interval_seconds": "3"})
    assert (cfg.vector_db_qdrant_max_retries, cfg.vector_db_qdrant_retry_interval_seconds) == (5, 10)
    monkeypatch.setenv("VECTOR_DB_QDRANT_RETRY_INTERVAL_SECONDS", "3")
    assert load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1").vector_db_qdrant_retry_interval_seconds == 3
    monkeypatch.setenv("VECTOR_DB_QDRANT_RETRY_INTERVAL_SECONDS", "0")
    with pytest.raises(ValueError, match="qdrant_retry_interval_seconds"):
        load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")


def test_ingest_parallelism_ignores_instance_overrides(tmp_path):
    """取り込みの並列数は全インスタンス共通のため、config_overrides.json の値は使わない（admin/knowledge.py 参照）。"""
    cfg = _load(tmp_path, {"ingest_workers": "4", "ingest_processes": "2", "ingest_embed_requests": "8", "chunk_size": "300"})
    assert (
        cfg.vector_db_ingest_workers, cfg.vector_db_ingest_processes, cfg.vector_db_ingest_embed_requests, cfg.vector_db_chunk_size
    ) == (1, 0, 0, 300)


def test_embedding_encoding_format_and_ingest_processes(tmp_path, monkeypatch):
    cfg = load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")
    assert (cfg.vector_db_embedding_encoding_format, cfg.vector_db_ingest_processes) == ("base64", 0)
    monkeypatch.setenv("VECTOR_DB_INGEST_PROCESSES", "4")
    cfg = _load(tmp_path, {"embedding_encoding_format": "FLOAT"})
    assert (cfg.vector_db_embedding_encoding_format, cfg.vector_db_ingest_processes) == ("float", 4)
    with pytest.raises(ValueError, match="embedding_encoding_format"):
        _load(tmp_path, {"embedding_encoding_format": "binary"})
    monkeypatch.setenv("VECTOR_DB_INGEST_PROCESSES", "-1")
    with pytest.raises(ValueError, match="ingest_processes"):
        _load(tmp_path, {})


_SHARED_INGEST_ENVS = (
    "VECTOR_DB_SCAN_WORKERS",
    "VECTOR_DB_MAX_FILES_PER_COLLECTION",
    "VECTOR_DB_MAX_FILE_MB",
    "VECTOR_DB_OFFICE_TIMEOUT_SECONDS",
)


def test_scan_workers_and_file_limits_defaults_and_validation(tmp_path, monkeypatch):
    for name in _SHARED_INGEST_ENVS:
        monkeypatch.delenv(name, raising=False)
    cfg = load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")
    assert (
        cfg.vector_db_scan_workers, cfg.vector_db_max_files_per_collection, cfg.vector_db_max_file_mb, cfg.vector_db_office_timeout_seconds
    ) == (8, 1000000, 50, 180)
    for name, value in zip(_SHARED_INGEST_ENVS, ("16", "5000", "2.5", "30")):
        monkeypatch.setenv(name, value)
    cfg = load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")
    assert (
        cfg.vector_db_scan_workers, cfg.vector_db_max_files_per_collection, cfg.vector_db_max_file_mb, cfg.vector_db_office_timeout_seconds
    ) == (16, 5000, 2.5, 30)
    for name, key in zip(_SHARED_INGEST_ENVS, ("scan_workers", "max_files_per_collection", "max_file_mb", "office_timeout_seconds")):
        monkeypatch.setenv(name, "0")
        with pytest.raises(ValueError, match=key):
            load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")
        monkeypatch.delenv(name)


def test_scan_workers_and_file_limits_ignore_instance_overrides(tmp_path, monkeypatch):
    """全インスタンス共通の設定（管理者設定）のため、config_overrides.json の値は使わない。"""
    for name in _SHARED_INGEST_ENVS:
        monkeypatch.delenv(name, raising=False)
    cfg = _load(
        tmp_path, {"scan_workers": "16", "max_files_per_collection": "5000", "max_file_mb": "2", "office_timeout_seconds": "30"}
    )
    assert (
        cfg.vector_db_scan_workers, cfg.vector_db_max_files_per_collection, cfg.vector_db_max_file_mb, cfg.vector_db_office_timeout_seconds
    ) == (8, 1000000, 50, 180)
