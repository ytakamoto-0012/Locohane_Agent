"""内蔵の埋め込み（CPU）。外部の埋め込みサーバーを使わず、Locohane のプロセスの中で ONNX のモデルを動かす。

[vector_db] embedding_url が空で、[vector_db] local_embedding_model のフォルダにモデルがあるときに使う
（src/config.py の _as_local_embedding_endpoints()。そのとき埋め込みの接続先は provider="local" の1件になり、
src/vector_db/embeddings.py の embed_texts() がこのモジュールへ回す）。

フォルダは Hugging Face の sentence-transformers 形式のモデルの一部（tools/download_embedding_model.py で取得）:

- ``onnx/model.onnx``（無ければ ``onnx/*.onnx``・``*.onnx`` の最初の1つ）: 入力 input_ids・attention_mask
  （あれば token_type_ids）、出力 last_hidden_state（または sentence_embedding）。
- ``tokenizer.json``: Hugging Face tokenizers の形式。
- ``1_Pooling/config.json``（任意）: プーリングの方式（pooling_mode_cls_token / pooling_mode_mean_tokens /
  pooling_mode_lasttoken）。無ければ平均（mean）。

ベクターは L2 正規化して返す（コサイン類似度で比べるため）。テキストのみで、画像の埋め込みには対応しない。

計算する場所は [vector_db] local_embedding_device で選ぶ（DEVICES）:

- cpu: CPUExecutionProvider（onnxruntime。requirements.txt の既定）。
- cuda: CUDAExecutionProvider（NVIDIA の GPU。onnxruntime の代わりに onnxruntime-gpu を入れる）。
- directml: DmlExecutionProvider（Windows の DirectX 12 の GPU。NVIDIA・AMD・Intel。onnxruntime の代わりに onnxruntime-directml を入れる）。
- auto: cuda → directml → cpu の順に、入っていて使えるものを使う。

onnxruntime・onnxruntime-gpu・onnxruntime-directml は同じ onnxruntime モジュールを入れるため、1つの環境に1つだけ入れる。
GPU を選んだのにその実行プロバイダーが使えない（パッケージが違う・CUDA の DLL が読めない）ときは、CPU に黙って切り替えずに
LocalEmbeddingError にする（CPU は数倍〜十数倍遅く、気付かないまま取り込みが長引くため）。auto だけは CPU に切り替えてログに残す。

使うメモリーは [vector_db] local_embedding_ram_limit_mb（cpu）・local_embedding_vram_limit_mb（cuda・directml）に収める。
1回の計算のメモリーは「モデルの重み ＋ 件数 × トークン数²（注意機構）」でほぼ決まるため、estimate_memory() で見積もり、
長すぎる文を切り詰め（1件で上限を超えない長さまで）、上限に収まる件数ずつに分けて計算する（plan_batches）。
DirectML は確保した VRAM を入力の形ごとに持ち続けるため、長さを 512 トークン単位にそろえ、上限の半分で計画する。

onnxruntime・tokenizers・numpy は、内蔵の埋め込みを使うときだけ読み込む（使わない環境の起動を遅くしない）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TOKENIZER_FILE = "tokenizer.json"
POOLING_FILE = Path("1_Pooling") / "config.json"
# ONNX のファイルを探す順（量子化版は fp32 版とのずれが大きく、バッチの組み方で結果も変わるため、fp32 を先にする）。
_MODEL_CANDIDATES = (Path("onnx") / "model.onnx", Path("model.onnx"))
POOLING_MODES = ("cls", "mean", "lasttoken")
# [vector_db].local_embedding_device の選択肢（src/config.py の LOCAL_EMBEDDING_DEVICES と同じ）。
DEVICES = ("cpu", "cuda", "directml", "auto")
_PROVIDERS = {"cpu": "CPUExecutionProvider", "cuda": "CUDAExecutionProvider", "directml": "DmlExecutionProvider"}
# 選んだ GPU の実行プロバイダーが入っていないときに案内するパッケージ。
_PACKAGES = {"cpu": "onnxruntime", "cuda": "onnxruntime-gpu[cuda,cudnn]", "directml": "onnxruntime-directml"}


class LocalEmbeddingError(Exception):
    """内蔵の埋め込みモデルを読めない・計算できない。"""


def find_model_file(model_dir: Path) -> Path | None:
    """model_dir の中の ONNX のファイル（_MODEL_CANDIDATES の順。無ければ onnx/・直下の *.onnx の名前順の最初）。"""
    for candidate in _MODEL_CANDIDATES:
        path = model_dir / candidate
        if path.is_file():
            return path
    for folder in (model_dir / "onnx", model_dir):
        if folder.is_dir():
            found = sorted(p for p in folder.glob("*.onnx") if p.is_file())
            if found:
                return found[0]
    return None


def is_model_dir(model_dir: Path) -> bool:
    """model_dir に ONNX のファイルと tokenizer.json がそろっているか（onnxruntime は読み込まない）。"""
    return model_dir.is_dir() and (model_dir / TOKENIZER_FILE).is_file() and find_model_file(model_dir) is not None


def read_pooling(model_dir: Path) -> str:
    """1_Pooling/config.json からプーリングの方式（POOLING_MODES のいずれか）を読む。無い・読めなければ mean。"""
    path = model_dir / POOLING_FILE
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "mean"
    if not isinstance(config, dict):
        return "mean"
    if config.get("pooling_mode_cls_token"):
        return "cls"
    if config.get("pooling_mode_lasttoken"):
        return "lasttoken"
    if config.get("pooling_mode_mean_tokens", True):
        return "mean"
    raise LocalEmbeddingError(
        f"{path}: this pooling mode is not supported (supported: pooling_mode_cls_token / pooling_mode_mean_tokens / pooling_mode_lasttoken)."
    )


# 使うメモリーの見積もり（estimate_memory）の係数。granite-embedding-97m-multilingual-r2（ModernBERT、12ヘッド）で
# 件数 1〜64・トークン数 512〜8192 を実測した値（RAM は peak working set、VRAM は nvidia-smi の増分）より少し大きめにしてある。
# 計算は1回の推論で注意機構（ヘッド数 × トークン数²）の行列が大半を占めるため、それに比例させる。
# 1件・ヘッド・トークン数²あたりのバイト数。
_ATTENTION_BYTES = {"cpu": 11.5, "cuda": 11.5, "directml": 19.5}
# モデルの重み以外に、読み込んだだけで使う量（MB。cpu は Python・トークナイザー（RAM）の分を含む。CUDA は CUDA のコンテキスト・cuDNN の作業領域）。
_OVERHEAD_MB = {"cpu": 350.0, "cuda": 400.0, "directml": 50.0}
# 見積もりで、長すぎる文を切るときの下限のトークン数（これも収まらない上限はエラーにする）。
MIN_TOKENS = 128
# DirectML は入力の形（件数 × トークン数）ごとに確保した VRAM を手放さずに持ち続ける（実測: 同じ形の繰り返しでは増えず、
# 違う長さが来るたびに積み上がる）。形の種類を減らすため長さをこの単位に切り上げてそろえ、ほかの形の分が残ることを見込んで
# 上限のこの割合で1回の計算を計画する。
_DIRECTML_PAD_MULTIPLE = 512
_DIRECTML_LIMIT_RATIO = 0.5


@dataclass(frozen=True)
class MemoryEstimate:
    """内蔵の埋め込みが1回の計算で使うメモリー（MB）の見積もり。

    使う量 ≒ base_mb + 件数 × (per_token2_mb × トークン数² + per_token_mb × トークン数)。
    cpu では RAM、cuda・directml では VRAM（RAM は GPU でも 1〜2GB ほど使うが、件数・長さではあまり変わらない）。
    """

    base_mb: float
    per_token2_mb: float
    per_token_mb: float

    def batch_mb(self, count: int, length: int) -> float:
        return self.base_mb + count * (self.per_token2_mb * length * length + self.per_token_mb * length)

    def max_tokens(self, limit_mb: float) -> int:
        """1件だけで limit_mb に収まる最大のトークン数（0 なら1トークンも収まらない）。"""
        room = limit_mb - self.base_mb
        if room <= 0:
            return 0
        a, b = self.per_token2_mb, self.per_token_mb
        return int((-b + (b * b + 4 * a * room) ** 0.5) / (2 * a))

    def max_count(self, length: int, limit_mb: float) -> int:
        """長さ length の文を limit_mb の中で一度に計算できる件数（1 以上）。"""
        per_item = self.per_token2_mb * length * length + self.per_token_mb * length
        return max(1, int((limit_mb - self.base_mb) // per_item)) if per_item > 0 else 1 << 30


def estimate_memory(model_dir: Path, model_file: Path, device: str) -> MemoryEstimate:
    """model_dir の config.json（num_attention_heads・hidden_size・intermediate_size）と重みのファイルの大きさから、
    device（"cpu" / "cuda" / "directml"）で使うメモリーを見積もる。config.json が無ければ BERT-base 相当とみなす。"""
    try:
        config = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    heads = int(config.get("num_attention_heads") or 12)
    hidden = int(config.get("hidden_size") or 768)
    intermediate = int(config.get("intermediate_size") or hidden * 4)
    weights_mb = model_file.stat().st_size / 2**20 if model_file.is_file() else 0.0
    mb = 1.0 / 2**20
    return MemoryEstimate(
        base_mb=weights_mb * 1.05 + _OVERHEAD_MB[device],
        per_token2_mb=heads * _ATTENTION_BYTES[device] * mb,
        # 層の中間の活性化（float32）の分。
        per_token_mb=(intermediate * 4 + hidden * 8) * 4 * mb,
    )


def plan_batches(lengths: list[int], estimate: MemoryEstimate | None, limit_mb: float) -> list[list[int]]:
    """lengths（各文のトークン数）の文を、1回の計算が limit_mb に収まるように分けた添字の組を返す。

    長い順に並べ、組の先頭（その組で一番長い文。パディングはこの長さにそろう）で件数の上限を決める。
    長さの近い文が同じ組に入るため、パディングも少なくなる。estimate が None か limit_mb が 0 以下なら分けない。
    """
    if not lengths:
        return []
    if estimate is None or limit_mb <= 0:
        return [list(range(len(lengths)))]
    order = sorted(range(len(lengths)), key=lambda i: lengths[i], reverse=True)
    batches: list[list[int]] = []
    current: list[int] = []
    capacity = 0
    for index in order:
        if not current:
            current = [index]
            capacity = estimate.max_count(max(lengths[index], 1), limit_mb)
        elif len(current) < capacity:
            current.append(index)
        else:
            batches.append(current)
            current = [index]
            capacity = estimate.max_count(max(lengths[index], 1), limit_mb)
    batches.append(current)
    return batches


@dataclass
class _Model:
    session: Any
    tokenizer: Any
    pooling: str
    input_names: tuple[str, ...]
    output_names: tuple[str, ...]
    pad_id: int
    # InferenceSession は既定で全コアを使うため、同時に複数の推論を走らせるとコアの取り合いで遅くなる。1つずつ計算する
    # （GPU は1回の計算で使い切るため並列にしても速くならず、DirectML は同時に計算するとプロセスが落ちる）。
    # 1つずつなので、メモリーのピークも1回の計算の分で決まる（memory_limit_mb がそのまま効く）。
    lock: threading.Lock
    # 実際に計算している場所（"cpu" / "cuda" / "directml"）。
    device: str = "cpu"
    # その場所のメモリーの上限（MB。0 は上限なし）と見積もり。
    memory_limit_mb: float = 0.0
    estimate: MemoryEstimate | None = None
    # 1件を切り詰めるトークン数（max_tokens とメモリーの上限の小さい方）。
    max_tokens: int = 8192
    # パディングで長さをそろえる単位（DirectML のときだけ _DIRECTML_PAD_MULTIPLE）。
    pad_multiple: int = 1
    # session.run に渡す RunOptions（cpu でメモリーの上限があるとき、計算の終わりにプールを縮める。_run_options）。
    run_options: Any = None


_models: dict[tuple, _Model] = {}
_models_lock = threading.Lock()


def physical_cores() -> int:
    """物理コア数（Windows は GetLogicalProcessorInformationEx で数える。数えられなければ論理コア数の半分）。"""
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            size = ctypes.c_ulong(0)
            # RelationProcessorCore = 0。1回目で必要な大きさを得る。
            kernel32.GetLogicalProcessorInformationEx(0, None, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if size.value and kernel32.GetLogicalProcessorInformationEx(0, buffer, ctypes.byref(size)):
                count = offset = 0
                while offset < size.value:
                    # SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX: Relationship (DWORD), Size (DWORD), ...
                    entry_size = int.from_bytes(buffer.raw[offset + 4 : offset + 8], "little")
                    if entry_size <= 0:
                        break
                    count += 1
                    offset += entry_size
                if count > 0:
                    return count
        except (AttributeError, OSError, ValueError):
            pass
    return max(1, (os.cpu_count() or 2) // 2)


def default_threads() -> int:
    """local_embedding_threads = 0 のときのスレッド数（物理コア数の半分、切り上げ、最低1）。

    1回の計算で全コアを使うと、同時に動く取り込みの準備（PDF・Office の読み取り）やチャットの応答が遅くなる。
    実測（10コア）では半分の5スレッドで全コアの約8割の速さが出て、それ以上はほとんど伸びない。
    """
    return max(1, (physical_cores() + 1) // 2)


def _candidates(device: str) -> tuple[str, ...]:
    if device == "auto":
        return ("cuda", "directml", "cpu")
    return (device,)


def _create_session(
    ort: Any, model_dir: Path, model_file: Path, device: str, threads: int, device_id: int, ram_limit_mb: float, vram_limit_mb: float
) -> tuple[Any, str]:
    """device（DEVICES のいずれか）で InferenceSession を作り、(セッション, 実際に使う場所) を返す。"""
    if device not in DEVICES:
        raise LocalEmbeddingError(f"Unknown device {device!r} (choose from {'/'.join(DEVICES)}).")
    available = set(ort.get_available_providers())
    reasons: list[str] = []
    for candidate in _candidates(device):
        provider = _PROVIDERS[candidate]
        if provider not in available:
            reasons.append(
                f"{candidate}: {provider} is not available in this onnxruntime "
                f"(uninstall onnxruntime / onnxruntime-gpu / onnxruntime-directml, then pip install {_PACKAGES[candidate]})"
            )
            continue
        options = ort.SessionOptions()
        provider_options: dict[str, Any] = {}
        if candidate == "cpu":
            options.intra_op_num_threads = threads if threads > 0 else default_threads()
            if ram_limit_mb > 0 and _register_cpu_allocator(ort):
                options.add_session_config_entry("session.use_env_allocators", "1")
        else:
            provider_options["device_id"] = device_id
        if candidate == "directml":
            # DirectML の実行プロバイダーの要件（メモリーの再利用パターンを切り、順番に実行する）。
            options.enable_mem_pattern = False
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        if candidate == "cuda":
            if vram_limit_mb > 0:
                # 見積もりが外れても、VRAM からあふれて共有メモリー（RAM）で極端に遅く動き続けるのではなく、
                # onnxruntime のメモリー不足のエラーで止める（CUDA のコンテキストの分は上限に含まれないため差し引く）。
                limit = max(vram_limit_mb - _OVERHEAD_MB["cuda"], 256.0)
                provider_options["gpu_mem_limit"] = int(limit * 2**20)
                provider_options["arena_extend_strategy"] = "kSameAsRequested"
            if hasattr(ort, "preload_dlls"):
                # onnxruntime-gpu[cuda,cudnn] が pip で入れた CUDA・cuDNN の DLL を読み込む（無ければ PATH の CUDA を使う）。
                try:
                    ort.preload_dlls()
                except Exception as e:  # noqa: BLE001 - 読み込めなくても PATH の CUDA で動くことがあるため、セッションの作成で判定する
                    logger.debug("Built-in embeddings: preload_dlls failed: %s", e)
        limit_mb = _planning_limit(candidate, ram_limit_mb if candidate == "cpu" else vram_limit_mb)
        if limit_mb > 0:
            estimate = estimate_memory(model_dir, model_file, candidate)
            if estimate.max_tokens(limit_mb) < MIN_TOKENS:
                need = estimate.batch_mb(1, MIN_TOKENS)
                key = "local_embedding_ram_limit_mb" if candidate == "cpu" else "local_embedding_vram_limit_mb"
                reasons.append(f"{candidate}: [vector_db] {key} = {limit_mb:g} is too small for this model (needs at least about {need:,.0f} MB)")
                continue
        try:
            providers: list[Any] = [(provider, provider_options)]
            if candidate != "cpu":
                # GPU で動かせない演算があれば CPU で動かす（onnxruntime の通常の使い方）。
                providers.append("CPUExecutionProvider")
            session = ort.InferenceSession(str(model_file), sess_options=options, providers=providers)
        except Exception as e:  # noqa: BLE001 - onnxruntime の例外は種類が多いため、まとめて理由を返す
            reasons.append(f"{candidate}: {e}")
            continue
        # CUDA の DLL が読めない等で GPU の実行プロバイダーを作れないと、onnxruntime は警告だけ出して CPU で動かす。
        if session.get_providers()[0] != provider:
            reasons.append(f"{candidate}: onnxruntime fell back to {session.get_providers()[0]} (check the GPU driver and the CUDA/cuDNN DLLs)")
            continue
        if device == "auto" and reasons:
            # auto は入っているものを使う設定のため、先の候補が使えないのは想定どおり（警告にしない）。
            logger.info("Built-in embeddings: using %s (%s)", candidate, "; ".join(reasons))
        return session, candidate
    raise LocalEmbeddingError(f"Cannot use the device {device!r} for the built-in embeddings: {'; '.join(reasons)}")


_cpu_allocator_registered: bool | None = None


def _register_cpu_allocator(ort: Any) -> bool:
    """CPU のメモリープールを「足りない分だけ広げる」設定にして、プロセスの共有の割り当て器として登録する（1回だけ）。

    onnxruntime の既定のプールは、足りなくなると倍々の単位で確保し、確保した領域を手放さない。入力の形（件数 × トークン数）が
    毎回違う取り込みでは、どの形の分も持ち続けて1回の計算の倍以上に膨らむ（実測: 1回 約 4GB の計算の繰り返しで 8.6GB）。
    必要な分だけ広げ（arena_extend_strategy = kSameAsRequested）、計算の終わりに使っていない領域を返す
    （_CPU_RUN_SHRINK。_Model.run_options）と、速さを変えずに 3.9GB に収まった。プールを使わない設定
    （enable_cpu_mem_arena = False）でも収まるが、約 1.7 倍遅くなる。
    """
    global _cpu_allocator_registered
    if _cpu_allocator_registered is None:
        try:
            memory_info = ort.OrtMemoryInfo("Cpu", ort.OrtAllocatorType.ORT_ARENA_ALLOCATOR, 0, ort.OrtMemType.DEFAULT)
            # OrtArenaCfg(max_mem=0（上限なし）, arena_extend_strategy=1（kSameAsRequested）, 既定, 既定)
            ort.create_and_register_allocator(memory_info, ort.OrtArenaCfg(0, 1, -1, -1))
            _cpu_allocator_registered = True
        except Exception as e:  # noqa: BLE001 - 登録できなくても既定のプールで動く（ピークが上限を超えることがある）
            logger.warning("Built-in embeddings: cannot register the CPU memory allocator (the RAM limit may be exceeded): %s", e)
            _cpu_allocator_registered = False
    return _cpu_allocator_registered


def _run_options(ort: Any, device: str, limit_mb: float) -> Any:
    """計算の終わりに、使っていないメモリープールの領域を返させる RunOptions（cpu で上限があるときだけ。無ければ None）。"""
    if device != "cpu" or limit_mb <= 0:
        return None
    options = ort.RunOptions()
    options.add_run_config_entry("memory.enable_memory_arena_shrinkage", "cpu:0")
    return options


def _planning_limit(device: str, limit_mb: float) -> float:
    """1回の計算を計画するときに使う上限（DirectML は確保した VRAM を持ち続けるため、上限の _DIRECTML_LIMIT_RATIO）。"""
    return limit_mb * _DIRECTML_LIMIT_RATIO if device == "directml" and limit_mb > 0 else limit_mb


def _padded(length: int, multiple: int) -> int:
    return -(-length // multiple) * multiple if multiple > 1 else length


def _load(
    model_dir: Path,
    threads: int,
    max_tokens: int,
    device: str = "cpu",
    device_id: int = 0,
    ram_limit_mb: float = 0.0,
    vram_limit_mb: float = 0.0,
) -> _Model:
    """model_dir のモデルを読み込む（同じフォルダ・設定ならプロセス内で使い回す）。"""
    key = (str(model_dir.resolve()), threads, max_tokens, device, device_id, ram_limit_mb, vram_limit_mb)
    with _models_lock:
        model = _models.get(key)
        if model is not None:
            return model
        model_file = find_model_file(model_dir)
        if model_file is None or not (model_dir / TOKENIZER_FILE).is_file():
            raise LocalEmbeddingError(
                f"No built-in embedding model in {model_dir} (needs onnx/model.onnx and {TOKENIZER_FILE}; "
                "get it with tools/download_embedding_model.py)."
            )
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as e:
            raise LocalEmbeddingError(f"onnxruntime and tokenizers are needed for the built-in embeddings (pip install -r requirements.txt): {e}") from e
        session, used = _create_session(ort, model_dir, model_file, device, threads, device_id, ram_limit_mb, vram_limit_mb)
        limit_mb = _planning_limit(used, ram_limit_mb if used == "cpu" else vram_limit_mb)
        pad_multiple = _DIRECTML_PAD_MULTIPLE if used == "directml" else 1
        estimate = estimate_memory(model_dir, model_file, used) if limit_mb > 0 else None
        effective_tokens = max_tokens
        if estimate is not None:
            effective_tokens = min(max_tokens, estimate.max_tokens(limit_mb))
            if pad_multiple > 1:
                # 切り上げた長さでも上限に収まるよう、切り詰める長さも単位にそろえる。
                effective_tokens = max(effective_tokens // pad_multiple * pad_multiple, min(effective_tokens, MIN_TOKENS))
        try:
            tokenizer = Tokenizer.from_file(str(model_dir / TOKENIZER_FILE))
        except Exception as e:  # noqa: BLE001 - tokenizers の例外は種類が多いため、まとめて理由を返す
            raise LocalEmbeddingError(f"Cannot load the tokenizer of the built-in embedding model {model_dir}: {e}") from e
        padding = tokenizer.padding or {}
        pad_id = int(padding.get("pad_id", 0) or 0)
        # パディングは自分で付ける（tokenizer.json の設定のままだと encode_batch が付けたパディングと区別できない）。
        tokenizer.no_padding()
        tokenizer.enable_truncation(effective_tokens)
        input_names = tuple(i.name for i in session.get_inputs())
        output_names = tuple(o.name for o in session.get_outputs())
        if "input_ids" not in input_names:
            raise LocalEmbeddingError(f"{model_file} has no input_ids input (inputs: {', '.join(input_names)}).")
        if "sentence_embedding" not in output_names and "last_hidden_state" not in output_names and len(output_names) != 1:
            raise LocalEmbeddingError(
                f"{model_file} has neither sentence_embedding nor last_hidden_state output (outputs: {', '.join(output_names)})."
            )
        model = _Model(
            session,
            tokenizer,
            read_pooling(model_dir),
            input_names,
            output_names,
            pad_id,
            threading.Lock(),
            used,
            limit_mb,
            estimate,
            effective_tokens,
            pad_multiple,
            _run_options(ort, used, limit_mb),
        )
        logger.info(
            "Built-in embeddings: loaded %s on %s (pooling=%s, %s, %s, max_tokens=%d)",
            model_file,
            used,
            model.pooling,
            f"threads={threads or default_threads()}" if used == "cpu" else f"device_id={device_id}",
            f"{'RAM' if used == 'cpu' else 'VRAM'} limit {ram_limit_mb if used == 'cpu' else vram_limit_mb:g} MB (planning with {limit_mb:g} MB)"
            if limit_mb > 0
            else "no memory limit",
            effective_tokens,
        )
        _models[key] = model
        return model


def embed(
    model_dir: Path,
    texts: list[str],
    *,
    threads: int = 0,
    max_tokens: int = 8192,
    device: str = "cpu",
    device_id: int = 0,
    ram_limit_mb: float = 0.0,
    vram_limit_mb: float = 0.0,
) -> tuple[list[list[float]], int]:
    """texts の埋め込み（L2 正規化済み）を入力と同じ順で返す。2つ目の戻り値は入力のトークン数の合計。

    device は DEVICES のいずれか、device_id は GPU の番号（cuda・directml のときだけ使う）。threads は cpu のときだけ使う。
    ram_limit_mb（cpu）・vram_limit_mb（cuda・directml）が 0 より大きければ、1回の計算がその量に収まるよう
    長すぎる文を切り詰め、texts を何回かに分けて計算する（plan_batches。0 なら max_tokens で切るだけで、まとめて1回）。

    Raises:
        LocalEmbeddingError: モデルを読めない・計算できない場合。
    """
    if not texts:
        return [], 0
    import numpy as np

    model = _load(model_dir, threads, max_tokens, device, device_id, ram_limit_mb, vram_limit_mb)
    encodings = model.tokenizer.encode_batch(list(texts))
    lengths = [len(e.ids) for e in encodings]
    if "sentence_embedding" in model.output_names:
        output = "sentence_embedding"
    elif "last_hidden_state" in model.output_names:
        output = "last_hidden_state"
    else:
        output = model.output_names[0]
    vectors: list[Any] = [None] * len(texts)
    padded = [_padded(max(length, 1), model.pad_multiple) for length in lengths]
    for batch in plan_batches(padded, model.estimate, model.memory_limit_mb):
        width = max(padded[i] for i in batch)
        ids = np.full((len(batch), width), model.pad_id, dtype=np.int64)
        mask = np.zeros((len(batch), width), dtype=np.int64)
        for row, index in enumerate(batch):
            ids[row, : lengths[index]] = encodings[index].ids
            mask[row, : lengths[index]] = 1
        feeds = {"input_ids": ids}
        if "attention_mask" in model.input_names:
            feeds["attention_mask"] = mask
        if "token_type_ids" in model.input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        try:
            with model.lock:
                result = model.session.run([output], feeds, model.run_options)[0]
        except Exception as e:  # noqa: BLE001 - onnxruntime の例外（メモリー不足等）は種類が多いため、まとめて理由を返す
            raise LocalEmbeddingError(f"The built-in embedding model failed ({len(batch)} texts of up to {width} tokens): {e}") from e
        result = np.asarray(result, dtype=np.float32)
        if result.ndim == 3:
            result = _pool(result, mask, model.pooling)
        if result.ndim != 2:
            raise LocalEmbeddingError(f"The built-in embedding model returned an unexpected shape {result.shape}.")
        norms = np.linalg.norm(result, axis=1, keepdims=True)
        result = result / np.maximum(norms, 1e-12)
        for row, index in enumerate(batch):
            vectors[index] = result[row].tolist()
    return vectors, sum(lengths)


def _pool(hidden: Any, mask: Any, pooling: str) -> Any:
    """トークンごとのベクター（batch × 長さ × 次元）を1文1ベクターにする。"""
    import numpy as np

    if pooling == "cls":
        return hidden[:, 0]
    if pooling == "lasttoken":
        last = np.maximum(mask.sum(axis=1) - 1, 0)
        return hidden[np.arange(hidden.shape[0]), last]
    weights = mask[:, :, None].astype(np.float32)
    return (hidden * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1e-9)


def clear_cache() -> None:
    """読み込んだモデルを捨てる（テスト用）。"""
    with _models_lock:
        _models.clear()
