# Phase 2-E2 — Secure MCP Tunnel ローカル疎通検証

パスは[表記規約](path-conventions.md)の疑似パスです。シェル例は同ページの変数を設定してから使用します。原本ログは変更していません。

実施日: 2026-10-28（Asia/Tokyo）。開始commit: `07b9808218c50b4ef4624f729d1c9371af075c8c`。
本書はAIによる調査・実測の要約であり、原出力や公式仕様そのものではない。

## 結果: Tunnel接続・readiness成功、Tunnel経由のMCP呼出しは未検証

公式tunnel-clientの署名・checksum・SBOM検証、ユーザー領域への導入、実CLI確認、
専用daemon＋読み取りstdio MCPのローカル疎通、既存Tunnelを参照するprofile作成まで実施した。

初回は実行環境の`CONTROL_PLANE_API_KEY`が未設定だったため、利用者の停止条件に従って停止した。
その後、利用者の操作端末でdoctorを実行した結果が提示され、result=ok、profile_load／control_plane_api_key等のPASSを確認した。
これは利用者提供ログによる確認で、当方が同じ端末の資格情報を受け取って再実行した結果ではない。
その後、利用者端末でhelperを実行して生成されたtunnel-attempt-b33015d5-bd7c-454c-b7df-3541b4d7d2dc.jsonを当方が読み取り確認した。
healthz／readyzともHTTP 200、control_plane_poll.ok=true、ready=true、Tunnel metadata取得とstdio子process起動を確認した。
終了コード0、survivors=[]で、今回の接続・readiness・意図した停止後の解放は成功している。
ChatGPT側の接続登録・実呼出しは未実施。Tunnel接続成功を、ChatGPTからのMCP操作を含む全完了とは扱わない。
キーの新規発行・他のキーやCodex認証の探索／流用・組織権限や関連付け変更は行っていない。

対象は利用者指定の既存Tunnelのみ:

- 名称: `aceis-mcp-dev`（利用者の指定。サーバー側照合は未実施）
- ID: `tunnel_6ae1d745be7c8191b6d502b613317b4c`
- profile: `aceis-mcp-dev-readonly`

新しいTunnelは作成していない。削除・再作成もしていない。

## 事前確認

グローバルAGENTS.md、README、E1報告、接続設計、既存adapter・testを確認した。
開始時のtracked treeはclean、前工程の`docs/config-change-investigation.md`だけが未追跡、remoteなし。
調査したorchestratorのdaemon／adapter processと専用Unix socketは開始時に見つからなかった。
既存Codex daemon／IPCには接続・停止・変更していない。

