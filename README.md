# deepseek-bridge

CodexなどのMCP clientから、起動元Git repositoryに固定したDeepSeek Harnessを状態付きworkerとして使うstdio MCP server。

```text
MCP client → stdio MCP → TaskManager → 専用worker thread
                                       ↓
                         公式DeepSeek Harness SDK / runtime
                                       ↓
                   sdk-minimal / deepseek-official / deepseek-flash / max
                                       ↓
                              起動元repositoryのworktree
```

bridgeの責務はworkspace固定、session継続、単一writer、待機・取消・shutdown、privacy設定と結果検証。
Gitのstage/commit、branch/worktree作成、難度評価、model routing、review、deploy、OpenCodeや別modelへのfallbackは実装しない。
Gitを使うのは起動時の読み取り専用root解決だけ。

## 導入

Python 3.12以上、Git、uvが必要。検証環境はPython 3.12.8 / uv 0.5.9。
DeepSeek Harnessはdeveloper preview。SDKとruntimeを共に`0.1.5rc1`へ厳密固定した。
直接依存は`pyproject.toml`、間接依存・配布hashは`uv.lock`が正本。固定version一覧は[DEPENDENCIES.md](DEPENDENCIES.md)。

このrepositoryで次を実行する。

```bash
uv sync --frozen
.venv/bin/python -m pytest -m 'not wire'
.venv/bin/mypy
.venv/bin/ruff check src tests
```

DeepSeek APIを利用するには、DeepSeek側のアカウント・API利用契約・有効なAPI keyと利用枠が必要。
Codex/ChatGPTの契約とは別のAPI利用になる。実APIへの接続は料金が発生しうる。wire testはlocal fixtureだけを使い、実API keyを必要としない。

API keyは環境変数`DEEPSEEK_API_KEY`からだけ読む。shell履歴、tool引数、設定fileへkeyを貼らず、secret managerから環境へ渡す。
ai-agent-rulesの起動scriptは環境変数を優先し、未設定ならzshの`.zshrc`を読み込んで保護付きbridgeへ渡す。`.zshrc`へ`export DEEPSEEK_API_KEY='...'`を書く方式は平文保存になるため、Git管理へ含めない。bridge本体はキーの保存方式に依存しない。
通常は`DEEPSEEK_BASE_URL`を設定しない。DeepSeek互換のcustom endpointを使うときだけ設定する。
URL内の認証情報・query・fragmentは受け付けない。環境や認証情報を含むdiagnosticをそのまま転送しない。

起動する**対象repository**へ移動して、インストールした実行fileの絶対pathで起動する。

```bash
cd /absolute/path/to/target-repository
/absolute/path/to/deepseek-bridge/.venv/bin/deepseek-bridge
```

または同じ環境の`python -m deepseek_bridge`。stdioのstdoutはMCP専用なので、人がプロンプトを直接入力するCLIではない。
runtime起動時にpackage取得・self-update・latest解決は行わない。SDKは同梱済みruntimeを使い、DSH homeへ同梱profileを展開する。

Git外、`/`、homeそのもの、symlinkのrepository rootを拒否する。起動後にrootを変更するtoolはない。
同じrepositoryをこのbridge自身のrepositoryとして起動する場合も特別扱いしない。

## Codex設定例

対象repositoryにホスト側の保護wrapperが用意されている前提。
次の例のpathを環境に合わせる。他repositoryの設定をbridgeが自動編集することはない。

```toml
[mcp_servers.deepseek-worker]
command = "bash"
args = [
  ".codex/hooks/shell/deepseek-launch.sh",
  "/absolute/path/to/deepseek-bridge/.venv/bin/deepseek-bridge"
]
startup_timeout_sec = 20
default_tools_approval_mode = "approve"
```

MCP clientが対象repositoryをcwdとして起動し、必要な`DEEPSEEK_API_KEY`を環境に渡すこと。
この例にkeyの値を書く必要はない。

## 公開toolと状態

公開toolは次の4個だけ。追加引数は拒否する。workspace/model/provider/effort/profileは公開しない。

