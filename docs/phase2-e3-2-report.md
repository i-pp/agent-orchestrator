# Phase 2-E3.2 — 動作優先PoC

パスは[表記規約](path-conventions.md)の疑似パスです。シェル例は同ページの変数を設定してから使用します。原本ログは変更していません。

実施日: 2026-10-29（Asia/Tokyo）。開始commit: `dac0d51758255846da02807ad2746bf3ee1718fe`。
本書はAIによる実施要約。原証拠はGit対象外の`.runtime/phase2-e3-2-evidence/`。

## 結果

**systemd配下の専用runtimeで、実Codex 1 turnによるコード生成・テスト・独立検証・正常終了と、daemon再起動後のSQLite保持に成功した。**
利用者端末での実Tunnel起動後、当方もreadiness・control-plane poll成功を確認した。
OpenAI hosted Tunnel経由の読み取りMCP呼出しは引き続き未検証。
実tunnel-clientと模擬control planeを使った読み取りMCP転送の成功を、実Tunnel/ChatGPT接続成功と混同しない。

| 要求 | 実測結果 |
| --- | --- |
| systemd user配下の実Codex | 成功。専用transient unit、subscription認証 |
| テストworkspaceへのコード生成 | calculator.pyを生成、所定のSHA-256に一致 |
| Codexによるテスト実行 | unittest 3件成功。turnのcommandExecution記録で確認 |
| ランナーによる独立検証 | code/test双方のhashと許可済みtest command成功、COMPLETED/verification PASSED |
| daemon再起動とSQLite保持 | 成功。PID変更、同じtaskのCOMPLETED保持、execution数1、integrity_check=ok |
| 読み取りMCP | 実client＋loopback模擬control planeで3ツール発見、status/get_task成功 |
| 実OpenAI Tunnel | 利用者端末で起動。healthz/readyz HTTP 200、poll成功を当方も確認。MCP呼出しは未検証 |
| 実サービス導入、自動起動 | 未実施。unit配置/enable/linger変更なし |
| 実Tunnelの書込み公開 | 未実施。adapterに--allow-writesを渡していない |

## 採用方式

元のunshare＋Python mount/pivot bootstrapとCodex内部sandboxはそのまま残す。
新しい`--development-poc`を明示した場合だけ、**既存bwrapにmount setupを一段で行わせ、CodexにはexternalSandboxを指定する**。
namespace自体をすべて省く方式ではない。AppArmor専用profile/native helperは実装・適用せず、既存のbwrap profileを変更しない。
単なるPython wrapperへのsetup権限継承ではなく、mountを既存bwrapで完結する別経路である。

```text
専用systemd transient user unit
  → 既存daemon scope → worker scope（32 TID）
    → 1 CPU affinity → bwrap（既存profile）
      → private filesystem → App Server stdio → 実Codex turn
          Codex turn: externalSandbox / network enabled
    → 別のno-auth・network namespace付きread-only bwrap → 許可済みtest
```

