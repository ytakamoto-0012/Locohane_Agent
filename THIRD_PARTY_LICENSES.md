# 第三者ライセンス告知 (THIRD_PARTY_LICENSES)

本ファイルは Locohane が実行時に利用する第三者オープンソース
パッケージとそのライセンスの一覧です（tools/gen_licenses.py で自動生成 / 生成日: 2026-10-28）。
対象は requirements.txt の直接依存から到達する実行時の推移的依存集合です。

再生成:

```cmd
call python_env.bat
python tools/gen_licenses.py
```

## ライセンス種別サマリ

| ライセンス種別 | パッケージ数 |
|---|---:|
| Apache 2.0 | 74 |
| MIT | 57 |
| BSD | 26 |
| PSF | 3 |
| MPL 2.0 | 2 |
| **合計** | **162** |

> いずれも寛容ライセンス（MIT / Apache 2.0 / BSD / PSF / ISC）または
> ファイル単位の弱いコピーレフト（MPL 2.0）であり、改変せず依存として利用する
> 限り本ソフトウェアへの組み込み・商用配布が可能です。
> GPL / AGPL / LGPL は含まれません。各パッケージを改変する場合は当該ライセンス条項に従ってください。

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
| certifi | 2026.6.17 | Mozilla Public License 2.0 (MPL 2.0) | https://github.com/certifi/python-certifi |
| chainlit | 2.12.0 | Apache-2.0 | https://chainlit.io/ |
| charset-normalizer | 3.4.9 | MIT |  |
| chevron | 0.14.0 | MIT License | https://github.com/noahmorrison/chevron |
| click | 8.4.2 | BSD-3-Clause | https://github.com/pallets/click/ |
| colorama | 0.4.6 | BSD License | https://github.com/tartley/colorama |
| cuid | 0.4 | Apache Software License | http://github.com/necaris/cuid.py |
| dataclasses-json | 0.6.7 | MIT License | https://github.com/lidatong/dataclasses-json |
| deprecated | 1.3.1 | MIT License | https://github.com/laurent-laporte-pro/deprecated |
| distro | 1.9.0 | Apache Software License | https://github.com/python-distro/distro |
| et-xmlfile | 2.0.0 | MIT License | https://foss.heptapod.net/openpyxl/et_xmlfile |
| exceptiongroup | 1.3.1 | MIT License | https://github.com/agronholm/exceptiongroup |
| fastapi | 0.139.0 | MIT | https://github.com/fastapi/fastapi |
| filetype | 1.2.0 | MIT License | https://github.com/h2non/filetype.py |
| frozenlist | 1.8.0 | Apache-2.0 | https://github.com/aio-libs/frozenlist |
| googleapis-common-protos | 1.75.0 | Apache Software License | https://github.com/googleapis/google-cloud-python/tree/main/packages/googleapis-common-protos |
| grpcio | 1.82.1 | Apache-2.0 | https://grpc.io |
| h11 | 0.16.0 | MIT License | https://github.com/python-hyper/h11 |
| httpcore | 1.0.9 | BSD-3-Clause | https://www.encode.io/httpcore/ |
| httpx | 0.28.1 | BSD License | https://github.com/encode/httpx |
| httpx-sse | 0.4.3 | MIT | https://github.com/florimondmanca/httpx-sse |
| idna | 3.18 | BSD-3-Clause | https://github.com/kjd/idna |
| inflection | 0.5.1 | MIT License | https://github.com/jpvanhal/inflection |
| jinja2 | 3.1.6 | BSD License | https://github.com/pallets/jinja/ |
| jiter | 0.16.0 | MIT | https://github.com/pydantic/jiter/ |
| jmespath | 1.1.0 | MIT License | https://github.com/jmespath/jmespath.py |
| jsonpatch | 1.33 | BSD License | https://github.com/stefankoegl/python-json-patch |
| jsonpointer | 3.1.1 | BSD License | https://github.com/stefankoegl/python-json-pointer |
| jsonschema | 4.26.0 | MIT | https://github.com/python-jsonschema/jsonschema |
| jsonschema-specifications | 2025.9.1 | MIT | https://github.com/python-jsonschema/jsonschema-specifications |
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
| lxml | 6.1.1 | BSD-3-Clause | https://lxml.de/ |
| markupsafe | 3.0.3 | BSD-3-Clause | https://github.com/pallets/markupsafe/ |
| marshmallow | 3.26.2 | MIT License | https://github.com/marshmallow-code/marshmallow |
| mcp | 1.30.0 | MIT License | https://modelcontextprotocol.io |
| multidict | 6.7.1 | Apache License 2.0 | https://github.com/aio-libs/multidict |
| mypy-extensions | 1.1.0 | MIT | https://github.com/python/mypy_extensions |
| nest-asyncio | 1.6.0 | BSD License | https://github.com/erdewit/nest_asyncio |
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
| pypdf | 6.21.2 | BSD-3-Clause | https://github.com/py-pdf/pypdf |
| pypdfium2 | 4.30.0 | (Apache-2.0 OR BSD-3-Clause) AND LicenseRef-P | https://github.com/pypdfium2-team/pypdfium2 |
| python-calamine | 0.9.0 | MIT | https://github.com/dimastbk/python-calamine |
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
| simple-websocket | 1.1.0 | MIT License | https://github.com/miguelgrinberg/simple-websocket |
| sniffio | 1.3.1 | MIT License; Apache Software License | https://github.com/python-trio/sniffio |
| sqlite-vec | 0.1.9 | MIT License, Apache License, Version 2.0 | https://TODO.com |
| sse-starlette | 3.4.5 | BSD-3-Clause | https://github.com/sysid/sse-starlette |
| starlette | 1.3.1 | BSD-3-Clause | https://github.com/Kludex/starlette |
| syncer | 2.0.3 | MIT License | https://github.com/miyakogi/syncer |
| tenacity | 9.1.4 | Apache Software License | https://github.com/jd/tenacity |
| tiktoken | 0.13.0 | MIT License | https://github.com/openai/tiktoken |
| tomli | 2.4.1 | MIT | https://github.com/hukkin/tomli |
| tqdm | 4.68.4 | MPL-2.0 AND MIT | https://tqdm.github.io |
| traceloop-sdk | 0.62.1 | Apache-2.0 | https://github.com/traceloop/openllmetry |
| typing-extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| typing-inspect | 0.9.0 | MIT License | https://github.com/ilevkivskyi/typing_inspect |
| typing-inspection | 0.4.2 | MIT | https://github.com/pydantic/typing-inspection |
| urllib3 | 2.7.0 | MIT |  |
| uuid-utils | 0.17.0 | BSD-3-Clause | https://github.com/aminalaee/uuid-utils |
| uvicorn | 0.51.0 | BSD-3-Clause | https://uvicorn.dev/ |
| watchfiles | 1.2.0 | MIT License | https://github.com/samuelcolvin/watchfiles |
| websockets | 15.0.1 | BSD License | https://github.com/python-websockets/websockets |
| wrapt | 2.2.2 | BSD-2-Clause | https://github.com/GrahamDumpleton/wrapt |
| wsproto | 1.3.2 | MIT | https://github.com/python-hyper/wsproto/ |
| xlrd | 2.0.2 | BSD License | http://www.python-excel.org/ |
| xlsxwriter | 3.2.9 | BSD License | https://github.com/jmcnamara/XlsxWriter |
| xxhash | 3.8.1 | BSD-2-Clause | https://github.com/ifduyue/python-xxhash |
| yarl | 1.24.2 | Apache-2.0 | https://github.com/aio-libs/yarl |
| zstandard | 0.25.0 | BSD-3-Clause | https://github.com/indygreg/python-zstandard |
