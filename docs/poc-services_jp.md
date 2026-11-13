# 端末から独立したPoCサービスの手動管理

パスは[表記規約](path-conventions.md)の疑似パスです。シェル例は同ページの変数を設定してから使用します。原本ログは変更していません。

状態: 2026-10-29、利用者承認後に実PoCの2 transient user serviceを手動起動済み。
実キーはnative clientがfile:参照で使用。キー移行・copy・commitは行っていない。
従来の通常モードunitとAppArmor設計は変更しない。
既存のPoC runner、systemd user manager、native tunnel-client、readinessを使う。
新しいdaemon実装や独自のPID supervisor、SDK、認証方式は追加しない。

## 承認済みの適用対象

| 項目 | 対象 |
| --- | --- |
| runtime | `<PROJECT_ROOT>/.runtime/development-poc-e32-c00a7a45` |
| transient daemon unit | `agentorchestrator-poc-365d01e41070-daemon.service` |
| transient Tunnel unit | `agentorchestrator-poc-365d01e41070-tunnel.service` |
| Codex | `<CODEX_BINARY>` |
| Tunnel client | `~/.local/opt/tunnel-client/v0.0.18/tunnel-client` |
| profile | リポジトリ既存 `systemd/aceis-mcp-tunnel.yaml` |
| 接続先 | 既存aceis-mcp-dev / tunnel_6ae1d745be7c8191b6d502b613317b4c |
| 非秘密state | runtime内 `poc-services-state.json`（0600） |
| 操作lock | runtime内 `poc-services.lock`（0600、flock、強制解除なし） |
| ログ | runtime/logs内 `poc-service-daemon.log`、`poc-service-tunnel.log` |
| health locator | runtime内 `poc-service-health-url`、loopbackのみ |

unit名はruntime絶対パスのhashから生成し、起動世代ごとのnonceとsystemd InvocationIDをstateへ保存する。
runtime/logsはUID所有0700、state/markerはprivate regular file、symlinkは禁止。
unitファイルを~/.config/systemd/userへ配置せず、既存agent-orchestrator.service等は上書きしない。
system/root権限、enable、linger、managerへのキーimportは使わない。

現在のキーは`<CONTROL_PLANE_KEY_FILE>`。内容を読まずにUID 1000・非symlink等を再確認し、
利用者承認を受けて当該inodeだけを0664→0600に変更した。
既存設計の標準予定先`~/.config/agent-orchestrator/credentials/control-plane-api-key`へは移行していない。
`--key-file`で指定する承認済みファイルは、リポジトリ外、UID 1000所有、0600、非symlink、1〜16384 byte、
単一hard linkとし、第三者が書ける祖先directoryを拒否する（root所有sticky一時directoryはfixture用に許す）。
自動作成・copy・move・chmodはしない。

**本工程で利用者が承認した事項:**

1. 既存`<CONTROL_PLANE_KEY_FILE>`を同じ保存先のまま継続利用する。
2. 当該ファイルのmodeだけを0600に変更する。スクリプトに自動chmod機能は追加していない。
3. 当該キーを、明示stop・障害・user manager停止まで存続するTunnelプロセスから読む。
4. 上表の専用runtimeと2つのtransient user unitを手動起動し、ログを保存する。

native `--control-plane.api-key=file:/absolute/path`がキーを読む。スクリプトはmetadataのみ検査する。
LoadCredentialによるruntimeコピーも作らず、秘密値を環境変数やargvへ展開しない。
アクセス権はUID単位であり、同じUIDの全プロセスからキーを隔離する保証はない。
ファイル差替え・キー失効・ローテーションは利用者管理で、本機能は実施しない。

## 承認済み対象の操作手順

読み取りの計画表示は秘密値を読まず、unitやstateを作成しない。

```bash
cd "${PROJECT_ROOT}"
python3 -B scripts/poc_services.py plan --key-file "$CONTROL_PLANE_KEY_FILE"
```