| Tool | 入力 | 動作 |
|---|---|---|
| `start_task` | `brief`、省略可能な`title` | 即座にtask ID、session ID、`running`を返す |
| `wait_task` | `task_id`、`timeout_ms`（既定60000） | 状態変化または新SDK activityで起床。timeout後もtaskはrunning |
| `continue_task` | `task_id`、`message` | 同じtask/sessionを`running`へ戻す |
| `abort_task` | `task_id` | 実行中worker/runtimeを回収し`aborted`にする |

`brief`/`message`は1〜32,000文字、`title`は1〜200文字。空白だけの入力、credentialらしい入力、環境に設定されたkeyの混入を拒否する。
credential検出は防御の補助であり、任意形式のsecretをすべて見つける保証はない。secretをタスクへ渡さないこと。
`timeout_ms`は整数0〜60,000。timeoutやwait requestの取消は、実行taskの失敗・取消を意味しない。
`wait_task`はstatus変化または呼出時より新しいactivity sequenceで起床し、poll自体はactivity時刻を更新しない。

```text
start → running → completed / needs_decision / failed / aborted / interrupted
completed / needs_decision → running  （continue_task）
failed / aborted / interrupted        （終端）
```

`wait_task`は`task_id/session_id/status/started_at/last_activity_at/elapsed_ms/phase/observability/final_response/finish_reason/error`を返す。
`started_at`/`last_activity_at`はUTC aware date-time、`elapsed_ms`は単調時計の非負整数。terminal後は`elapsed_ms`をfrozenする。
`phase`は固定Literal語彙（`starting`/`process_start`/`run_start`/`turn_start`/`turn_end`/`step_start`/`step_end`/`tool_call`/`tool_result`/`model_attempt`/`assistant_message`/`user_message`/`system_message`とterminal status）。`observability`は`available`/`unavailable`。
SDK `on_notification`の実eventだけで`last_activity_at`とphaseを更新し、`starting`/`process_start`/`run_start`/`step_start`/`tool_call`/`model_attempt`は次eventまで内部進捗を観測できないため`unavailable`、他の離散eventは`available`とする。
terminal遷移は実処理終了・cleanup完了の事実として`last_activity_at`/activity sequence/phaseをterminal statusへ進め、`observability`を`available`にする。wait/pollはactivity時刻を更新しない。
event本文・tool引数・model出力・例外本文・secretはsnapshotへ出さず、executorからqueueされた最終activityもterminal公開前に処理する。
`final_response`は次節の検証済みobjectまたはnull。`error`は短い`class/message`またはnull。
stdioの1 request行は最大1 MiB。入力pipeは非同期に読み、JSON受信途中でもsignalによる終了を待たせない。

同一process内では1 taskだけがwriter。start/continueの競合は片方を拒否する。
複数bridge process間の分散lockはない。同じworkspaceへのbridge多重起動をhost側で防ぐこと。
通常task間ではruntimeを再利用し、新taskには別session IDを発行する。
failed taskの暗黙resumeはなく、runtime crash・abort後のfresh taskではruntimeを作り直す。

SDK protocolにcancel RPCがないため、abortは所有するruntimeの`close()`でshutdown、必要ならterminate/kill/waitを行う。
初期化と終了を直列化し、初期化中の取消でmodel turnを始めない。初期化中のabortはSDKの30秒の初期化期限まで待つ場合がある。
終了失敗は`abort_error`とし、新しいwriterを受け付けない。worktreeをGit reset/restore等で戻す処理はない。
task全体のhard timeoutは既定20分、最終activityからのinactivity timeoutは既定120秒とし、通常の47秒stepをinactivityで打ち切らない。
watchdogは単純sleepではなくconditionでactivity sequence/status変化を待ち、deadline到達後もcondition lock下で最新`last_activity_monotonic`とhard deadlineを再評価してからtimeout回収へ進む。
timeout時は所有runtimeのclose、run future回収、executor shutdownを行い、成功時は`failed`+`task_timeout_error`で予約を解放しfresh taskを開始できる。
cleanup失敗時は`failed`+`abort_error`として予約を保持し、fresh taskを拒否する。timeout・abort・shutdownの回収は`_cleanup` lockで直列化する。
`continue_task`は同じsessionを維持し、runごとのstarted/last_activity/elapsed/deadline/stop状態をresetする。
stdio EOF・SIGTERM・SIGINTでshutdownし、実行中taskを`interrupted`にしてworker/runtimeを回収する。
task状態はメモリ内のみ。再起動後は以前のIDを受け付けず、自動resumeしない。clientがworktreeを確認してfresh taskを始める。

