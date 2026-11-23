"""WebSocket の 1 フレームの上限の回帰テスト（app.py の _patch_uvicorn_ws_max_size）。

engineio の max_http_buffer_size だけ上げても、`chainlit run` が作る uvicorn.Config の ws_max_size（既定 16MiB）で
大きな 3D ビューアの確定の答えが切られていた。
"""

from __future__ import annotations

import uvicorn

import app


def test_uvicorn_config_accepts_the_websocket_message_limit() -> None:
    config = uvicorn.Config(app=lambda scope, receive, send: None)
    assert config.ws_max_size >= app._WEBSOCKET_MAX_MESSAGE_BYTES
    # 呼ぶ側がもっと大きな値を指定したら、そちらを使う。
    bigger = app._WEBSOCKET_MAX_MESSAGE_BYTES * 2
    assert uvicorn.Config(app=lambda scope, receive, send: None, ws_max_size=bigger).ws_max_size == bigger
    # 2 回当てても重ねて包まない。
    before = uvicorn.Config.__init__
    app._patch_uvicorn_ws_max_size()
    assert uvicorn.Config.__init__ is before