モデル生成なしのnative command/exec比較では、bwrap内でworkspaceWriteを指定すると入れ子bwrapのuser namespace作成が拒否された。
同じ専用workspaceでexternalSandboxを指定すると書込み・test実行が成功した。
externalSandboxがCodex自身のsandbox強制を省くことは[公式App Server資料](https://learn.chatgpt.com/docs/app-server#command-execution)でも確認した。
この指定を外側隔離なしで使うモードは実装していない。danger-full-accessも使っていない。

### 適用範囲と起動拒否

- runtimeはPROJECT/.runtime直下の`development-poc-NAME`に限定。symlinkを拒否し、rootはowner UID/0700、markerは0600を要求する。
- `development-poc.json`へmode/runtime/schemaを保存。明示flagを省いた再起動は拒否する。既存のデータ入りruntimeを変換しない。
- 実行できるのは名前付き専用fixtureのimplementer taskだけ。legacy workspace、reviewer、resumeはPoCでは拒否する。
- 既存のtask/execution/thread/turn分離、lease、FIFO、受入条件、二重実行防止、UNKNOWN保護、資源監督を再利用。DB schema変更なし。
- App Server/metadata probe/検証は新経路へ明示分岐する。bwrap/taskset失敗時に通常実行や非隔離実行へfallbackしない。
- unit資源値、scheduler-config.json、resource-limits.jsonを緩めていない。PoC内のCPU affinityだけを1 CPUへ制限した。

### filesystem・認証の扱いと制限

bwrapは/usr、/etc、必要なinstalled binaryとこのtaskのworkrootだけをread-onlyで見せる。
workspace-writeは明示taskのworkspaceだけをbind writableにする。App Server用の専用codex-homeとprivate /tmpは実行状態用に書込み可能。
coordinatorのSQLite/制御socket、別workspace、実HOMEの.ssh/.codex、製品repositoryはそのviewへ持ち込まない。
初期化済みの専用codex-homeは既存prepare/CONFIGを使う。
認証は既存ファイルをbwrap内の専用mountpointへread-only bindする。Pythonが内容を読み出してコピーする処理はない。
account/readでchatgpt型を要求し、API key環境変数をchildへ渡さない。元の認証が更新を必要とする場合も、元ファイルへの書込みを許すfallbackはない。
独立検証とversion probeはno-auth。認証のread-only bindを付けず、network namespaceも分離する。

これは本番品質の隔離設計の完成を意味しない。
App Serverとmodel toolは同じ外側viewを共有し、専用codex-homeはApp Serverの状態用に書込み可能である。
そのviewにはread-only認証mountもあり、悪意あるtaskを安全に受け付けられるとは主張しない。
authenticated server/turn側はhost networkを共有し、外向き通信先のallowlistは設けていない。
専用の信頼するfixture promptに限定し、実Tunnelからのsubmit/cancel公開や無人の外部入力ループは行わない。

## 実Codexの証拠

成功runtime: `.runtime/development-poc-e32-c00a7a45/`。

- binary: `<CODEX_BINARY>`
- 実version: `codex-cli 0.164.0-alpha.11.1`
- task: `fc9c4ea6-a6f3-493f-998d-209a65e74bcb`
- thread: `01a18331-db65-7793-a0a2-419c663322ba`
- turn: `01a18331-db7d-7b61-b1b9-e7f37f7902e2`
- 生成物: `calculator.py`（整数加算関数）。固定test_calculator.pyの正数/負数/ゼロ3件を実行。
- 受入条件: 生成codeのhash、固定testのhash、registryのpoc-calculator-tests。
- restart後execution数1。worker/App Server資源released=true、worker pids.peak=7、pids.max=32、超過なし。

証拠: `live-development-poc-e32-c00a7a45.json`、対応unit journal、runtime logs/tasks.sqlite、`resource-summary.json`。
原RPCログから実turn投入は成功runの1回だけで、失敗runにはturn/startがないことを確認した。
利用枠の観測値はtotalTokens=55,244、inputTokens=54,964、cachedInputTokens=40,576、outputTokens=280。
これはCodex通知のtoken集計であり、サブスクリプション残量や金額へ換算していない。
Platformモデル推論APIを独自に呼び出すコードやAPI従量課金への切替は追加していない。

### 初回失敗を含む経緯

初回runtime `.runtime/development-poc-e32-8804ac23`はturn投入前にworker pids.maxへ到達しCANCELLEDとなった。
worker receiptはlimit32、peak32、pids.events max6を記録。thread作成まで進んだが、モデルturnは投入していない。
この失敗は保持し、同じtaskを再実行したり成功へ書き換えたりしていない。

private viewにはhost cgroup topologyを見せないため、native runtimeのCPU数判定とthread pool拡張が問題になり得る。
PoCのbwrap起動をtasksetで1 CPUへ固定した。worker制限32のままversion-onlyを再測定し、peak6/max0で成功した。
その後、新規専用runtimeで1回実turnを実行し成功した。Tokio/Rayonの環境変数で制限する方式や、pids上限の引上げは採用していない。
起動時の「CodexのLinux sandboxにはuser namespaceが必要」というconfigWarningは残る。
明示したexternalSandbox経路で成功したことをもって通常モードの警告原因を解消したとは扱わない。

## テスト

| 対象 | 結果 |
| --- | --- |
| 既存148 | 全成功 |
| E3新規15 | 13成功・既知の2失敗を維持 |
| E3.2新規11 | 全成功 |
| 全174 | 172成功・2失敗、188.400秒、skipなし |
| 最終PoC限定11件 | 全成功。CLIのsymlink拒否も追加確認（new-tests-reviewed.txt） |
| 実Codex | 上記1 turn成功。新規unit/namespace/DB/成果物は専用runtimeのみ |
| 読み取りMCP転送 | actual client＋local synthetic control plane、2025-11-25、3ツール、status/get成功 |

旧試験の削除・skip・assert変更なし。FIFO EOFは今回再発しておらず、E2の原因解消とは報告しない。
既知の失敗は通常モードのsystemd SIGTERM/SIGKILL試験2件で、PoC成功に置き換えていない。
新規試験はmode明示/既存state変換拒否/不正marker/パス逸脱/実OS書込み境界/read-only拒否/失敗時fallbackなし/
Codex成功とartifact検証失敗の分離/DB再読込等を検証する。

```bash
python3 -B -m unittest discover -s tests -v
# 現ホストでは通常モードの既知2件によりexit 1
python3 -B -m unittest discover -s tests -p test_phase2_e3_2.py -v
# subscriptionを使う1 turn。既存taskの再実行ではなく新runtimeを作る
python3 -B tests/live_phase2_e3_2.py --execute
```

## MCPと実Tunnelの区別

`tests/poc_readonly_check.py`は、保存済みPoC runtimeにQUEUED/RUNNING/VERIFYINGがないことを確認し、
一時的な専用daemonとnative clientを起動して読み取りだけを試す。検証後は自分のprocessだけを停止する。
既存daemonが同runtimeを所有する場合は置換せず拒否する。
control planeは127.0.0.1の専用fixture、キーは公開synthetic値で、実OpenAI Tunnelへは接続しない。
保存済みの本物のCOMPLETED taskをMCPで取得し、local socketのtask_countsと照合した。
証拠`readonly-tunnel-e717ce29.json`はhosted_tunnel_verified=falseを明記する。

```bash
python3 -B tests/poc_readonly_check.py \
  --runtime "$POC_RUNTIME" --task-id "$POC_TASK_ID"
```

実Tunnel `aceis-mcp-dev` / `tunnel_6ae1d745be7c8191b6d502b613317b4c`は作成・変更・削除していない。
当初この実行環境のCONTROL_PLANE_API_KEYは未設定だったため、利用者端末での実行へ引き継いだ。
その後、利用者が実Tunnelのhealth結果を提示し、2026-10-29 01:20 JSTに当方もキーなしのnative healthで再確認した。
healthz/readyzはHTTP 200、control_plane_poll.ok=true、result=ok。
process argvの許可項目だけを確認し、接続先api.openai.com、既存Tunnel ID、読み取りadapterとPoC runtimeの指定を照合した。
argv全体や環境変数は記録せず、認証情報を取得していない。
実接続には利用者端末の既存認証を用いる。新規キー発行・課金設定・権限変更はしない。

利用者端末向け手順（起動・health確認まで実施済み）:

1. 専用runtimeの完了taskを確認し、既存daemonが所有していない場合だけPoC daemonを別端末の前景で起動する。
   実運用unitのinstallは不要。既存taskがQUEUEDなら実行が始まるため、未実行taskがないことを先に確認する。
   保存済み成功runtimeを再利用する前景コマンド（別端末を占有する）:

```bash
cd "${PROJECT_ROOT}"
POC_RUNTIME="${PROJECT_ROOT}/.runtime/development-poc-e32-c00a7a45"
: "${CODEX_BINARY:?Set the installed Codex executable}"
python3 -B src/runner.py --runtime "$POC_RUNTIME" --codex "$CODEX_BINARY" --development-poc daemon
```

2. 別端末でも上記POC_RUNTIMEを設定し、既存CONTROL_PLANE_API_KEYをexportする。値をchat/argv/Gitへ書かない。
3. 既存service用profileを使い、MCP targetをこのPoC runtimeへ明示する。MCP childからキー環境変数を除く。

```bash
# POC_RUNTIMEは確認済み専用runtimeの絶対パス。既存キー設定済み利用者端末だけで実行
export CONTROL_PLANE_API_KEY
# 利用者の指示により時間制限なし。終了時はCtrl+C
  "$TUNNEL_CLIENT" run \
  --profile-file "${PROJECT_ROOT}/systemd/aceis-mcp-tunnel.yaml" \
  --control-plane.api-key=env:CONTROL_PLANE_API_KEY \
  --mcp.command "channel=main,command=/usr/bin/env -u CONTROL_PLANE_API_KEY -u OPENAI_API_KEY -u OPENAI_ADMIN_KEY /usr/bin/python3 -B \"${PROJECT_ROOT}/src/mcp_adapter.py\" --runtime \"${POC_RUNTIME}\"" \
  --health.listen-addr=127.0.0.1:0 --health.url-file="$POC_RUNTIME/poc-tunnel-health-url" \
  --log.http-raw-unsafe=false --log.level=warn
```

4. 起動中に別端末から次を実行する。readinessと実MCP呼出しは別に記録する。

```bash
"$TUNNEL_CLIENT" health \
  --url-file "$POC_RUNTIME/poc-tunnel-health-url" --require-control-plane-poll --json
```

   読み取りMCPを呼び出せる既存の承認済み接続からstatus/getだけを呼ぶ。接続が未登録なら、別の接続検証工程へ引き継ぐ。
5. 利用者はTunnelをCtrl+C等で停止するまで維持する方針へ変更した。当方から停止命令を送っていない。
   終了時は今回の前景processだけを止め、キーのshell環境変数をunsetする。
   --allow-writes、submit/cancel、サービスenable/linger、ChatGPTへの書込み公開は行わない。

## 変更ファイル・既存環境

追加: src/development_poc.py、tests/test_phase2_e3_2.py、tests/live_phase2_e3_2.py、tests/poc_readonly_check.py、本報告。
更新: src/runner.py、src/daemon.py、src/verification.py、verification-commands.json、README。
許可test registryにはpoc-calculator-testsだけを追加し、既存entryは維持した。
AppArmor草案/native helper設計、既存unit2件、MCP adapter、旧テスト、scheduler/resource設定は変更していない。
新規systemd unitはUUID名のtransient fixtureだけで、実運用名のunitは未配置。
config/authのmetadataとconfig hash、凍結対象hashを開始時と照合し、結果をpreservation-check.jsonへ保存した。
config.tomlはhash/metadataの変化を検出し、利用者から「アプリで設定を変更した」と回答を得た。
元へ戻していない。書込みPIDの監査は未実施であり、内容差分をhashから復元できるとは扱わない。
認証metadata、凍結対象、既存テストは一致した。
全ての新規検証用processを停止し、fixture unit残存なしを確認。runtime/DB/logは削除しない。
登録予定10ファイルを明示し、index全79ファイルのblob/path監査はpassed、findingsなし。
前工程の未追跡報告2件は変更・commit混入しない。remote登録/push/外部公開はしない。

追加証拠: `hosted-tunnel-health-confirmation.json`。当方の独立health結果とlocal daemonの参照値を記録した。
local daemonはCOMPLETED=1、maximum_sessions=2、paused=false。
この照合はlocal socketへの読取りであり、実Tunnel経由のMCP呼出しとは別である。
次は、起動中のTunnelを使った読み取りMCPの実呼出しとlocal参照値の照合が必要。
実サービスの導入や実Tunnelへの書込み公開を行う場合は、対象と変更内容を提示して別途承認を求める。

## 追加調査: ChatGPTプラグイン作成時のdiscovery互換性

2026-10-29 JST、利用者のボタン操作前後で起動中clientのloopback metricsを読み取り比較した。
基準01:39:50、操作直前の01:42:25には増分なし、操作後01:46:16の増分は次のとおり。
原本全体を記録せず、許可したmetric名とlabelだけを抽出した証跡は
`.runtime/phase2-e3-2-evidence/plugin-click-baseline.json`、`plugin-click-after.json`。

| 観測対象 | 増分 |
| --- | ---: |
| main / initialize（poll_to_response） | 2 |
| main / server/discover（poll_to_response） | 1 |
| harpoon / unsupported channel | 2 |
| commands_polled / enqueued | 各5 |

要求がTunnelへ届いたことを確認した。latency_type違いのcountは同じ要求の別計測なので合算しない。
tunnel_service_status=200は転送層の値であり、JSON-RPC成功を証明しない。
以前共有されたHarpoonログの発生時刻は当該ボタン操作と一致すると確認できず、原因の証拠として扱わない。
今回の観測区間ではHarpoon拒否も増えたが、画面失敗への寄与と各要求の厳密な時刻・対応関係は未確定。
WARN設定では通常の要求が端末に表示されない場合があり、無出力から未受信とは判断しない。

確認済みの互換性不備: 旧adapterへ2026-07-28 metadata付きserver/discoverを送ると-32602になる。
公式仕様のserver/discoverと要求単位metadataに未対応であった。
旧adapterはlegacy initializeの重複も拒否する。ただし、実要求のparamsやRPC応答本文は取得していないため、
実ChatGPTの要求versionやinitialize失敗理由を断定しない。

修正範囲はsrc/mcp_adapter.py、tests/test_mcp_discovery.py、README、本追記。
2025-11-25の既存初期化と重複初期化拒否を保持し、2026-07-28のserver/discover／tools/list／tools/call／pingを追加した。
新方式は各要求のversionとclientCapabilities objectを必須検証し、結果にresultTypeとserverInfo metadataを付ける。
discovery/listはttlMs=0、cacheScope=private。未知versionは-32022と対応versionを返す。
新方式のmetadataを接続状態や実行権限として保存しない。公開ツールとoperator policy、socket境界は変更しない。
initialize/discover/listの監査は固定method名とerror codeだけで、params・clientInfo・生IDは記録しない。
単一ページのtools/listはcursor省略/nullを受理し、存在しない継続cursorを拒否する。

新規11テストは成功（初回0.879秒、最終0.867秒）。実stdio、metadata異常、未知version、legacy状態分離、
書込み・任意path拒否、監査失敗時の拒否、daemon不在時のtool errorを含む。
実tunnel-client v0.0.18＋loopback模擬control plane＋専用daemonでは、legacy handshakeなしで
server/discover、読み取り3ツールのlist、statusとlocal task_countsの一致を確認した。
この試験では実Platformへ接続せず、専用の公開synthetic fixture値のみ使用した。
Codexモデル生成、実Tunnelからのsubmit/cancel、Harpoon有効化、既存Tunnel/daemonの停止は行っていない。

回帰結果は全成功ではない。`python3 -B -m unittest discover -s tests -v`は185件中182成功、
既知のsystemd/AppArmor関連2 FAILに加え、既存の
test_worker_sigkill_and_other_session_continuesで1 ERROR（189.224秒）。
cancel後のstatus取得で、完結した応答の前にEOFとなった。
原本のfixtureのみの試験出力は`.runtime/phase2-e3-2-evidence/mcp-discovery-regression.log`。
今回FIFO試験は成功している。以前のFIFO試験で観測されたEOFと同一原因かは不明である。
既存テストファイルの変更・削除・skip・assert緩和はない。

EOFについては専用fixtureの出力・要求所要時間・状態・DB整合性を保存する診断を追加し、
同じworker SIGKILL試験を新しいfixtureで上限2回実施した。
両回は成功（1.167秒、1.237秒）、DB integrity_check=ok、対象状態はUNKNOWNとCANCELLED、
foregroundプロセスは試験終了前の観測時点で生存し、foreground出力は空だった。
診断証跡は`mcp-discovery-eof-diagnostic.log`と`mcp-eof-diagnostic/`。
再現しなかったため解消とは扱わず、全体試験の1 ERRORをそのまま残す。
静的にはdaemonのsocket IO例外・3秒期限超過・終了時に応答前接続が閉じられる経路があるが、
最初の失敗fixtureは既存tearDownにより破棄済みで、閉鎖理由を確定する証拠はない。
MCP adapterを使わない試験であり、今回の修正との因果関係は未確認。
既存daemon実装へ推測による修正や自動submit再試行を追加していない。

全体試験開始後のnotification監査ガードの小変更は、最終コードで新規11件と既存MCP21件を再検証した。
21件は全成功（9.736秒）。ログはmcp-discovery-targeted.log、mcp-discovery-e1-regression.log。
新方式の仕様照合・fixture成功と、全体回帰に残る3件の問題を区別する。

実ChatGPT側のプラグイン作成成功・ツール呼出しは引き続き未検証。
起動中stdio adapterは旧コードを保持するため、修正版の適用には利用者によるTunnel前景processの再起動が必要。
daemonの再起動やunit導入は不要。同じruntimeキーを再利用できるが、停止後のshellに変数がなければ安全なreadで再設定する。
再起動後も同じ画面操作が失敗する場合は、metadataのみのprotocol監査とmetricsの増分で次の拒否箇所を調べる。
Harpoonを追加公開して回避しない。実接続の成功をfixture結果から推定しない。

参照した公式資料:

- [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [MCP 2026-07-28 Discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)
- [MCP Base Protocol](https://modelcontextprotocol.io/specification/2026-07-28/basic/index)
- [MCP Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [MCP公式schema](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/schema/2026-07-28/schema.json)

公式schemaは調査証跡配下に保存し、必要なmetadata／result項目を照合した。外部schema validatorは導入していない。

## 読み取り接続の再確認（2026-10-29 11:43 JST）

開始commitはbaee13c。既存の未追跡報告2件以外に作業ツリー変更はなく、重複実装は行わなかった。
既存AceIS MCO Dev接続に読み取り3ツールが登録されていることを確認し、statusとlistを各1回呼び出した。
両方ともMCP -32603 / request timed outで失敗。getの追加呼出しや自動再試行は行っていない。
登録済みのtool metadataが取得できることと、現在の実RPC成功を区別する。

11:43:55 JSTのローカル確認では、専用runtimeのUnix socketとhealth URLファイルは存在せず、
対象の起動経路に一致するdaemon・Tunnel・adapterプロセスも見つからなかった。
したがって起動中プロセスの開始時刻や修正反映は今回確認できない。
SQLiteをmode=roで読み、COMPLETED=1、RUNNING/QUEUED=0と保存済みtask ID
fc9c4ea6-a6f3-493f-998d-209a65e74bcbの保持を確認した。DB状態は変更していない。
VS Code終了との因果関係や、各プロセスの終了時刻・終了理由は未確認。

後続の音声経由の補足で、利用者が「今トンネルを閉じてしまったから、つながらないはずだ」と
手動停止を申告した。したがって今回のstatus/listタイムアウトは、停止中の接続への呼出しとして扱い、
discovery修正の不具合・回帰を示す根拠にはしない。
この申告が説明するのはTunnelの停止であり、daemonの停止理由や終了時刻までは確定しない。
補足後に同じ接続試験を繰り返したり、別のタスクを投入したりしていない。

一方、保存済みadapter監査mcp-f3184d39-2718-41c8-84cf-77a392d95934.jsonlには、
11:17:15のserver/discover、tools/listへのerror_code=null、
11:19のorchestrator_list_tasks、orchestrator_status、orchestrator_get_taskへのoutcome=returnedがある。
getのtask IDは保存DBと一致する。この監査は修正後のdiscovery処理が過去に動いた証拠だが、
呼出し元のUIや応答本文は記録されておらず、実ChatGPT画面での回答表示・往復成功は断定しない。

現在の当方プロセスにCONTROL_PLANE_API_KEYはない（有無だけ確認）。
他プロセスの環境・秘密ファイルを読んでキーを取得せず、Tunnel再起動は保留した。
既存daemon保持という依頼に対し、現状はdaemon自体が不在のため、Tunnelだけの再起動では復旧しない。
次に必要なのは、利用者端末で専用daemonを前景起動し、同じruntimeを指定した読み取りTunnelを
既存キーの安全なread/exportで起動すること。その後、登録済み接続のstatus/getを再確認する。
新しいTunnel作成・再登録や認証設定変更が必要とは現時点で判断しない。

証跡は`.runtime/phase2-e3-2-evidence/read-only-followup-20261029-1143.json`。
今回の変更は本追記と秘密情報を含まない調査証跡のみ。ソース修正・全面回帰・モデル生成は行っていない。
プロセス停止・起動、systemd導入、認証やCodex設定変更、task投入・cancel、公開・pushは行っていない。

## PoC起動・停止スクリプトのローカル実装（2026-10-29 12:06 JST）

音声経由の依頼に基づき、設計・ローカル実装・synthetic fixture試験までを実施した。
本適用、実キー読取り、認証ファイルの作成/移動/copy/chmod、commitは承認範囲に含めず実施していない。
開始HEADはbaee13cで、本報告の先行する未コミット追記と既存未追跡報告2件を保持した。

追加: scripts/poc_services.py、tests/poc_services_driver.py、tests/test_poc_services.py、docs/poc-services.md。
更新: scripts/service_wait.py（daemon_readyへの任意manager_env引数）、README、本報告。
既存unit、daemon/runner、scheduler、MCP adapter、AppArmor案、既存テストは変更していない。
systemd user transient serviceを用い、独自のdaemon supervisorやnohup/PID kill方式を追加しない。
plan/start/status/stopを用意し、start/stopは別途承認後の--applyを必須とした。
実キーはnative clientのfile:参照だけで読み、スクリプトはmetadataのみを調べる。
normal modeのAppArmor問題を解決したとは扱わず、既存の明示PoC bwrapモードを維持する。

適用案のunitはagentorchestrator-poc-365d01e41070-daemon.serviceと
agentorchestrator-poc-365d01e41070-tunnel.service、対象は既存の専用PoC runtime。
既存のagent-orchestrator.service / aceis-mcp-tunnel.serviceは導入・上書きしない。
Description nonceとInvocationID、state/lockで所有世代を確認し、同名再利用・二重start・stale stateを拒否する。
state/log/health以外のruntime状態を直接書き換えず、pending taskがあれば起動前に照合を求める。
statusは現在のreadinessも確認し、保存済みREADYだけで接続成功と報告しない。

確認時の既存キー候補<CONTROL_PLANE_KEY_FILE>はUID1000・mode0664のため利用拒否。
標準credential予定先は不在。実キーの内容は調べていない。
承認対象は、既存キー保管先の確定、必要なら利用者による0600への変更、
同UIDのnative Tunnel processによる継続的なfile読取り、上記2 unitの手動起動とログ保存。
0600はUID単位の保護であり、同UIDの別processからキーを隔離する保証はない。
詳細と具体的なplan/start/status/stop手順はdocs/poc-services.mdへ記載した。

新規の最終14テストは全成功（2.764秒）。
最小環境の実CLI start/ready、起動元process終了とPTY閉鎖後の存続、CLI status/stop、
実tunnel-client＋loopback模擬planeのdiscovery/読み取り3ツール/status、所有対象のみ停止を確認した。
unsafe/missing key、重複start、flock、別InvocationID、stale state、pending taskの拒否、
daemon異常終了でTunnel未起動、Tunnel readiness失敗時の有限な後片付け、
synthetic値のargv/log非混入も確認した。fixture生成のkeyは専用一時領域の公開synthetic値のみ。
fixtureのtransient unitは全て停止・収集され、試験後のagentorchestrator-poc-*.service列挙は空だった。

関連回帰は既存ReadinessTests4件＋discovery11件＝15件が全成功（1.060秒）。
最初の新規10テストは、credential用の祖先permission検査をruntime markerにも適用したため全件ERRORだった。
既存src/リポジトリdirectoryが0775であることを確認し、credential検査と既存runtime/state検査の責務を分けた。
runtime/logsの0700とsymlink拒否、credentialの0600と祖先検査は維持し、ホストpermissionは変更していない。
この原因修正後に10件成功、その後に最小user bus環境・pending work等を追加して最終14件を検証した。
初期失敗を削除せずpoc-services-tests.logに保持した。
最終ログはpoc-services-cli-final.log、関連回帰はpoc-services-related-regression.log。
全て.runtime/phase2-e3-2-evidence配下。全面回帰や新しいモデル生成は行っていない。
以前の全体試験に残るAppArmor2 FAIL・EOF 1 ERRORは、この結果で解決扱いにしない。

実環境ではplanのみ実行し、key metadataの不適合と作成予定先をpoc-services-plan.jsonへ保存した。
実キーによる常駐、実Tunnel再接続、現時点のChatGPT往復、logout/reboot後の存続は未検証。
enable/lingerや自動restartは追加していない。実サービスへの適用は利用者承認待ちで停止する。
変更7ファイルのworking-tree内容を既存secret patternで監査し、検出0件（poc-services-source-audit.json）。
index操作・commitは行わず、実runtimeの新しいstate/lock/healthは不在、既存キーのmode0664は保持した。

## 利用者承認後の実PoC適用と読み取り往復（2026-10-29 12:15〜12:21 JST）

後続の音声確認で、既存キーをownerのみ読める権限へ変更し、そのキーを使ってdaemonと
読み取り専用Tunnelを端末から独立して動かす具体案に、利用者が「オッケー」と承認した旨を受領した。
承認対象のキーinode、runtime、2 unitとfile:参照だけに適用を限定した。
実行直前のHEADはbaee13c、未コミット作業は前項のまま保持した。
両unitはnot-found、既存Tunnel processと対象socketは不在、DBはCOMPLETED=1のみだった。
未知の仕事・別所有unitの停止や奪取は行っていない。

キー<CONTROL_PLANE_KEY_FILE>をlstatでregular、非symlink、UID1000、単一hard linkと再確認し、
O_NOFOLLOWで開いたFDのinode一致を確認してfchmod(0600)した。
FDは内容のread/writeやtruncateに使わず、size不変も確認した。コピー・移動・キー再発行なし。
変更後のplanはowner_uid=1000、mode0600、acceptable=true。
キー値を環境変数・argv・ツール出力へ展開していない。

`python3 -B scripts/poc_services.py start --key-file "$CONTROL_PLANE_KEY_FILE" --apply`がREADYで終了した。
起動処理中のstatusはflockにより一度拒否され、start終了後のstatusで確認した。
2 unitのExecMainStartTimestampはいずれも2026-10-29 12:15:47 JST。

| 対象 | MainPID | 所有UID | 状態 | systemd属性 |
| --- | ---: | ---: | --- | --- |
| agentorchestrator-poc-365d01e41070-daemon.service | 4107162 | 1000 | active / owned | transient、Restart=no、NRestarts=0 |
| agentorchestrator-poc-365d01e41070-tunnel.service | 4107170 | 1000 | active / owned | transient、Restart=no、NRestarts=0 |

Description nonceとInvocationIDがstateに一致した。
両MainPIDのPPIDは5758、当該親はsystemd --user、tty_nr=0。
起動CLIが終了した後も生存し、実際の端末を強制終了せず端末からの独立性を確認した。
native clientのargvは承認済みfile:参照と対象runtimeのread-only adapterに一致し、allow-writesなし。

daemon_ready=true、tunnel_ready=true。native healthではhealthz/readyzともHTTP200、
control_plane_poll.ok=true、result=ok。health listenerは127.0.0.1:37769。
現在のportは動的で、以後は固定portでなくpoc-service-health-urlを使用する。
資源制限の実測値はdaemon cpu.max=50000 100000、memory.max=536870912、
Tunnel cpu.max=25000 100000、memory.max=268435456。両方pids.max=64、swap.max=0だった。
file logはowner1000・0600で確認時点0 byte。対象unitのjournalも含む既存secret pattern検査は検出0件。
これはパターン検査であり、実キー値を読み出してログと全文照合したという意味ではない。

当方の既存AceIS MCO Devコネクターを使った実Tunnel経由のbusiness tool呼出しは次の3件すべて成功した。

| ツール | call_id | 確認結果 |
| --- | --- | --- |
| orchestrator_status | fe5ac830-3283-4c42-930b-9e13be3732fd | COMPLETED=1、最大2、paused=false、active taskなし |
| orchestrator_list_tasks | 7d50101b-5800-4047-865b-c070f25bf364 | 保存済み完了task1件 |
| orchestrator_get_task | ac7daf86-1558-4ed6-950c-c0b092d64ef0 | COMPLETED、Codex SUCCEEDED、verification PASSED、completion_verified=true |

経路は既存AceISコネクター→既存OpenAI Tunnel→native client→stdio adapter→local Unix socket→daemon。
getのtask/execution/thread/turn ID、受入検証結果とstatusの集計値をlocal daemonと照合し、全項目一致した。
listは縮約した一覧情報であり、詳細なID/完了検証はgetを使って照合した。
現在公開されるtool metadataは既存の読み取り3件だけで、書込みツールは有効にしていない。
今回raw JSON-RPCのserver/discover/tools/listを独立再送したとは扱わず、
実コネクターのtool公開と3 business callの成功を記録する。

外側の親セッションからも03:17 UTC（12:17 JST）にstatus成功との報告を受領した。
そのcall_id=d5901871-692e-46b6-96da-4b8dc207f829は、当方が読んだ現adapterのend/returned監査に存在した。
親の独立読取りによるowner/mode・2 unit・PID/PPID/TTYの報告も当方の観測と一致する。
これは親からの報告とserver側監査であり、当方がChatGPT UI上の回答表示を直接観察したものではない。

証跡: poc-services-live-systemd.json、poc-services-live-mcp.json、poc-services-live-comparison.json、
および現runtimeのmcp-74c00ea9-b1f6-4dd9-8933-7f34c5bf0860.jsonl。
前3件は.runtime/phase2-e3-2-evidence配下の秘密値なし記録。
実task新規投入・モデル生成・submit/cancel・stop試験は行わず、サービスは起動したまま引き継ぐ。
enable/linger/ログイン時自動起動、常設unit導入、AppArmor変更、認証の新規発行や保管先変更、
commit/pushは行っていない。今回の適用にソース修正は不要だった。
実サービスのstop、logout/reboot、ChatGPT画面の回答表示は未検証。
既知の通常モードAppArmor2 FAIL・原因未確定EOFを解決したとは扱わない。

利用者向けの現在状態確認と、明示停止コマンド:

```bash
cd "${PROJECT_ROOT}"
python3 -B scripts/poc_services.py status
# 利用者が停止したいときだけ実行。現在は実行していない。
python3 -B scripts/poc_services.py stop --apply
```