OpenAI Docsコネクターの取得はHTTP 403になったため、公式Webページで現行手順を確認した。
既存設計書のコマンドをそのまま実行せず、導入したbinaryのhelpと照合した。
公式ページから案内されているOpenAIのGitHub releaseを使用した。
[公式Tunnelガイド](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

## 配布物の真正性・導入

| 項目 | 実測 |
| --- | --- |
| OS／arch | Linux x86_64、linux-amd64成果物を選択 |
| release | v0.0.18、GitHub公開時刻2026-10-23 20:25:58 UTC |
| source commit | `274a5588414819f063d27100a4fb1096aae3830f` |
| 配布ZIP | tunnel-client-v0.0.18-linux-amd64.zip |
| ZIP SHA-256 | `3c82307316acd93c88a384b62af1d81d3f4cb0efa975f9c0bfaef922866a04c4` |
| 実version出力 | `0.0.18+274a5588414819f063d27100a4fb1096aae3830f`（git shaも一致） |
| 導入先 | `~/.local/opt/tunnel-client/v0.0.18/tunnel-client` |

[OpenAI公式release](https://github.com/openai/tunnel-client/releases/tag/v0.0.18)のGitHub API digestと、
公式SHA256SUMS.txtの双方にZIPが一致した。
公開されていたSigstore provenance bundleで、ZIPとSHA256SUMS.txtの署名検証も実施した。
公式READMEに従い、repo、signer workflow、source ref、source digest、signer digest、SLSA predicate、
self-hosted runner拒否を指定し、いずれもexit 0だった。

```text
repo: openai/tunnel-client
signer workflow: openai/tunnel-client/.github/workflows/release.yml
source ref: refs/tags/v0.0.18
source/signing digest: 274a5588414819f063d27100a4fb1096aae3830f
predicate: https://slsa.dev/provenance/v1
deny-self-hosted-runners: true
```

検証用ghは既存環境に存在しなかったため、GitHub CLI公式releaseのv2.103.3を一時領域に取得した。
配布元HTTPS、公式API digestとchecksum一覧を照合し、binaryを一時領域だけで使った。
ghのシステム導入、ログイン、Git設定変更は行っていない。
attestation検証には専用HOMEと明示した非秘密envだけを渡した。

さらに、同じOpenAI source commitの公式verify_release_archive.sh／verify_release_sbom.shを使用し、
ZIP内の安全なregular file構成、埋込みSBOMとsidecarの一致、SPDX inventoryとpayload hashの一致を検証した。
3段階ともpassed。これらが成功してからZIPを新しいユーザー領域へ展開し、binaryを初めて実行した。
既存ファイルを上書きせず、PATH・system設定は変更していない。
この確認は既知の脆弱性ゼロや将来の安全性を保証するものではない。
[公式検証手順](https://github.com/openai/tunnel-client/blob/v0.0.18/README.md)

## 実CLIとMCP revision

version、help、help quickstart、init／doctor／run／dev／profiles／healthのhelp、stdio sampleを実測した。
以下が実CLIでも確認できた。

- initのprofile-dir、tunnel-id、mcp-command、health-listen-addr。
- control-plane-api-key-refの既定値は`env:CONTROL_PLANE_API_KEY`。
- doctor／runのcontrol-plane.api-keyも秘密値のenv／file参照を指定する方式。
- healthのrequire-control-plane-pollは、単なるローカルhealthとcontrol-plane poll成功を区別するためのもの。
- stdioのlegacy初期化guardと、成功したinitialize後に通知を補うstdio_send_initialized_notification設定。

v0.0.18公式設定資料では、legacy requestはinitializeとnotifications/initializedを必要とし、
2026-07-28以降のself-contained requestとは別に扱う。
これはE1の2025-11-25 adapterとの設計上の整合を示す。
ただし、**今回実tunnel-clientを起動しておらず、実際の転送経路のrevision互換性は未検証**。
ローカルadapterの2025-11-25初期化成功と混同しない。
[v0.0.18のstdio仕様](https://github.com/openai/tunnel-client/blob/v0.0.18/docs/configuration.md#stdio)

## ローカル読み取りMCP実測

専用runtime: `<PROJECT_ROOT>/.runtime/phase2-e2-readonly/`。
既存の本物の前景daemonを新規runtimeで起動し、E1のsrc/mcp_adapter.pyをstdio子processとして実行した。
この疎通では--allow-writesを指定せず、submit／cancelも呼んでいない。
Codexモデル生成は0回、task数は前後とも0件だった。

| 検証 | 結果 |
| --- | --- |
| initialize | 2025-11-25で成功 |
| notifications/initialized | 送信後にtools処理へ移行 |
| tools/list | orchestrator_status／orchestrator_list_tasks／orchestrator_get_taskの3件だけ |
| annotations | 3件ともreadOnlyHint=true |
| status | 成功、task_counts={} |
| list_tasks | 成功、空の一覧 |
| get_task | 存在しないUUIDで安全なtool error。既存taskを作成・変更しないため、成功する既存task読取りは今回は未実施 |
| daemon停止後 | adapterは固定のtool errorを返し、別endpointやdaemon自動起動へfallbackしない |
| 解放 | 自分で起動した検証用daemonのみ正常停止、exit 0。adapterもEOFで終了 |

daemon停止は検証用runtimeだけの後片付け。既存daemonは停止・再起動していない。
これらはローカル直結の実測であり、Tunnel越しのMCP疎通ではない。

## profile

公式initがexit 0で作成したファイル:

```text
<PROJECT_ROOT>/.runtime/phase2-e2-evidence/profiles/aceis-mcp-dev-readonly.yaml
```

固定した設定:

- tunnel_id: 利用者指定の`tunnel_6ae1d745be7c8191b6d502b613317b4c`。
- MCP command: `/usr/bin/python3 -B "$PROJECT_ROOT/src/mcp_adapter.py" --runtime "$STATE_DIR"`。
- APIキーは値でなく`env:CONTROL_PLANE_API_KEY`だけ。
- health listenは`127.0.0.1:0`。MCP用HTTP listenerやUnix socketの直接外部公開はしない。
- 新規profileだけに`mcp.stdio_send_initialized_notification: true`を追加した。成功した初期化後に通知を補い、重複通知を抑止する公式オプション。
- profileは0600。既存profileやCodex設定を変更せず、remoteの組織・workspace関連付けも変更していない。

initの生成成功に加え、その後の利用者提示doctorログでprofile_loadのPASSを確認した。
profile作成をTunnel存在確認・認証成功・接続成功と解釈しない。

## 認証の停止点と再開手順

当方の初回確認では、実行環境でCONTROL_PLANE_API_KEYが非空かどうかだけを調べ、未設定だった。
後続の利用者提示ログでは、利用者端末のenv参照検査がPASSとなった。これをTunnelの認証・権限検証成功とは扱わない。
秘密値をチャット、モデル入力、argv、Git、ログ、profileに記録していない。
ChatGPTサブスクリプション認証、他の環境変数のキー、認証ファイルをコピー・流用しなかった。

再開時は、権限確認済みの既存runtimeキーを、**実際にtunnel-clientを起動する同じローカル端末**で受け渡す。
キーをチャットに貼らず、コマンド文字列に直接書かない。Bashなら次のようにechoされない入力を利用できる。

```bash
read -r -s -p 'CONTROL_PLANE_API_KEY: ' CONTROL_PLANE_API_KEY
printf '\n'
export CONTROL_PLANE_API_KEY
```

これはキー値を履歴へ記載しない方法であり、キーの発行・権限変更ではない。
別端末のexportは、すでに動作中のCodexや他の親processへ遡って反映されない。
同じ端末から検証用子processとして起動するか、既存の安全な実行環境へ秘密値を渡す必要がある。
不用意なset -x、env一覧出力、raw HTTP loggingを有効にしない。

以下は実CLI helpで確認した形を使う再開手順。doctorは利用者提示ログで成功を確認したが、run以降は未検証。
まず専用runtimeの検証用daemonだけを前景起動する。別の既存daemonへshutdownを送らない。

```bash
cd "$PROJECT_ROOT"
python3 -B src/runner.py --runtime .runtime/phase2-e2-readonly daemon
```

同じキー受渡し端末で、既存のprofileを使う自動確認helperを実行する。新しいTunnelやprofileを再作成する必要はない。

```bash
cd "$PROJECT_ROOT"
python3 -B .runtime/phase2-e2-evidence/check-tunnel-readonly.py
```

helperは固定profileで新規tunnel-clientを起動し、その存命中に専用のURLファイルを待ち、
health --require-control-plane-poll --jsonで確認する。接続確認は最大90秒、その後に有限のcleanupを行う。
既存clientが動いている場合は二重起動を拒否する。専用daemonが応答しなければ起動を拒否し、そのdaemonの停止操作は行わない。
生ログは保存せず、秘密値と既知の秘密形式を除去した結果をtunnel-attempt-UUID.jsonへ保存する。
helperはキーをファイルから読まず、呼出し端末の環境変数だけを使う。APIキーをargvへ渡さず、raw HTTP loggingも無効化する。

helperのfixture確認は4条件で成功した（キー未設定の拒否、daemon不在の拒否、mock health成功時の秘密値除去・新規client解放、既存daemonを停止しないこと）。
fixture結果と、その後の実Tunnel実行結果を分ける。実実行の結果は次節に記載する。

local health、認証・poll成功、MCP子process起動、legacy初期化とtools/listを別々に確認する。
loopback管理endpointを外部公開せず、試験終了時は検証用tunnel-clientと専用daemonだけを停止し、端末のキー変数もunsetする。
キー値やraw HTTPをログ化して原因調査しない。

### 利用者の60秒試験で観測した停止とURLファイル不在

利用者から、health-url.txt不在によるhealth failと、60秒後のadapterのKeyboardInterruptが提示された。
以前案内したtimeout --signal=INT 60sは期限で停止信号を送るため、このtracebackは予定されたSIGINTがPython子processへ伝わった場合と整合する。
これだけではTunnelの認証・接続失敗を意味しない。

v0.0.18の公式pkg/runtimehealth/health.goでは、health起動時にURLファイルを書き、OnStopでremoveURLFileを呼んで削除することを確認した。
したがって停止後のhealth照会は、今回のfile-not-foundになり得る。提示ログだけでは照会時刻が停止前か後か特定できず、起動中のURLファイル未生成も除外できない。
当方の読取り確認時にもファイルはなく、Tunnel processは見つからなかった。専用daemonのstatusも接続不能だった。
既存processへ停止・再起動操作は行っていない。
[対象版の公式実装](https://github.com/openai/tunnel-client/blob/v0.0.18/pkg/runtimehealth/health.go#L232)

このため、短い制限内に3端末で手動確認する手順を、上記helperで存命中に確認する方法へ変更した。
停止は新規clientのみにSIGTERMを送り、既存ProcessのPID/start照合付きcleanupを使う。
adapterのproduct sourceや既存testは変更していない。補助fixture結果はhelper-fixture-tests.json、参照した公式sourceも調査領域へ保存した。

### helperによる実Tunnel接続の確認

利用者が提示した実行証跡を読み取り確認した。
ファイル名: tunnel-attempt-b33015d5-bd7c-454c-b7df-3541b4d7d2dc.json。
これは当方が実資格情報を取得して実行したものではなく、操作者端末でhelperが生成した記録である。

- 2026-10-28 22:57:25 JSTにstdio MCP子process起動とloopback health listener起動を記録。
- metadataに返されたTunnel名は指定のaceis-mcp-devと一致。
- healthz=200/live、readyz=200/ready、control_plane_poll.ok=true、health.result=ok、全体ready=true。
- MCPツールの実呼出しはmcp_tools_called=false、ChatGPT呼出しはchatgpt_call_verified=false。
- cleanup.exit_code=0、survivors=[]、stderr_bytes=0。22:57:57 JSTまでに終了を記録した。
- 保存されたstdout末尾にはWARNが2件あり、いずれもstdio子processのsignal: terminatedと、それに伴う停止処理だった。
  helperによる意図した終了時刻と整合し、接続失敗の根拠にはしない。保存範囲にERROR記録はない。
- Harpoon起動ログもあるがtarget_count=0。公式設定資料では登録targetがないharpoon channelはunsupported_channelとなる。
  ローカルadapterの読み取り3ツール以外を実際に呼び出した証跡はない。

stdout総量は145,290 bytesで、保存は既存Processの末尾131,072 bytes上限により先頭の一部が省略されている。
末尾先頭の1行が途中から始まるのはこの切詰めによるもの。完全な原本ログが保存されたとは扱わない。
確認した要点はtunnel-connection-verification.jsonにも記録した。

この証跡によりcontrol planeとのpollとlocal readinessを実測できたが、Tunnel越しのinitialize／tools/list／tools/callの成功はまだ示されていない。
意図した停止・資源解放は確認できたが、異常なネットワーク切断後の回復試験とは区別する。

## 回帰試験

既存148件にはwrite機能を使うfixture試験があるため、禁止条件との整合を確認した。
利用者から「既存の隔離fixture内に限り148件すべて実行する」と回答を得てから実行した。
この例外を実Tunnelや今回の読み取り疎通へ広げていない。

初回は148件中1件がERROR。対象はtest_fifo_two_sessions_and_no_duplicate_dispatchで、
status応答前にUnix socketがEOFになった。失敗出力を保存し、成功とは扱わない。
コード・testを変更せず、対象の単独再実行は1件成功（3.412秒）。その後の全体再実行は**148件成功、133.178秒**だった。
初回EOFの根本原因は未特定であり、単独成功だけで解決済みとは扱わない。

## 証跡・保持・残余リスク

Git対象外の`.runtime/phase2-e2-evidence/`にmetadata、公式release情報、配布物、署名検証、
SBOM検証、CLI help、profile参照、ローカルMCP結果、認証の有無だけ、回帰出力、終了時照合を保存する。
署名・archive検証の作業用ghと配布ZIPもこの領域に保持する。
APIキーや認証本文は保存していない。source実装・既存testは変更していない。
Git commit、remote登録、push、systemd service導入・有効化、Platformの権限・課金設定変更は行っていない。
終了時照合ではconfigのhash、およびconfig・auth・AGENTS・history・session_indexのsize／mtime／mode／inodeが開始時と一致した。
今回作成したログ・profile・回帰出力への秘密情報パターン検査では検出0件。profileのenv参照は秘密値として数えていない。
既存tracked treeとcommitは不変。検証用controllerは終了し、専用socketも解放された。

利用者提供ログの要約はuser-doctor-result.jsonへ保存した。stdio targetのnetwork probe／OAuth metadataはSKIP、
codex_pluginも任意機能としてSKIPであり、pluginの導入や~/.codexの変更は行わない。
doctorのhealth_listener PASSは診断時のloopback bind検査であり、常駐Tunnelのreadinessではない。

未検証: runtimeキーの権限範囲全体、実tunnel-clientのMCP転送、異常ネットワーク切断後の回復、
ChatGPTからのdiscovery・呼出し、workspace関連付けのサーバー側状態。
また、元の配布物の脆弱性評価全体と、回帰試験初回EOFの根本原因は本作業で解消していない。

次工程のChatGPT操作は、Tunnel検証の完了後に別途行う。
対象workspaceで既存Tunnelを選び、読み取り3ツールだけが発見されることを確認する段階が必要だが、今回は登録しない。
実ChatGPT呼出し成功を報告するには、その段階の実測が必要である。