## Modelへの指示と最終応答

各session開始時に共通指示を渡す。rootの`AGENTS.md`と必要な参照文書の確認、ユーザー変更の保持、調査・編集・検証、Git変更禁止、設定・secret・lockfileの無断変更禁止、外部送信・公開・deploy・課金操作禁止を含む。
独立したsearch/readはsame stepへまとめ、already read fileをre-readしない指示も含む。
判断が必要なら`needs_decision`を要求する。repositoryコードや環境変数一覧を共通指示へ埋め込まない。

最終応答はMarkdown fenceなしのJSON object。以下の6 fieldをすべて必須とし、未知field、重複key、型違いを拒否する。

| Field | 制約 |
|---|---|
| `status` | `completed` / `needs_decision` / `failed` |
| `summary` | 空白以外を含む1〜2,000文字 |
| `tests` | 最大50個、各1〜500文字のstring |
| `question` | `needs_decision`時だけ空白以外を含む1〜1,000文字、他はnull |
| `affected_paths` | 最大50個、各1〜500文字のrepository相対path、親への遡行不可 |
| `unresolved` | 最大50個、各1〜500文字のstring |

応答全体は最大16,000文字。超過を切り詰めず`task_contract_error`にする。
credentialはJSON decode後にも検査する。SDKが正常終了しても契約違反を成功にしない。
modelが主張するテスト結果の事実確認・reviewはMCP clientの責務。

## Privacyとlocal state

[profiles/privacy.cordis.yml](profiles/privacy.cordis.yml)を正本とし、wheelにも同じfileを収録する。
`session-log-deepseek`と`plugin-package-inventory-deepseek`を`config.enabled: false`に固定する。
`llm-retry`も無効化し、障害時の自動再試行をしない。patchが欠ける・不正ならfail closed。
childには親の設定より優先して次を渡す。

```text
DSH_TELEMETRY_MODE=DISABLED
DSH_TELEMETRY_DISABLED=1
OTEL_SDK_DISABLED=true
```

通常のsystem prompt、user message、tool call、tool resultはmodelの入力としてDeepSeekへ送信される。
追加の`dsh_session_log`と`dsh_plugin_packages`を送らない。bridge独自のtelemetryやanalyticsはない。
bridgeはSDK/MCPの生のdiagnostic loggingを無効にし、stderrには固定のsanitized errorだけを出す。
key、header、prompt/response全文、file内容、tool result全文、環境変数一覧をlogしない。

DSHの**local JSONL session logは保存される**。upload無効化はlocal保存の無効化ではない。
保存先は`platformdirs.user_state_path("deepseek-bridge", appauthor=False)`配下。
macOSでは通常`~/Library/Application Support/deepseek-bridge`、Linuxでは`$XDG_STATE_HOME/deepseek-bridge`または`~/.local/state/deepseek-bridge`。

```text
<user-state>/deepseek-bridge/
  dsh-home/<SHA256(repository絶対path)>/sessions/<SDK workspace bucket>/<session-id>/session.v3.jsonl
  runtime/<SHA256(repository絶対path)>/
```

bridgeはrepository pathをそのまま分離directory名に使わない。SDK内部のworkspace bucketやsession内容にはcwdが含まれる。
DSH homeとruntime directoryを0700にし、CLIはumask 077で起動するため新規通常fileは0600になる。
bridge独自のprompt/response複製DBはない。既存SDK fileのpermissionを再帰的に修復する処理はない。
user-stateをtarget repository内へ設定した場合やbridge管理directory自体がsymlinkなら拒否する。

