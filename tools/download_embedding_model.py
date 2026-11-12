"""内蔵の埋め込み（CPU。[vector_db] local_embedding_model）のモデルを Hugging Face から取得する。

管理者が明示的に1回だけ実行する（Locohane 本体はモデルを取得しない。完全オフラインで使うときは、通信できる PC で
このスクリプトを実行し、できたフォルダを同じ場所へコピーする）。

使い方（コマンドプロンプトで、アプリのフォルダから）:

    call python_env.bat
    python tools\\download_embedding_model.py                 :: 既定（granite-embedding-97m-multilingual-r2、約 400MB）
    python tools\\download_embedding_model.py --model 311m    :: 精度優先（granite-embedding-311m-multilingual-r2、約 1.3GB）
    python tools\\download_embedding_model.py --model <組織>/<モデル>  :: そのほかの sentence-transformers 形式で onnx/model.onnx のあるモデル

保存先は既定で models/embedding/<モデル名>/（config.ini の [vector_db] local_embedding_model の既定と同じ）。
既にあるファイルはサイズが同じなら取り直さない。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HF_BASE = "https://huggingface.co"
ALIASES = {
    "97m": "ibm-granite/granite-embedding-97m-multilingual-r2",
    "311m": "ibm-granite/granite-embedding-311m-multilingual-r2",
}
# 取得するファイル（required が False のものは、無くてもエラーにしない）。モデルの重みは ONNX（fp32）だけ。
FILES = (
    ("onnx/model.onnx", True),
    ("tokenizer.json", True),
    ("1_Pooling/config.json", False),
    ("config.json", False),
    ("tokenizer_config.json", False),
    ("special_tokens_map.json", False),
    ("modules.json", False),
    ("sentence_bert_config.json", False),
    ("config_sentence_transformers.json", False),
    # ライセンスはモデルカード（README.md）に書かれている。
    ("README.md", False),
    ("LICENSE", False),
)


def _download(client: httpx.Client, url: str, dest: Path, label: str) -> bool:
    """url を dest へ保存する。無ければ（404）False。サイズが同じファイルが既にあれば取り直さない。"""
    with client.stream("GET", url) as response:
        if response.status_code == 404:
            return False
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        if dest.is_file() and total and dest.stat().st_size == total:
            print(f"  {label}: already downloaded")
            return True
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        done = 0
        with part.open("wb") as f:
            for chunk in response.iter_bytes(1024 * 1024):
                f.write(chunk)
                done += len(chunk)
                if total > 10 * 1024 * 1024:
                    print(f"\r  {label}: {done / 1e6:,.0f} / {total / 1e6:,.0f} MB", end="", flush=True)
        if total > 10 * 1024 * 1024:
            print()
        part.replace(dest)
        print(f"  {label}: {done / 1e6:,.1f} MB")
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download the built-in embedding model (CPU) for [vector_db] local_embedding_model.")
    parser.add_argument("--model", default="97m", help="97m (default), 311m, or a Hugging Face repo id such as ibm-granite/granite-embedding-97m-multilingual-r2")
    parser.add_argument("--revision", default="main", help="Branch, tag or commit of the repo (default main)")
    parser.add_argument("--dest", default="", help="Folder to save to (default models/embedding/<model name>)")
    args = parser.parse_args(argv)

    repo = ALIASES.get(args.model, args.model)
    if repo.count("/") != 1:
        parser.error(f"--model must be 97m, 311m or <org>/<name>: {args.model!r}")
    dest = Path(args.dest) if args.dest else PROJECT_ROOT / "models" / "embedding" / repo.split("/")[1]
    if not dest.is_absolute():
        dest = PROJECT_ROOT / dest
    print(f"Downloading {repo} ({args.revision}) to {dest}")
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0, connect=15.0)) as client:
        for name, required in FILES:
            url = f"{HF_BASE}/{repo}/resolve/{args.revision}/{name}"
            try:
                found = _download(client, url, dest / name, name)
            except httpx.HTTPError as e:
                print(f"Error: cannot download {url}: {e}", file=sys.stderr)
                return 1
            if not found and required:
                print(f"Error: {repo} has no {name} (the built-in embeddings need an ONNX model and tokenizer.json).", file=sys.stderr)
                return 1
    rel = dest.relative_to(PROJECT_ROOT).as_posix() if dest.is_relative_to(PROJECT_ROOT) else str(dest)
    print("Done. In config.ini, leave [vector_db] embedding_url empty and set:")
    print(f"  local_embedding_model = {rel}")
    print("Check the license of the model in README.md in the folder before using it commercially.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
