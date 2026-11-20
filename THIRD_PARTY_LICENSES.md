# 第三者ライセンス告知 (THIRD_PARTY_LICENSES)

本ファイルは Locohane が実行時に利用する第三者オープンソース
パッケージとそのライセンスの一覧です（tools/gen_licenses.py で自動生成 / 生成日: 2026-11-21）。
対象は requirements.txt の直接依存から到達する実行時の推移的依存集合です。

再生成:

```cmd
call python_env.bat
python tools/gen_licenses.py
```

## ライセンス種別サマリ

| ライセンス種別 | パッケージ数 |
|---|---:|
| Apache 2.0 | 89 |
| MIT | 69 |
| BSD | 38 |
| PSF | 4 |
| MPL 2.0 | 2 |
| LGPL | 1 |
| **合計** | **203** |

> 寛容ライセンス（MIT / Apache 2.0 / BSD / PSF / ISC）、ファイル単位の弱いコピーレフト（MPL 2.0）、
> ライブラリ単位の弱いコピーレフト（LGPL）だけであれば、改変せず依存として利用する
> 限り本ソフトウェアへの組み込み・商用配布が可能です。
> GPL / AGPL は含まれません。
> LGPL: casadi（3D ビューア用の cadquery の依存）。改変せず、pip で入れた別のライブラリとして
> 使う限り商用利用できます。改変して配布する場合や、1 つの実行ファイルへ静的にまとめる場合は LGPL の条項に従ってください。
> 各パッケージを改変する場合は当該ライセンス条項に従ってください。
>
> **改変禁止**: bidict, casadi, certifi, orjson, tqdm、および cadquery-ocp に同梱の OpenCASCADE 本体（LGPL / MPL 2.0）。
> ファイルの書き換え・パッチ・フォーク版への差し替えをせず、LGPL のものは静的にリンクしないこと
> （README_DETAIL.md「依存パッケージ（商用利用可）」の「改変禁止のパッケージ」参照）。

> `cadquery-ocp`（OCP）はラッパー自体は Apache 2.0 ですが、OpenCASCADE Technology（OCCT）の
> ビルド済みライブラリを同梱しており、OCCT は LGPL-2.1 + Open CASCADE Exception です（上の集計では
> パッケージのメタデータどおり Apache 2.0 に数えています）。

> `pypdfium2` はビルド済みPDFiumバイナリに libpng / LibTIFF / FreeType（FTL） / zlib /
> libjpeg-turbo / ICU 等の第三者コードを同梱しており、パッケージの
> `LicenseRef-PdfiumThirdParty` にその全文が含まれます。確認の結果、いずれも寛容
> ライセンスでGPL等の混入はありません。

## パッケージ一覧