削除前に対象bridgeを停止する。対象repositoryで次を実行すると**削除対象の2 directoryだけを表示する**。

```bash
/absolute/path/to/deepseek-bridge/.venv/bin/python -c 'import hashlib, os; from pathlib import Path; from platformdirs import user_state_path; from deepseek_bridge.runtime import bind_workspace; root=bind_workspace(); digest=hashlib.sha256(os.fsencode(root)).hexdigest(); base=Path(user_state_path("deepseek-bridge", appauthor=False)); print(base/"dsh-home"/digest); print(base/"runtime"/digest)'
```

表示されたdirectoryを確認して、その2つをfile manager等で削除する。local session履歴も削除される。
これはAPI providerが保持するデータの削除ではない。

## OS sandboxの境界

**cwd指定はfilesystem jailではない。** bridge単体でworkspace外の読み書きを防げるとは主張しない。
共通指示のGit禁止もOSのアクセス制御ではない。host側で`.git`、agent設定、secret、workspace外のpathを保護し、必要な通信先だけを許可する。
SDK `sdk-minimal`のlocal shellはhostの権限内で動く。API keyだけでなく親環境に他のcredentialを大量に渡さないこと。

SDK 0.1.5rc1のmacOS process inspectorはsetuid付き`/bin/ps`を使うため、Seatbelt内でそのままでは起動できない。macOSでは実行専用のCordis pluginを追加し、SDK内部の2種類の同期process queryだけを、通常権限の`libproc` APIへ置き換える。

`macos_process.mjs`が固定queryを照合し、隔離モードのPython helperがPID・親PID・開始時刻・foreground groupを読み取る。開始時刻を含むidentity比較を維持し、未知query・ABI不一致・自process treeの不可視は失敗する。通常のshellから`/bin/ps`を実行する権限は追加しない。

SDK binary、system prompt、model-facing tool、Git保護・network policyは変更しない。Linuxは互換pluginを追加しない。2026-09-17の過去記録では、macOSでも外部通信禁止のsandbox内で実shell編集・test・abort・shutdown回収のwire全10件が成功し、XFAIL/skipを解消した。
2026-09-18にはhost wrapperのwire用通信制限を復旧し、現在のmacOS arm64保護環境でも実SDK wire全10件が成功した。実shell編集・test・実行中shellのabort/shutdown回収を含む。復旧前のsandbox二重起動の失敗と、復旧後の実測を[VALIDATION.md](VALIDATION.md)に分けて記録している。

## 検証

```bash
.venv/bin/python -m pytest -m 'not wire'
.venv/bin/mypy
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
```

実SDK wire検証はlocal DeepSeek互換SSE serverに接続する。SDK自体をmockに置き換えない。
macOSの通常ターミナル、またはLinuxでbubblewrapが使える環境では次を実行する。

```bash
.venv/bin/python tests/wire_sandbox.py
```

このrunnerはloopback以外の通信をOSで禁止する。既にsandbox内の場合、macOSはsandboxの二重起動を拒否しうる。
このrepositoryの開発環境では、外側の`.codex/hooks/shell/protected-exec.py`がGit metadata・agent設定の保護とloopback制限を一つのsandboxで適用する。macOSでrepository rootから実行する以下の完全一致commandだけが対象。2026-09-18にユーザーが限定差分を適用し、この経路で全10件の成功を確認した。uv cacheの書込み例外は追加していない。

```bash
bash .codex/hooks/shell/outside.sh '.venv/bin/python tests/wire_sandbox.py'
```

`BRIDGE_WIRE_SANDBOX_OUTER=1`はそのhost wrapperが設定する印であり、利用者が未検証のまま設定してはいけない。
外部接続probeがOS拒否を受けることも検査する。単なる接続timeoutは合格にしない。
直接`pytest -m wire`を使った場合、egress検証はskipとなり、その実行を全通信境界の確認済みとは扱わない。

