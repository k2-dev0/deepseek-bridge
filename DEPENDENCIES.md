# 固定dependency versions

runtime / development依存は`uv.lock`のexact versionと配布hashで固定する。
platform markerによりインストールされないpackageも含む。bridge自身は`0.1.0`。

```text
annotated-types==0.8.0
anyio==4.15.1
attrs==26.1.0
cffi==2.1.1
click==8.5.0
colorama==0.4.6
cryptography==50.0.1
deepseek-harness-runtime-bin==0.1.5rc1
deepseek-harness-sdk==0.1.5rc1
h11==0.16.0
httpcore2==2.13.0
httpx2==2.13.0
httpx2-jsfetch==1.0
idna==3.19
iniconfig==2.3.0
jsonschema==4.26.0
jsonschema-specifications==2025.9.1
mcp==2.2.0
mcp-types==2.2.0
mypy==1.18.1
mypy-extensions==1.1.0
opentelemetry-api==1.44.0
packaging==26.3
pathspec==1.1.1
platformdirs==4.4.0
pluggy==1.6.0
pycparser==3.0
pydantic==2.12.5
pydantic-core==2.41.5
pygments==2.21.0
pyjwt==2.14.0
pytest==8.4.2
pytest-asyncio==1.2.0
python-multipart==0.0.32
pywin32==312
pyyaml==6.0.2
referencing==0.37.0
rpds-py==2026.6.3
ruff==0.13.0
sse-starlette==3.4.11
starlette==1.6.0
truststore==0.10.4
types-pyyaml==6.0.12.20250915
typing-extensions==4.16.0
typing-inspection==0.4.4
uvicorn==0.53.0
```

build環境も`pyproject.toml`の`build-system.requires`で直接・間接依存を固定する。

```text
hatchling==1.27.0
packaging==26.3
pathspec==1.1.1
pluggy==1.6.0
trove-classifiers==2026.6.1.19
```

検証に使用したツール・コンテナ：

```text
Python 3.12.8
uv 0.5.9
python:3.12.8-slim-bookworm
sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
Docker platform: linux/amd64
```

`opentelemetry-api`は公式MCP SDKの依存。exporterやbridge独自のtelemetryを導入するものではない。
SDK runtime同梱のJavaScript依存はruntime wheelのversionとhashに含まれる。
