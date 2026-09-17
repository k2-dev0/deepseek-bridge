# 検証記録 — 2026-09-17

Python 3.12.8 / uv 0.5.9。SDK/runtimeは共に0.1.5rc1。
実APIへの課金を伴う接続は実行していない。model endpointだけをlocal HTTP/SSE fixtureへ置き換え、Harness SDKとruntimeは実物を起動した。

## TDD

- production code追加前に通常33件・wire6件がbridge未実装のassertionで失敗。
- 各test fileを単独commitし、cleanな状態でbaseline `5ab46744b50b7418224e73239bca4bb30bde9f05`を記録。
- JSON escapeで隠したcredential、completed時の空白questionについても失敗を確認してから修正。
- 追加検証により現在のtest数は54件。

## 結果

| 検証 | 結果 |
|---|---|
| 通常unit/integration、CLI module/console smoke | 44 passed |
| Linux x64 real SDK wire（Docker、network none） | 10 passed |
| macOS arm64 real SDK wire（loopback限定Seatbelt） | 7 passed、2 skipped、1 XFAIL |
| mypy strict | 7 source files、診断なし |
| Ruff lint / format | 診断なし |
| `uv lock --check --offline` | lock一致、47 package（bridge自身含む） |
| `uv build` | sdist/wheel生成成功 |
| wheel inspection | privacy profileが正本と一致、controller fileを含まない |

macOS XFAILは実shell起動時の`spawnSync /bin/ps EPERM`。SDKのsetuid process inspectionとこのhost sandboxの非互換であり、shell成功とは扱わない。
2件のskipはLinux専用の実行中shellのabort/shutdown回収検証。同じ2件はLinuxで通過した。

## Wireの観測結果

通常requestのtop-level field集合：

```json
["max_tokens", "messages", "model", "reasoning_effort", "stream", "stream_options", "thinking", "tools"]
```

privacy正本を適用せず、両metadata pluginを有効化したnegative fixture：

```json
["dsh_plugin_packages", "dsh_session_log", "max_tokens", "messages", "model", "reasoning_effort", "stream", "stream_options", "thinking", "tools"]
```

- model: `deepseek-flash`
- reasoning effort: `max`
- 通常入力と共通指示がmessagesに存在。同じsessionの続行には前turnの履歴が存在し、fresh sessionには混入しない。
- 通常task/続行/fresh sessionで同一runtime processを再利用。
- canary keyがAuthorizationに使われることを確認。通常body、stdout/stderr、bridge error、通常local state fileへの非露出を検査。
- 指定OTel collectorへのrequestは0件。
- 外部宛てsocket接続がOSによって拒否されることを確認。単なるtimeoutは合格条件から除外。
- Linuxの実shellが指定cwdで`example.py`とtestを作成し、`unittest`を実行。
- Linuxの実行中shellに対するabort/shutdown後に子PIDが存在しないこと、worker threadが残らないことを確認。
- 401/429/503/malformed streamはそれぞれ所定のsanitized error。各fixtureのmodel requestは1回。
- runtime crash後はfailed。abort後のfresh taskでは新しいruntime PID。

全外部宛ての**送信試行**をpacket captureで数えたわけではない。OSで外部egressを拒否し、設定したlocal collectorへの通信を観測した範囲の結果。
実DeepSeek serviceのデータ保持・請求・実modelによる指示遵守は、この検証では確認していない。

## 再現command

```bash
.venv/bin/python -m pytest -m 'not wire' -q
.venv/bin/mypy
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
sh tests/linux_wire.sh
.venv/bin/python tests/wire_sandbox.py
```

この開発環境の外部実行はREADMEの`outside.sh`経由commandを用いた。
ユーザーが適用した`.codex/hooks/shell/protected-exec.py`の変更は、uv一時cacheのmarker 1個への例外と、wire検証commandへのloopback制限。
`.codex/`は既存方針どおり管理外で、アプリの配布物には含めない。
