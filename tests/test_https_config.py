"""HTTPS 対応（[admin].ssl_certfile / ssl_keyfile）の回帰テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from admin import instances as inst
from admin.supervisor import Supervisor
from src.config import resolve_ssl_files


def _write_ini(tmp_path: Path, cert: str, key: str) -> Path:
    ini = tmp_path / "config.ini"
    ini.write_text(f"[admin]\nssl_certfile = {cert}\nssl_keyfile = {key}\n", encoding="utf-8")
    return ini


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("SSL_CERTFILE", raising=False)
    monkeypatch.delenv("SSL_KEYFILE", raising=False)


def test_both_empty_means_http(tmp_path):
    assert resolve_ssl_files(_write_ini(tmp_path, "", "")) is None


def test_missing_section_means_http(tmp_path):
    ini = tmp_path / "config.ini"
    ini.write_text("[llm]\n", encoding="utf-8")
    assert resolve_ssl_files(ini) is None


def test_both_set_returns_absolute_paths(tmp_path):
    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    cert.write_text("c", encoding="utf-8")
    key.write_text("k", encoding="utf-8")
    assert resolve_ssl_files(_write_ini(tmp_path, str(cert), str(key))) == (cert, key)


def test_only_one_set_is_rejected(tmp_path):
    cert = tmp_path / "server.crt"
    cert.write_text("c", encoding="utf-8")
    with pytest.raises(ValueError, match="Specify both"):
        resolve_ssl_files(_write_ini(tmp_path, str(cert), ""))


def test_missing_file_is_rejected(tmp_path):
    cert = tmp_path / "server.crt"
    cert.write_text("c", encoding="utf-8")
    with pytest.raises(ValueError, match="ssl_keyfile"):
        resolve_ssl_files(_write_ini(tmp_path, str(cert), str(tmp_path / "nope.key")))


def test_env_vars_override_ini(tmp_path, monkeypatch):
    cert, key = tmp_path / "env.crt", tmp_path / "env.key"
    cert.write_text("c", encoding="utf-8")
    key.write_text("k", encoding="utf-8")
    monkeypatch.setenv("SSL_CERTFILE", str(cert))
    monkeypatch.setenv("SSL_KEYFILE", str(key))
    assert resolve_ssl_files(_write_ini(tmp_path, "", "")) == (cert, key)


def test_supervisor_command_passes_ssl_args():
    meta = inst.InstanceMeta(name="default", display_name="default")
    cert, key = Path("C:/certs/server.crt"), Path("C:/certs/server.key")
    cmd = Supervisor._command_for(meta, (cert, key))
    i = cmd.index("--ssl-cert")
    assert cmd[i : i + 4] == ["--ssl-cert", str(cert), "--ssl-key", str(key)]
    assert "--ssl-cert" not in Supervisor._command_for(meta)
