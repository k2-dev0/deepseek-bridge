# 検証記録

## 2026-09-18 — 現在の保護環境での再検証

復旧後のwire実行時HEADは`bbc6958ca160d56ddc6e9efb93a846b36e459286`。macOS arm64、bridge 0.1.1、SDK/runtime 0.1.5rc1。既存のmacOS互換処理は変更していない。

| 検証 | 今回の結果 |
|---|---|
| `.venv/bin/python -m pytest -m 'not wire' -q` | 58 passed, 10 deselected |
| `.venv/bin/mypy` | 8 source files、診断なし |
| `.venv/bin/ruff check src tests` | 成功 |
| `.venv/bin/ruff format --check src tests` | 16 files、成功 |
| `bash .codex/hooks/shell/outside.sh '.venv/bin/python tests/wire_sandbox.py'`（sandbox外から実行） | 10 passed, 58 deselected、18.15秒。skip/XFAILなし |
| Codex `hooks/list`による設定照会 | project hookは全てenabled/trusted、errors/warningsなし |
| 専用`code-reviewer`の正規準備・初回起動 | 準備は成功。初回起動は既存`AGENTS.md`変更に対するclean判定で拒否 |
| `uv --cache-dir /private/tmp/deepseek-bridge-research/uv-cache build --offline`（出力先は一時領域） | exit 2、cacheの`sdists-v6/.git`への書込み用openをOSが拒否 |
| 固定build backendのPEP 517 `build_sdist` / `build_wheel` | 0.1.1のsdist/wheel生成成功。既存cacheの固定5依存だけを読み、`outside.sh`内で実行 |
| 配布物検査 | wheelの全source・macOS互換module・privacy正本が一致。sdistの文書・互換module・privacy正本が一致。両方とも`.codex/`・`.agents/`を含まない |

復旧前のwireは`exit 71: sandbox-exec: sandbox_apply: Operation not permitted`でSDK起動前に停止した。当時の`protected-exec.py`には指定wire commandへのloopback制限と外側marker設定がなく、`wire_sandbox.py`が内側のsandboxを起動していた。

ユーザーが現在のwrapperへ限定差分を適用した後、ファイル内容が提案版と一致することを確認し、同じcommandで全10件が成功した。macOSかつrepository rootかつ完全一致commandの場合だけ、既存のGit metadata・agent設定の保護にloopback制限を加え、同じsandbox内のchildへ`BRIDGE_WIRE_SANDBOX_OUTER=1`を渡す。継承したmarkerは消去する。環境変数だけの手動設定やwrapperを外した実行では代用していない。uv cacheの書込み例外も追加していない。

レビュー準備も復旧前には`exit 2: preparation hook did not run; check project trust and hooks before continuing`だった。projectはtrusted、`features.hooks=true`、専用roleは利用可能だったが、準備・レビューlifecycle hookが`modified`、一部保護hookが`untrusted`だった。ユーザーによるhook信頼確認後のCodex 0.154.0照会では、全project hookがenabled/trustedで読込errors/warningsなし。hook stateの直接作成や準備経路の迂回は行っていない。独立コードレビューは、この検証表とは別に最終HEADを固定して実施する。

信頼確認後の`agent-input.py prepare code-reviewer`は成功したが、`2e07ab1c44826164dbd1f967f2ca718e41f42b2e`から`c85e94780622222f7ddb9b5e70dab78a1bf1b164`を対象とした専用roleの初回起動は、`independent-review.sh`の全追跡fileをcleanとする条件で拒否された。残る変更は作業開始前からあるユーザーの`AGENTS.md`だけであり、保持指定のためcommit・復元していない。この拒否を指摘なしのレビュー結果とは扱わない。

ユーザーがレビューhookの限定差分を適用したことも確認した。現在のrepositoryとこのtask IDに限り、未stageの変更が`AGENTS.md`だけで、mode変更なし・内容が確認済みSHA-256と一致する状態を起動時と結果受理時に許容する。stage済み変更、追加file、既存変更の消失・内容変更は拒否し、他task/repositoryでは元のclean条件を維持する。Git・agent設定のOS保護と正規の準備・専用role・契約注入経路は維持する。独立レビューの結果は、これらの文書変更も含む最終HEADを対象に別途報告する。

buildではGit保護やuv cacheの例外を追加していない。既存cacheにある`pyproject.toml`のbuild依存5件をexact versionで照合し、同じHatchling backendを直接使用した。`uv build`自体の成功とは区別する。生成物は検査用の一時領域に置き、公開していない。sdistの`AGENTS.md`は既存の未コミット変更を含む現在のworktree内容で、元fileの変更・commit・復元は行っていない。

復旧後のmacOS wireで今回確認した内容：