キーfileと所有状態を確認して手動startする。次のstart/statusは本工程で実行済み。
既に起動中の状態でstartを重ねると拒否する。

```bash
python3 -B scripts/poc_services.py start --key-file "$CONTROL_PLANE_KEY_FILE" --apply
python3 -B scripts/poc_services.py status
```

startはdaemon socketとunitの衝突を拒否し、保存DBのQUEUED/RUNNING/VERIFYINGがあれば先に照合を求めて停止する。
未知の仕事を起動の副作用としてdispatchしない。UNKNOWNは既存runnerの復旧規則に従い自動再実行しない。
daemonは`--development-poc`を明示し、既存bwrap経路と資源制限を維持する。
daemon readiness（自unit MainPIDとの親子関係を含む）確認後、Tunnelを起動する。
healthz・readyz・control-plane pollの成功後にREADYとする。MCPやChatGPT往復成功は別の検証である。
statusはunit所有世代と現在のreadinessを確認し、古いREADY stateだけで成功扱いしない。

各processはsystemd user managerが親となり、端末を閉じても存続する。
最後のログアウト時の存続は保証せず、OS再起動後も自動起動しない。enable/lingerは変更しない。
Restart=noで、自動的な再接続・再起動ループやタスクの再投入は追加しない。

停止はこのスクリプトが記録した2 unitだけを対象にする。

```bash
python3 -B scripts/poc_services.py stop --apply
```

両方のDescription nonce・Transient属性・InvocationIDを先に検証し、Tunnel→daemonの順に停止する。
PIDだけを信用したsignal、stale lock削除、他runtimeの停止はしない。
daemon停止は既存foreground helperのSIGTERM→shutdown処理に従う。
将来このruntimeでタスクが実行中なら中断・UNKNOWNが発生し得る。副作用のrollbackは保証しない。
TimeoutStopSecはdaemon60秒、Tunnel20秒。期限後の当該unitの後片付けはsystemdが担当する。
stop確認ができなければ成功を返さない。

復旧はstatus→既存runnerの状態/資源照合→stop→新たなstartという手動の順で行う。
同名unitが別世代になった場合、所有stateがない場合、途中起動の所有が確定しない場合は安全に拒否する。
stateやstale health fileを自動で消して再試行しない。残った証拠とunit所有者を確認して別途判断する。
start中の例外は識別できた自unitだけを有限に停止し、失敗をFAILEDまたはCLEANUP_UNCONFIRMEDとして残す。
フロントエンド自体がSIGKILLされた場合の全cleanupを保証せず、次回は不完全stateからの無条件startを拒否する。

## 環境・ログ・資源

user busは固定`/run/user/<UID>`（owner UID、0700、非symlink）と、そのUID所有のbus socketを検証する。
systemctlとdaemon readinessの両方へ、同じ固定環境を渡す。
実serviceコマンドも`env -i`でPATH/HOME/LANGと固定user bus情報だけを受け取り、
親端末やuser managerのAPIキー・MCP target上書き環境を引き継がない。
MCP adapterは読み取り3ツールのみ、外部listenerや追加channelは作らない。

既存unitと同じCPUQuota（daemon50%、Tunnel25%）、MemoryMax（512MiB/256MiB）、
TasksMax64、swap0、core0、KillMode=mixedを適用する。
worker/App Serverは既存scope管理のままで、outer unitの制限が全worker合計のhard quotaとは主張しない。
ログはprivate runtimeにappendしLimitFSIZE=64Mを設定する。自動削除・pruneはしない。
native raw HTTP loggingはfalse。秘密値を含むdebug/環境dumpは追加しない。
安全なpermissionと同UID信頼を前提とし、任意の同UIDプロセスやsource改ざんに対する境界は追加しない。

## 対象試験と未検証