| パッケージ | バージョン | ライセンス | URL |
|---|---|---|---|
| aiofiles | 25.1.0 | Apache Software License | https://github.com/Tinche/aiofiles |
| aiohappyeyeballs | 2.7.1 | Python Software Foundation License | https://github.com/aio-libs/aiohappyeyeballs |
| aiohttp | 3.14.1 | Apache-2.0 AND MIT | https://github.com/aio-libs/aiohttp |
| aiosignal | 1.4.0 | Apache Software License | https://github.com/aio-libs/aiosignal |
| aiosqlite | 0.22.1 | MIT License |  |
| annotated-doc | 0.0.4 | MIT | https://github.com/fastapi/annotated-doc |
| annotated-types | 0.7.0 | MIT License | https://github.com/annotated-types/annotated-types |
| anyio | 4.14.1 | MIT | https://github.com/agronholm/anyio |
| asyncer | 0.0.18 | MIT | https://github.com/fastapi/asyncer |
| attrs | 26.1.0 | MIT | https://tidelift.com/subscription/pkg/pypi-attrs?utm_source=pypi-attrs&utm_medium=pypi |
| bidict | 0.23.1 | Mozilla Public License 2.0 (MPL 2.0) | https://github.com/jab/bidict |
| cadquery | 2.8.0 | Apache Software License | https://github.com/CadQuery/cadquery |
| cadquery-ocp | 7.9.3.1.1 | Apache Software License |  |
| cadquery-ocp-proxy | 7.9.3.1.1 | Apache Software License |  |
| casadi | 3.8.2 | GNU Lesser General Public License v3 or later (LGPLv3+) | http://casadi.org |
| certifi | 2026.6.17 | Mozilla Public License 2.0 (MPL 2.0) | https://github.com/certifi/python-certifi |
| chainlit | 2.12.0 | Apache-2.0 | https://chainlit.io/ |
| charset-normalizer | 3.4.9 | MIT |  |
| chevron | 0.14.0 | MIT License | https://github.com/noahmorrison/chevron |
| click | 8.4.2 | BSD-3-Clause | https://github.com/pallets/click/ |
| colorama | 0.4.6 | BSD License | https://github.com/tartley/colorama |
| contourpy | 1.3.3 | BSD License | https://github.com/contourpy/contourpy |
| cuid | 0.4 | Apache Software License | http://github.com/necaris/cuid.py |
| cycler | 0.12.1 | BSD License | https://matplotlib.org/cycler/ |
| dataclasses-json | 0.6.7 | MIT License | https://github.com/lidatong/dataclasses-json |
| deprecated | 1.3.1 | MIT License | https://github.com/laurent-laporte-pro/deprecated |
| distro | 1.9.0 | Apache Software License | https://github.com/python-distro/distro |
| et-xmlfile | 2.0.0 | MIT License | https://foss.heptapod.net/openpyxl/et_xmlfile |
| exceptiongroup | 1.3.1 | MIT License | https://github.com/agronholm/exceptiongroup |
| ezdxf | 1.4.5 | MIT License | https://github.com/mozman/ezdxf |
| fastapi | 0.139.0 | MIT | https://github.com/fastapi/fastapi |
| filelock | 4.0.8 | MIT | https://github.com/tox-dev/py-filelock |
| filetype | 1.2.0 | MIT License | https://github.com/h2non/filetype.py |
| flatbuffers | 25.12.19 | Apache Software License | https://google.github.io/flatbuffers/ |
| fonttools | 4.63.0 | MIT | http://github.com/fonttools/fonttools |
| frozenlist | 1.8.0 | Apache-2.0 | https://github.com/aio-libs/frozenlist |
| fsspec | 2026.9.0 | BSD-3-Clause | https://github.com/fsspec/filesystem_spec |
| googleapis-common-protos | 1.75.0 | Apache Software License | https://github.com/googleapis/google-cloud-python/tree/main/packages/googleapis-common-protos |
| grpcio | 1.82.1 | Apache-2.0 | https://grpc.io |
| h11 | 0.16.0 | MIT License | https://github.com/python-hyper/h11 |
| hf-xet | 1.6.0 | Apache-2.0 | https://github.com/huggingface/xet-core |
| httpcore | 1.0.9 | BSD-3-Clause | https://www.encode.io/httpcore/ |
| httpcore2 | 2.13.1 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| httpx | 0.28.1 | BSD License | https://github.com/encode/httpx |
| httpx-sse | 0.4.3 | MIT | https://github.com/florimondmanca/httpx-sse |
| httpx2 | 2.13.1 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| huggingface-hub | 2.1.1 | Apache Software License | https://github.com/huggingface/huggingface_hub |
| idna | 3.18 | BSD-3-Clause | https://github.com/kjd/idna |
| inflection | 0.5.1 | MIT License | https://github.com/jpvanhal/inflection |
| jinja2 | 3.1.6 | BSD License | https://github.com/pallets/jinja/ |
| jiter | 0.16.0 | MIT | https://github.com/pydantic/jiter/ |
| jmespath | 1.1.0 | MIT License | https://github.com/jmespath/jmespath.py |
| jsonpatch | 1.33 | BSD License | https://github.com/stefankoegl/python-json-patch |
| jsonpointer | 3.1.1 | BSD License | https://github.com/stefankoegl/python-json-pointer |
| jsonschema | 4.26.0 | MIT | https://github.com/python-jsonschema/jsonschema |
| jsonschema-specifications | 2025.9.1 | MIT | https://github.com/python-jsonschema/jsonschema-specifications |
| kiwisolver | 1.5.1 | BSD License | https://github.com/nucleic/kiwi |
| langchain-core | 1.4.9 | MIT License | https://docs.langchain.com/ |
| langchain-openai | 1.3.5 | MIT License | https://docs.langchain.com/oss/python/integrations/providers/openai |
| langchain-protocol | 0.0.18 | MIT License | https://github.com/langchain-ai/agent-protocol/tree/main/streaming |
| langgraph | 1.2.9 | MIT | https://docs.langchain.com/oss/python/langgraph/overview |
| langgraph-checkpoint | 4.3.1 | MIT | https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint |
| langgraph-checkpoint-sqlite | 3.1.2 | MIT | https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-sqlite |
| langgraph-prebuilt | 1.1.0 | MIT | https://github.com/langchain-ai/langgraph/tree/main/libs/prebuilt |
| langgraph-sdk | 0.4.2 | MIT | https://github.com/langchain-ai/langgraph/tree/main/libs/sdk-py |
| langsmith | 0.10.2 | MIT | https://smith.langchain.com/ |
| lazify | 0.4.0 | BSD | https://github.com/numberly/lazify |
| literalai | 0.1.201 | Apache License 2.0 |  |
| llvmlite | 0.50.0 | BSD-2-Clause; Apache-2.0 WITH LLVM-exception (bundled LLVM) | http://llvmlite.readthedocs.io |
| lxml | 6.1.1 | BSD-3-Clause | https://lxml.de/ |
| markupsafe | 3.0.3 | BSD-3-Clause | https://github.com/pallets/markupsafe/ |
| marshmallow | 3.26.2 | MIT License | https://github.com/marshmallow-code/marshmallow |
| matplotlib | 3.11.2 | Python Software Foundation License | https://matplotlib.org |
| mcp | 1.30.0 | MIT License | https://modelcontextprotocol.io |
| more-itertools | 11.1.0 | MIT | https://github.com/more-itertools/more-itertools |
| msgpack | 1.2.4 | Apache-2.0 | https://msgpack.org/ |
| multidict | 6.7.1 | Apache License 2.0 | https://github.com/aio-libs/multidict |
| multimethod | 1.12 | Apache Software License | https://github.com/coady/multimethod |
| mypy-extensions | 1.1.0 | MIT | https://github.com/python/mypy_extensions |
| nest-asyncio | 1.6.0 | BSD License | https://github.com/erdewit/nest_asyncio |
| nlopt | 2.11.0 | MIT License |  |
| numba | 0.68.1 | BSD License | https://numba.pydata.org |
| numpy | 2.3.5 | BSD License | https://numpy.org |
| onnxruntime | 1.31.1 | MIT License | https://onnxruntime.ai |
| openai | 2.54.0 | Apache Software License | https://github.com/openai/openai-python |
| openpyxl | 3.1.5 | MIT License | https://openpyxl.readthedocs.io |
| opentelemetry-api | 1.43.0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-api |
| opentelemetry-exporter-otlp-proto-common | 1.43.0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-otlp-proto-common |
| opentelemetry-exporter-otlp-proto-grpc | 1.43.0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-otlp-proto-grpc |
| opentelemetry-exporter-otlp-proto-http | 1.43.0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-otlp-proto-http |
| opentelemetry-instrumentation | 0.64b0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/opentelemetry-instrumentation |
| opentelemetry-instrumentation-agno | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-agno |
| opentelemetry-instrumentation-alephalpha | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-alephalpha |
| opentelemetry-instrumentation-anthropic | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-anthropic |
| opentelemetry-instrumentation-bedrock | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-bedrock |
| opentelemetry-instrumentation-chromadb | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-chromadb |
| opentelemetry-instrumentation-cohere | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-cohere |
| opentelemetry-instrumentation-crewai | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-crewai |
| opentelemetry-instrumentation-google-generativeai | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-google-generativeai |
| opentelemetry-instrumentation-groq | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-groq |
| opentelemetry-instrumentation-haystack | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-haystack |
| opentelemetry-instrumentation-lancedb | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-lancedb |
| opentelemetry-instrumentation-langchain | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-langchain |
| opentelemetry-instrumentation-litellm | 0.1.0 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-litellm |
| opentelemetry-instrumentation-llamaindex | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-llamaindex |
| opentelemetry-instrumentation-logging | 0.64b0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/instrumentation/opentelemetry-instrumentation-logging |
| opentelemetry-instrumentation-marqo | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-marqo |
| opentelemetry-instrumentation-mcp | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-mcp |
| opentelemetry-instrumentation-milvus | 0.60.0 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-milvus |
| opentelemetry-instrumentation-mistralai | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-mistralai |
| opentelemetry-instrumentation-ollama | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-ollama |
| opentelemetry-instrumentation-openai | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-openai |
| opentelemetry-instrumentation-openai-agents | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-openai-agents |
| opentelemetry-instrumentation-pinecone | 0.60.0 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-pinecone |
| opentelemetry-instrumentation-qdrant | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-qdrant |
| opentelemetry-instrumentation-redis | 0.64b0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/instrumentation/opentelemetry-instrumentation-redis |
| opentelemetry-instrumentation-replicate | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-replicate |
| opentelemetry-instrumentation-requests | 0.64b0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/instrumentation/opentelemetry-instrumentation-requests |
| opentelemetry-instrumentation-sagemaker | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-sagemaker |
| opentelemetry-instrumentation-sqlalchemy | 0.64b0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/instrumentation/opentelemetry-instrumentation-sqlalchemy |
| opentelemetry-instrumentation-threading | 0.64b0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python-contrib/instrumentation/opentelemetry-instrumentation-threading |
| opentelemetry-instrumentation-together | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-together |
| opentelemetry-instrumentation-transformers | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-transformers |
| opentelemetry-instrumentation-urllib3 | 0.64b0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/instrumentation/opentelemetry-instrumentation-urllib3 |
| opentelemetry-instrumentation-vertexai | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-vertexai |
| opentelemetry-instrumentation-voyageai | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-voyageai |
| opentelemetry-instrumentation-watsonx | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-watsonx |
| opentelemetry-instrumentation-weaviate | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-weaviate |
| opentelemetry-instrumentation-writer | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-writer |
| opentelemetry-proto | 1.43.0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-proto |
| opentelemetry-sdk | 1.43.0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-sdk |
| opentelemetry-semantic-conventions | 0.64b0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-semantic-conventions |
| opentelemetry-semantic-conventions-ai | 0.5.1 | Apache-2.0 |  |
| opentelemetry-util-http | 0.64b0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/util/opentelemetry-util-http |
| orjson | 3.11.9 | MPL-2.0 AND (Apache-2.0 OR MIT) | https://github.com/ijl/orjson |
| ormsgpack | 1.12.2 | Apache-2.0 OR MIT | https://github.com/ormsgpack/ormsgpack |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| pillow | 13.0.0 | MIT-CMU | https://python-pillow.org |
| propcache | 0.5.2 | Apache Software License | https://github.com/aio-libs/propcache |
| protobuf | 7.35.1 | 3-Clause BSD License | https://developers.google.com/protocol-buffers/ |
| pydantic | 2.13.4 | MIT | https://github.com/pydantic/pydantic |
| pydantic-core | 2.46.4 | MIT | https://github.com/pydantic/pydantic |
| pydantic-settings | 2.14.2 | MIT | https://github.com/pydantic/pydantic-settings |
| pyjwt | 2.13.0 | MIT | https://github.com/jpadilla/pyjwt |
| pyparsing | 3.3.2 | MIT | https://github.com/pyparsing/pyparsing/ |
| pypdf | 6.21.4 | BSD-3-Clause | https://github.com/py-pdf/pypdf |
| pypdfium2 | 4.30.0 | (Apache-2.0 OR BSD-3-Clause) AND LicenseRef-PdfiumThirdParty | https://github.com/pypdfium2-team/pypdfium2 |
| python-calamine | 0.9.0 | MIT | https://github.com/dimastbk/python-calamine |
| python-dateutil | 2.9.0.post0 | BSD License; Apache Software License | https://github.com/dateutil/dateutil |
| python-docx | 1.2.0 | MIT License | https://github.com/python-openxml/python-docx |
| python-dotenv | 1.2.2 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| python-engineio | 4.13.3 | MIT | https://github.com/miguelgrinberg/python-engineio |
| python-multipart | 0.0.32 | Apache-2.0 | https://github.com/Kludex/python-multipart |
| python-pptx | 1.0.2 | MIT License | https://github.com/scanny/python-pptx |
| python-socketio | 5.16.3 | MIT | https://github.com/miguelgrinberg/python-socketio |
| pywin32 | 311 | Python Software Foundation License | https://github.com/mhammond/pywin32 |
| pyyaml | 6.0.3 | MIT License | https://pyyaml.org/ |
| referencing | 0.37.0 | MIT | https://github.com/python-jsonschema/referencing |
| regex | 2026.7.10 | Apache-2.0 AND CNRI-Python | https://github.com/mrabarnett/mrab-regex |
| reportlab | 4.5.1 | BSD License | https://www.reportlab.com/ |
| requests | 2.34.2 | Apache Software License | https://github.com/psf/requests |
| requests-toolbelt | 1.0.0 | Apache Software License | https://toolbelt.readthedocs.io/ |
| rpds-py | 2026.6.3 | MIT | https://github.com/crate-py/rpds |
| runtype | 0.5.3 | MIT License | https://github.com/erezsh/runtype |
| scipy | 1.17.1 | BSD License | https://scipy.org/ |
| simple-websocket | 1.1.0 | MIT License | https://github.com/miguelgrinberg/simple-websocket |
| six | 1.17.0 | MIT License | https://github.com/benjaminp/six |
| sniffio | 1.3.1 | MIT License; Apache Software License | https://github.com/python-trio/sniffio |
| sqlite-vec | 0.1.9 | MIT License, Apache License, Version 2.0 | https://TODO.com |
| sse-starlette | 3.4.5 | BSD-3-Clause | https://github.com/sysid/sse-starlette |
| starlette | 1.3.1 | BSD-3-Clause | https://github.com/Kludex/starlette |
| syncer | 2.0.3 | MIT License | https://github.com/miyakogi/syncer |
| tenacity | 9.1.4 | Apache Software License | https://github.com/jd/tenacity |
| tiktoken | 0.13.0 | MIT License | https://github.com/openai/tiktoken |
| tokenizers | 0.23.3 | Apache Software License | https://github.com/huggingface/tokenizers |
| tomli | 2.4.1 | MIT | https://github.com/hukkin/tomli |
| tqdm | 4.68.4 | MPL-2.0 AND MIT | https://tqdm.github.io |
| traceloop-sdk | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry |
| trame | 4.0.0 | Apache Software License |  |
| trame-client | 4.1.4 | MIT License |  |
| trame-common | 1.3.0 | Apache Software License |  |
| trame-components | 2.5.0 | Apache Software License |  |
| trame-server | 4.1.2 | Apache Software License |  |
| trame-vtk | 2.11.17 | BSD License |  |
| trame-vuetify | 3.3.0 | MIT License |  |
| truststore | 0.10.4 | MIT | https://github.com/sethmlarson/truststore |
| typing-extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| typing-inspect | 0.9.0 | MIT License | https://github.com/ilevkivskyi/typing_inspect |
| typing-inspection | 0.4.2 | MIT | https://github.com/pydantic/typing-inspection |
| urllib3 | 2.7.0 | MIT |  |
| uuid-utils | 0.17.0 | BSD-3-Clause | https://github.com/aminalaee/uuid-utils |
| uvicorn | 0.51.0 | BSD-3-Clause | https://uvicorn.dev/ |
| vtk | 9.6.2 | BSD License | https://vtk.org |
| watchfiles | 1.2.0 | MIT License | https://github.com/samuelcolvin/watchfiles |
| websockets | 15.0.1 | BSD License | https://github.com/python-websockets/websockets |
| wrapt | 2.2.2 | BSD-2-Clause | https://github.com/GrahamDumpleton/wrapt |
| wslink | 2.6.0 | BSD License |  |
| wsproto | 1.3.2 | MIT | https://github.com/python-hyper/wsproto/ |
| xlrd | 2.0.2 | BSD License | http://www.python-excel.org/ |
| xlsxwriter | 3.2.9 | BSD License | https://github.com/jmcnamara/XlsxWriter |
| xxhash | 3.8.1 | BSD-2-Clause | https://github.com/ifduyue/python-xxhash |
| yarl | 1.24.2 | Apache-2.0 | https://github.com/aio-libs/yarl |
| zstandard | 0.25.0 | BSD-3-Clause | https://github.com/indygreg/python-zstandard |