- 通常requestは`max_tokens/messages/model/reasoning_effort/stream/stream_options/thinking/tools`。`model=deepseek-flash`、`reasoning_effort=max`。
- 通常requestに`dsh_session_log`と`dsh_plugin_packages`がなく、両pluginを有効にしたnegative fixtureでは両fieldを観測。
- 通常入力・共通指示・同session継続履歴が存在し、fresh sessionへ履歴が混入しない。通常task間ではruntimeを再利用。
- canary API keyはAuthorizationに使われ、body・stdout/stderr・bridge error・通常local stateに露出しない。
- 指定OTel collectorへのrequestは0件。外部接続probeはOS拒否で、timeoutを成功扱いしていない。
- 実Harness shellが指定cwdで`example.py`とtestを作成し、`unittest`を実行。
- 実行中shellのabort/shutdown後に対象PIDが消え、worker threadが残らない。worktreeを復元しない。
- 401/429/503/malformed streamの分類、各fixtureのrequestが1回であること、abort後のruntime再生成とcrash時のfailedを確認。

SDK呼出しをmockに置き換えていない。下記の2026-09-17のmacOS結果とLinux旧0.1.0の結果は過去記録として残し、今回の実測とは区別する。Linuxは今回再実行していない。全送信先へのpacket監査、実DeepSeek serviceのデータ保持・請求・model品質は未確認。実APIへの追加疎通は行っていない。

## 2026-09-17 — 過去の検証記録

Python 3.12.8 / uv 0.5.9。SDK/runtimeは共に0.1.5rc1。
通常・wire検証はmodel endpointだけをlocal HTTP/SSE fixtureへ置き換え、Harness SDKとruntimeは実物を起動した。追加で、ユーザー設定の環境変数を使った実APIの短い疎通2回を実施した。

## TDD

- production code追加前に通常33件・wire6件がbridge未実装のassertionで失敗。
- 各test fileを単独commitし、cleanな状態でbaseline `5ab46744b50b7418224e73239bca4bb30bde9f05`を記録。
- JSON escapeで隠したcredential、completed時の空白questionについても失敗を確認してから修正。
- 独立レビューで指摘されたsignal終了とJSON形式credentialも失敗を再現してから修正。
- 追加検証により現在のtest数は68件。

## 結果

| 検証 | 結果 |
|---|---|
| 通常unit/integration、CLI module/console smoke | 58 passed |
| Linux x64 real SDK wire（Docker、network none、旧0.1.0の記録） | 10 passed |
| macOS arm64 real SDK wire（loopback限定Seatbelt） | 10 passed |
| mypy strict | 8 source files、診断なし |
| Ruff lint / format | 診断なし |
| `uv lock --check --offline` | lock一致、47 package（bridge自身含む） |
| `uv build` | 0.1.1のsdist/wheel生成成功 |
| wheel inspection | privacy profile・macOS互換moduleが正本と一致、controller fileを含まない |

macOSの旧XFAIL/skipは、SDK内部のprocess queryをlibprocへ渡す実行専用pluginで解消した。OS sandbox内のshell編集・test・取消・shutdown回収を実行し、全10件が成功した。SDK binaryやmodel-facing toolは変更していない。

ai-agent-rulesの統合probeでも、実stdio MCPと実SDKを配布保護wrapperで起動し、編集・test・Git書き込み拒否・同session継続・401・実shell取消を確認した。別fixtureでruntime回収後に失敗を注入し、`failed + abort_error`で親のwriter制限を解除しないことを確認した。storageだけを一時領域へ向け、実APIは呼んでいない。

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
- SIGTERM/SIGINT、開いたstdin、受信途中のJSON、実行中taskを組み合わせた8条件で終了し、taskはinterruptedとなる。

全外部宛ての**送信試行**をpacket captureで数えたわけではない。OSで外部egressを拒否し、設定したlocal collectorへの通信を観測した範囲の結果。
実DeepSeek serviceのデータ保持・請求額・一般の開発taskにおける指示遵守は、この検証では確認していない。

## 実API疎通

ユーザーが`.zshrc`へ設定したAPI keyを環境変数として取得し、値は出力・記録しなかった。

- 公式Chat Completionsへ固定の短文を1回送信：HTTP 200、応答は期待した`OK`。入力9・出力1、合計10 tokens。
- 配布の起動script→Git保護wrapper→実stdio MCP→実DSH sdk-minimal→公式APIへ1回送信：`deepseek-flash` / `max`で`completed`、期待したJSONと一致。
- 後者は空の一時repositoryと一時stateだけを使用し、file読込・tool実行を要求しなかった。ユーザーのコードは送信していない。

開発作業の品質や長時間taskの成功を、この疎通から推定しない。Linuxの今回の再実行は行っていない。

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
当時の記録にある`.codex/hooks/shell/protected-exec.py`の変更は、uv一時cacheのmarker 1個への例外と、wire検証commandへのloopback制限。2026-09-18の復旧前にはどちらも存在せず、今回復旧したのはwire検証commandの制限だけ。
`.codex/`は既存方針どおり管理外で、アプリの配布物には含めない。