```bash
python3 -B -m unittest discover -s tests -p test_poc_services.py -v
PYTHONPATH=tests:scripts:src python3 -B -m unittest test_phase2_e3.ReadinessTests test_mcp_discovery -v
```

fixtureはUUIDの専用PoC runtime、公開synthetic値の一時ファイル、実systemd user transient unit、
実tunnel-client v0.0.18、loopback模擬control planeを使う。実キーや実Platformは使わない。
CLI startの呼出し元にuser bus環境がない状態で起動し、端末PTYを閉じた後も存続し、
CLI status/stopも同じ最小環境で動くことを検証する。tool discoveryとstatusは模擬plane経由で照合する。
同名重複・世代違い・stale state・unsafe key・pending work・起動失敗時cleanupを検証する。
ソースとunit内の秘密値、argv/logへのfixture値漏れを確認する。

承認後の実起動では2 unitのactive/owned、healthz/readyz HTTP200、control-plane poll成功を確認した。
既存AceISコネクターのstatus/list/getが成功し、保存済み完了taskの詳細をlocal daemonと照合した。
起動コマンド終了後も、両MainPIDの親はsystemd --user、tty_nr=0だった。
実サービスのstop、ChatGPT画面での回答表示、ログアウト・OS再起動は未検証。
元の全体回帰に残るAppArmor関連2件・原因未確定EOFは、この追加機能の成功で解決したとは扱わない。
詳細な実測結果と初期失敗記録は[PoC報告](phase2-e3-2-report.md)を参照。

公式参照:

- [systemd-run 259（端末から分離したuser transient service）](https://www.freedesktop.org/software/systemd/man/259/systemd-run.html)
- [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [OpenAI tunnel-client configuration（file:参照）](https://github.com/openai/tunnel-client/blob/master/docs/configuration.md)


## 2026-10-29 bounded localhost 推論の承認済み追加

利用者の個別承認により `aceis_local_llm_generate` のみを4操作目として追加した。
2026-10-29 09:08 UTC、所有世代を確認して専用Tunnelだけを反映しREADY。
daemonのPID/InvocationID、canonical policy/guard、キー保存・公開先・unitのsecurity属性は不変。
現在のTunnelは `--allow-local-llm` を付け、submit/cancelは無効のまま。
明示stop後に同じ承認済み構成を手動startする場合は次を使う（起動中の再実行は禁止）。

```bash
python3 -B scripts/poc_services.py start --key-file "$CONTROL_PLANE_KEY_FILE" --allow-local-llm --apply
```

引数を省くと従来の読み取り3操作になる。新操作はlocalhost resident30B限定、
逐次、入力16384 UTF-8 bytes、出力1024 tokens、MCP全体90秒以内。
verified common policy本文を付与し、出力はNOT_EXECUTEDの助言として返す。
新規namespace・生成コード実行・別モデル起動・API課金・自動再送はしない。
51件のofflineテストとcatalog/healthを確認。反映担当から追加生成は0回。
ChatGPT側からの推論E2Eは親が単発で確認する別工程であり、catalog成功と区別する。
停止は既存の `python3 -B scripts/poc_services.py stop --apply`。

## 2026-10-30 現在の運用範囲

上記の3/4操作やChat往復未確認の記述は各時点の履歴。現在は既存7操作＋mailbox4操作の計11操作、ChatGPT UIでの更新と限定1往復の回答保存・回収を確認済み。親dotの直接catalogと休止Chatの起動は別途未確認。

保存済みkey/codex/3flagを引き継ぐ`resume-plan`、mailbox flagだけを外す`rollback-plan`を使用する。planは実行せず、preflight/stop/start/statusの引数配列を返す。UNKNOWN・WAITING_FOR_HUMANを含む未解決仕事はstop直前にも拒否する。既存キー値は読まず、CLI既定値への切替はしない。通常手順・制限・検証は[mailbox運用](mailbox-operations.md)を参照。