Linux x64の再現にはDockerと次のscriptを使える。準備時だけ固定依存を取得し、実テストは`--network none`、capability全削除、ソースread-onlyで実行する。

```bash
sh tests/linux_wire.sh
```

現在の開発環境では外部実行を`outside.sh`に通す。

```bash
bash .codex/hooks/shell/outside.sh 'sh tests/linux_wire.sh'
```

以下は2026-09-18のmacOS再検証でも観測した結果。Linuxの全10件成功は旧0.1.0時点の過去記録で、今回は再実行していない。
観測した通常requestのfieldは`max_tokens/messages/model/reasoning_effort/stream/stream_options/thinking/tools`。
modelは`deepseek-flash`、reasoning effortは`max`。通常入力とsession継続履歴が存在する。
privacyを無効化し両pluginを有効にするnegative fixtureでは、これに`dsh_session_log/dsh_plugin_packages`が追加される。
wire testはこのfield集合とSDK/runtime versionを固定して、更新時の差を検出する。
canary API keyはAuthorization headerにだけ使われ、body・stdout・stderr・bridge error・通常session保存fileへの非露出を検査する。
指定OTel collectorへのrequestは0件。外部egressはOSで拒否した。未知の送信先への**送信試行そのもの**の全件packet監査や、実DeepSeek serviceの保持方針までは検証しない。

配布側との統合はai-agent-rulesの`tests/probe_deepseek_bridge.py --bridge-root <このrepository>`をこの環境のPythonで実行する。実MCP/DSH、ローカル模擬API、配布されたGit保護・非同期hookを通し、編集・test・継続・認証失敗・実shell取消と、回収失敗時に親のwriter制限が残ることを検証する。

## Errorとtroubleshooting

| class | 確認すること |
|---|---|
| `configuration_error` | Git root、API key環境、入力上限、task ID/状態、固定SDK version |
| `privacy_configuration_error` | 必須patchの存在と正本の内容 |
| `authentication_error` | DeepSeek API key・利用権限（401） |
| `transport_error` | endpoint到達性、429・5xx。自動retryはしない |
| `harness_start_error` | runtime wheel、platform、初期化。SDKは30秒で期限切れ |
| `harness_protocol_error` | malformed stream、runtime crash、SDK protocol不一致 |
| `model_error` | modelの失敗状態、正常完了以外の終了理由 |
| `task_contract_error` | JSON最終応答の形式・長さ・credential混入 |
| `abort_error` | runtime回収失敗。新taskを開始せずhostで残processを確認 |
| `internal_error` | bridge内部障害。secretを除いた再現条件を確認 |

SDKの生exceptionやstderrをそのままissueへ貼らない。bridgeはそれらをMCPへ転送しない。
実APIへの請求を伴う疎通やmodelの実際の品質評価は、このlocal fixture検証には含めていない。

## SDK更新

1. 利用者が一時branchを用意し、新SDKと同versionのruntimeをexact指定する。
2. 隔離環境でlockを更新し、unit/integration testを実行する。
3. privacy wire testの通常・negative request差を確認する。
4. 同じsessionの継続、fresh sessionの分離を確認する。
5. abort、実行中shellの回収、runtime crash、初期化失敗、shutdownを確認する。
6. 小さなfixture workspaceで実shellの編集・テストを実行する。
7. field・telemetry・profile・session semanticsの差を確認する。期待値だけを緩めない。
8. 成功したversion、正本patch、version assertion、`uv.lock`を揃えてcommitする。

互換性を推測したfallback、effortの自動低下、privacy patchなしでの再実行はない。
bridge更新時はwheelへprivacy profileが含まれることも確認する。

## 公式資料

- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
- [公式Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)
- [Session log upload](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-log-deepseek/README.md)
- [Plugin inventory](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/plugin-package-inventory-deepseek/README.md)
- [OTel backend](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-telemetry-otel/README.md)
- [DeepSeek V4.1 Flash](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash)

masterの資料と固定wheelに差がありうるため、runtime設定の実物とwire behaviorを優先して検証する。
