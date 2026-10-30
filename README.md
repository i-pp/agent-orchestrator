# Agent Orchestrator — 開発運用とMCP 11操作

パスは[表記規約](docs/path-conventions.md)の疑似パスです。シェル例は同ページの変数を設定してから使用します。原本ログは変更していません。

ChatGPTサブスクリプション認証済みCodexを、専用App Server stdioから制御する最大2セッションのランナーとユーザーdaemonです。
Python 3.14標準ライブラリを使用し、新しいSDK・フレームワークやモデル推論の従量課金APIは使いません。
障害後の照合と再実行、Codexの正常終了とタスクの検証成功を分けます。専用workspaceの隔離・並列実行・独立レビュー、永続FIFOとUnix socket制御に対応します。現用MCPは状態参照3操作・既存ローカルLLM4操作・固定slotのmailbox4操作の計11操作です。ChatGPT側の要求取得・回答保存とローカルCodexによる本文回収を確認しました。実装ジョブのsubmit/cancelは公開せず、親dotの直接呼出しと休止Chatの起動は未検証です。新しい通常タスクのslotは候補準備と個別承認を経て追加します。独自の公開HTTP listenerは追加していません。
Secure MCP TunnelにはCodexのサブスクリプション認証とは別の既存runtimeキーとPlatform権限を使用します。キーは承認済みのprivate fileをnative clientが参照し、新規発行や課金・権限設定の変更は行っていません。

## 環境

Linux、Python 3.14、既存ChatGPTファイル認証、`codex`、`unshare`、`mount`、`setpriv`、`bwrap`、cgroup v2を委譲されたsystemd user managerが必要です。
検証済み環境はUbuntu 26.04.1 LTS、Python 3.14.4、`codex-cli 0.164.0-alpha.11.1`です。
検証時のバイナリの場所は `<CODEX_BINARY>`。
App Serverコマンドは実験的です。生成schemaと公式資料を照合し、`experimentalApi=false`で検証しました。
他OS・他CLI版・keyring認証は未検証です。隔離不能時の通常起動へのフォールバックはありません。

## 実行と受入条件

```bash
cd "$PROJECT_ROOT"
python3 src/runner.py check
python3 -c "from pathlib import Path; Path('.runtime/workspace/fixture.txt').write_text('phase1-marker: CERULEAN-17\n')"
python3 src/runner.py submit 'Read only fixture.txt and report its marker.' \
  --acceptance examples/read-fixture.acceptance.json
python3 src/runner.py run "$TASK_ID" --timeout 60
python3 src/runner.py show "$TASK_ID"
```

既存モードのcwdは`<runtime>/workspace/`、sandboxの既定はread-onlyです。既存モードは共有workspaceなので直列実行です。
並列・書込みには以下の`--isolated`モードを明示してください。
`submit`は投入のみ、`run`は同期実行です。別プロセスから`show`・`cancel`を呼べます。
`--runtime`は本プロジェクトの`.runtime/`またはその配下のみを許可します。
`--codex`で実行ファイルを指定できます。両オプションともサブコマンドより前に指定します。

受入条件の例:

```json
{
  "artifacts": [{"path": "fixture.txt", "sha256": "8c2e2a6987a3c8d4b699f8397994e7d0c8bccc1c07f3d5f386a2316f70709833"}],
  "tests": ["fixture-check"],
  "conditions": [{"kind": "result_contains", "value": "CERULEAN-17"}],
  "human": false
}
```

artifactsは通常ファイルの存在・任意のSHA-256、conditionsは結果文字列の一致・包含です。
testsは[verification-commands.json](verification-commands.json)の事前登録IDだけです。
argvと制限時間を投入時に固定し、実行前にも許可定義と照合します。モデル出力をシェルコマンドにしません。

受入条件なし、文字列条件だけ、またはhuman=trueなら、実行成功後もWAITING_FOR_HUMANです。
存在だけでは新規作成や意味的正しさを証明できません。目的に適した内容・ハッシュ・テストを指定してください。

```bash
python3 src/runner.py review "$TASK_ID" --decision approve \
  --actor operator --reason '成果物と根拠を確認した'
# 却下は --decision reject。失敗した検証や変更された成果物は承認で上書きできません。
```

## 隔離workspaceと最大2セッション

`--isolated`はタスクごとにworkspaceとCODEX_HOMEを作成します。
入力は`<runtime>/fixtures/<name>`に用意した専用fixtureのみ。製品リポジトリを渡したり直接変更したりしません。
symlink・hardlink・path traversal・workspace内mount境界は拒否します。

```bash
python3 src/runner.py --runtime .runtime/demo check
python3 - <<'PYFIXTURE'
from pathlib import Path
for name in ['demo-a', 'demo-b']:
    p = Path('.runtime/demo/fixtures') / name
    p.mkdir(parents=True)
    (p / 'expected.txt').write_text('phase2-c: ORCHID-23\n')
PYFIXTURE
python3 src/runner.py --runtime .runtime/demo submit \
  'Copy the exact bytes of expected.txt to result.txt, then verify byte equality with Python.' \
  --isolated --fixture demo-a --access workspace-write \
  --acceptance examples/workspace-fixture.acceptance.json
# demo-bも同様に投入して、返された2つのtask_idを指定
python3 src/runner.py --runtime .runtime/demo run-pair "$TASK_A" "$TASK_B" --timeout 120
python3 src/runner.py --runtime .runtime/demo cancel "$TASK_A"
python3 src/runner.py --runtime .runtime/demo cancel-all
```

`run`を別プロセスから2つ同時に呼んでも同じ制限が働きます。3つ目はQUEUEDのまま拒否し、自動待機・再試行しません。
`run-pair`は前景の2 workerを起動し、両方COMPLETEDなら終了コード0、それ以外は2です。
個別cancelは他タスクを変更しません。cancel-allはそのruntimeのQUEUED/RUNNING/VERIFYINGすべてに適用します。
run-pairへのSIGINT/SIGTERMは、その組の2件だけを中断します。

workspace-writeは明示指定したfixtureタスクのみ。受入条件に成果物SHA-256と許可済みテストの両方を要求します。
専用ファイルシステムには自分のworkspace・Codex状態・システム実行資源だけをmountします。
他workspace・元のHOME・coordinator DBは見せず、read-onlyはmountでも書込みを拒否します。
書込み時もCodex sandboxはworkspace-write、writableRootsは自身のworkspaceだけ、networkAccess=false、approval=neverです。
namespaceや入れ子sandboxの検証に失敗したら実行を拒否します。

## ImplementerとReviewer

roleはタスク定義のimplementer/reviewerです。同じモデル設定を利用でき、役割とモデル・人格を結び付けません。
Implementerの終了・資源解放後、lease下でスナップショットを作ります。

```bash
python3 src/runner.py --runtime .runtime/demo snapshot "$IMPLEMENTER_TASK"
python3 src/runner.py --runtime .runtime/demo submit \
  'Independently inspect expected.txt and result.txt, verify equality, and report findings with file/test evidence.' \
  --isolated --role reviewer --review-snapshot "$SNAPSHOT_ID" \
  --acceptance examples/workspace-fixture.acceptance.json
python3 src/runner.py --runtime .runtime/demo run "$REVIEWER_TASK" --timeout 120
python3 src/runner.py --runtime .runtime/demo release "$STOPPED_TASK"
```

Reviewerは新しいworkspace/threadで、スナップショットの独立コピーをread-onlyで検証します。
指摘・根拠はReviewer自身のresult、JSONL、独立したverificationsへ保存します。
ReviewerのCOMPLETEDはその受入検証の成功であり、元タスクの自動承認ではありません。
resumeは元のworkspaceとCODEX_HOMEを引き継いで直列化します。権限・役割の変更は拒否します。
releaseは終了済みworkspaceを再利用不可にし、ファイル・履歴は削除せず保持します。UNKNOWNが残る場合は拒否します。


## 中断・復旧・再開

```bash
python3 src/runner.py cancel "$TASK_ID"
python3 src/runner.py recover
python3 src/runner.py reconcile "$TASK_ID"
```

起動時にタスク／workspaceのleaseを取得できた残存RUNNING/VERIFYINGをUNKNOWNにします。ライブownerの実行中は変更しません。
recoverは不確実性の記録だけ、reconcileは所有プロセス・workspace・ログ・公式保存履歴の照合だけです。
どちらも新しいCodexターンを開始しません。`reconcile --offline`ではセッションの再開可能性を確定しません。

```bash
# 保存結果だけを再検証する。Codexを再実行しない。
python3 src/runner.py resolve "$TASK_ID" --reconciliation "$RECONCILIATION_ID" \
  --kind verify --actor operator --reason '保存turnと副作用を照合した'

# UNKNOWNを明示的に失敗として閉じる。
python3 src/runner.py resolve "$TASK_ID" --reconciliation "$RECONCILIATION_ID" \
  --kind fail --actor operator --reason '結果を確定できないため終了する'

# 許可判断だけを記録する。これだけでは投入・実行しない。
python3 src/runner.py resolve "$TASK_ID" --reconciliation "$RECONCILIATION_ID" \
  --kind allow_resume --actor operator --reason '終了状態と副作用を確認した'
python3 src/runner.py submit 'Continue the previous task.' --resume-of "$TASK_ID" \
  --resume-approval "$DECISION_ID" --acceptance examples/read-fixture.acceptance.json
python3 src/runner.py run "$NEW_TASK_ID" --timeout 60
```

変更・不明な副作用を認める判断には`--accept-side-effects`が必要です。未解放資源や不明なセッション状態は無視できません。
判断後のworkspace変更で許可は失効し、再開直前にも照合します。許可の二重使用は拒否します。
既知の終了タスクでworkspaceが不変なら`--resume-of`だけでも再開できます。
resumeは新しいtask/executionによる新しいturnです。途中の処理のメモリ復元や副作用の巻戻しではありません。
`--request-key`による同じ入力の再送は同じIDを返します。同一task_idとUNKNOWNの自動再実行はありません。

## 状態と識別子

| 状態 | 意味 |
| --- | --- |
| QUEUED | 保存済み、未実行 |
| RUNNING | 1つのexecutionでCodexを制御中 |
| VERIFYING | Codex結果保存済み、独立検証中 |
| COMPLETED | 実行・受入検証・必要な人間判断が成功 |
| FAILED | 明示的実行失敗、受入不合格、または人間の終了・却下判断 |
| TIMED_OUT | Codexまたは検証の期限到達。解放結果も記録 |
| CANCELLED | 中断要求による終了。解放結果も記録 |
| UNKNOWN | 実行・解放・永続化を確定できない。自動再実行しない |
| WAITING_FOR_HUMAN | 明示的な人間判断が必要 |

task_idは目的、execution_idは実行の所有・障害、thread_id/session_idはCodex会話、turn_idは会話内の1回の生成を識別します。
再開で会話IDが同じでも、task/execution/turnのIDは別です。
codex_status、verification_status、human_statusを分離します。CodexがSUCCEEDEDでも成果物不備ならFAILEDです。
completion_verifiedはPhase 2の完了条件を満たした場合だけtrue。旧COMPLETEDは履歴として保ち、LEGACY_UNVERIFIEDと区別します。

runの終了コードはCOMPLETED=0、それ以外の記録済み状態=2です。処理エラーは主に1、引数解析エラーは2です。
show/resolve等の終了コード0は操作の成功だけを示します。タスク成功はJSONを確認してください。

## 保存と移行

runtime内のtasks.sqlite（user_version=3）を直接操作し、Codex内部DBは操作しません。
DBはtasks、executions、transitions、reconciliations、verifications、decisions、workspaces、snapshotsに分けています。
daemonはscheduler_state／scheduler_options／scheduler_dispatchesを追加します（拡張schema_version=1、core user_version=3は維持）。
logs/<execution_id>.jsonl、reconcile/verifyログ、db-backups、専用codex-home、workspaceをruntime内に保存します。
showは最新の検証・照合根拠と判断履歴も返します。

Phase 1 DBはv0→v2→v3、Phase 2-A/B DBはv2→v3へ、排他・SQLite backup・トランザクションで移行し、旧列とデータを保持します。
未認識のschemaや独自index/trigger/viewは拒否します。

```bash
python3 src/runner.py db-rollback "$BACKUP_FILENAME"
```

v0への復元はPhase 2レコードや旧データ変更がない場合だけ、v2への復元はworkspace／snapshot追加や旧表の行変更がない場合だけ許可します。現DBも退避し、データを捨てる復元は拒否します。
daemonの追加schemaがある場合も、キュー記録を消すdb-rollbackを拒否します。
復元後に新コードを起動すると再移行されます。旧コード復元も含む手順は[復旧設計](docs/recovery-design.md)を参照してください。

既存~/.codexは名前空間内で隠し、認証だけをread-only mountでCodex標準処理へ渡します。
Pythonから既存認証・Codex内部DB・IPCを直接読み書きしません。認証更新に書込みが必要なら失敗します。
ログは秘密形式除去済みであり、無加工の原本ではありません。環境変数全体は記録しません。
DB・ログ・実験データ・バックアップはGit対象外。push・外部公開は行っていません。

## ユーザーdaemonと永続FIFO

専用runtimeを明示し、まず前景で起動します。このコマンドはserviceをinstall／enable／startしません。

```bash
# 端末1（前景）
python3 -B src/runner.py --runtime .runtime/daemon-demo daemon

# 端末2
python3 -B src/runner.py --runtime .runtime/daemon-demo pause
python3 - <<'PYDAEMON'
from pathlib import Path
p = Path('.runtime/daemon-demo/fixtures/demo')
p.mkdir(parents=True)
(p / 'fixture.txt').write_text('phase1-marker: CERULEAN-17\n')
PYDAEMON
python3 -B src/runner.py --runtime .runtime/daemon-demo --via-daemon submit \
  'Read fixture.txt and report its marker. Do not modify files.' \
  --fixture demo --request-key demo-read-1 --timeout 60 \
  --acceptance examples/read-fixture.acceptance.json
python3 -B src/runner.py --runtime .runtime/daemon-demo resume_scheduler
python3 -B src/runner.py --runtime .runtime/daemon-demo status
python3 -B src/runner.py --runtime .runtime/daemon-demo list --limit 20 --offset 0
python3 -B src/runner.py --runtime .runtime/daemon-demo --via-daemon show "$TASK_ID"
python3 -B src/runner.py --runtime .runtime/daemon-demo --via-daemon cancel "$TASK_ID"
python3 -B src/runner.py --runtime .runtime/daemon-demo shutdown
```

Socket投入は既定で独立したread-only workspaceです。書込みfixtureは従来どおり明示指定と内容hash／許可テストを要求します。
submitは保存だけで、schedulerがFIFO（SQLite挿入順）で最大2件を取得します。
pauseは実行中を止めず新規取得だけを止め、再起動後も保持します。
shutdown／SIGTERMは新規dispatchを止め、所有taskを中断し、未取得QUEUEDを残します。

同じrequest_keyは同じtask。応答が失われた場合もkeyを再利用し、盲目的に別taskを作らないでください。
statusはcore状態に加えscheduler_phase／worker_pending／scheduler_timeoutを返します。
成功確認にはcompletion_verifiedを使います。COMPLETED checkpoint直後もworker監督が未完了ならfalseです。

daemon実行中の通常run／run-pairは二重取得防止のため拒否します。投入・照会と実行は分離します。
UNKNOWN／WAITING_FOR_HUMAN／FAILEDを自動再実行しません。再開は明示した新taskと既存の許可・照合に従います。
未解放資源や未照合UNKNOWNでは新規取得を止め、解放・照合済みの独立UNKNOWNはworkspace単位で保護します。
容量予算に達しても証拠を削除せず、dispatch停止をstatus.blockedへ表示します。

user service定義は[systemd/agent-orchestrator.service](systemd/agent-orchestrator.service)です。
Phase 2-E3でTunnel unit、credential file参照、有限のreadiness確認を追加しました。
実systemdの専用transient fixtureで検証し、実サービスの配置・設定作成・手動start・enable・linger変更はしていません。
**systemd配下のuser namespaceがAppArmorに拒否されるため、Stage Aは未完了、実導入は禁止です。**
既存148試験の成功と、systemd配下でのタスク実行成功を区別してください。
解消条件と承認後の手順は[systemd導入設計](docs/systemd-deployment.md)、実測は[Phase 2-E3報告](docs/phase2-e3-report.md)を参照してください。
詳細は[daemon設計](docs/daemon-design.md)、[scheduler設計](docs/scheduler-design.md)を参照してください。


## 資源制限と保存量

通常のCLI実行は、App Server・照合probe・検証コマンドに[resource-limits.json](resource-limits.json)を適用します。
既定はCPU帯域100%／scope CPU予算60秒、memory 2 GiB、swapなし、512 TID、1ファイル64 MiBです。
systemd user scopeの実効値を確認してからnamespace子を起動し、設定不能なら拒否します。制限を無効にするCLIはありません。
daemon／workerにも[scheduler-config.json](scheduler-config.json)の別scope制限を加えます。ユーザーに委譲済みのcontrollerのみを使い、host設定・Codex設定を変更しません。

runtime保存量2 GiB／filesystem空き1 GiBの監視停止と通常JSONL 32 MiB上限も適用します。
**runtime合計は監視予算であり厳密なfilesystem quotaではありません**。高速書込みの超過余地があります。
厳密な総容量を保証する運用は、専用filesystem／quotaの設計・承認・検証まで開始しないでください。
制限超過はFAILED、dispatch後の監督・解放が不明ならUNKNOWNとし、成功扱いしません。

```bash
python3 -B src/runner.py --runtime .runtime/"$STATE_DIR" usage
```

保存量・分類・未解決taskをJSONで表示します。自動削除は実装していません。
UNKNOWNなどの証拠は保持し、容量不足では新しい実行を止めます。
詳細は[資源制限](docs/resource-limits.md)と[保持方針](docs/retention-policy.md)を参照してください。

## ローカルGit管理

このディレクトリだけを独立したGitリポジトリとして扱い、HOME／~/.codexはGit管理しません。
.runtime、SQLite、ログ、認証・秘密情報、cache、backupを除外します。
登録予定の**index上のblob**を検査するコマンド:

```bash
python3 -B scripts/git_audit.py
```

検査はパス、symlink／binary、大きすぎるblob、既知の秘密形式を対象とし、検出した値を表示しません。
機密性の万能な判定ではないため、差分・用途も人間／agentがレビューします。
既存のGit署名設定を尊重してローカルcommitを作成します。remote作成・GitHub作成・push・公開は行いません。


## テストと設計書

```bash
python3 -m unittest discover -s tests -v
# 実Codexを使用し、サブスクリプション利用枠を消費する
python3 tests/live_phase2.py --execute
# Phase 2-C: 8回のモデルturn、新しい専用runtimeを作成する
python3 -B tests/live_phase2_c.py
# Phase 2-D1: 実Codex 2並列のcoordinator SIGKILL＋別の正常タスク（3 turn）
python3 -B tests/live_phase2_d1.py
# Phase 2-D2: daemon経由の正常・2並列・中断・再起動（3 turn）
python3 -B tests/live_phase2_d2.py
```

オフライン試験は模擬App Server、実OSプロセス、SQLite、Linux namespaceを使います。
実機試験は新しい専用runtimeで正常検証・保存再開・期限・中断・親SIGKILLを確認します。
Phase 2-D2の123件を維持し、版確認の隔離とMCPの試験を追加しています。最新の件数・結果は[Phase 2-E1報告](docs/phase2-e1-report.md)を参照してください。

## ローカルMCP stdioアダプター

既存の専用daemon runtimeを指定します。アダプターはdaemonを起動せず、HTTP listenerも作りません。

```bash
cd "$PROJECT_ROOT"
# 別端末で既存の前景daemonを手動起動する場合
python3 -B src/runner.py --runtime .runtime/mcp-demo daemon
# MCPクライアントが次のコマンドを子プロセスとして起動
python3 -B src/mcp_adapter.py --runtime .runtime/mcp-demo
```

MCP 2025-11-25ではinitialize／notifications/initialized後にtools/list／tools/callを扱います。
2026-07-28の要求単位のversion／clientCapabilities metadataとserver/discoverにも対応します。
新方式の要求はlegacyの初期化状態を変更せず、各要求でmetadataを検証します。
既定はorchestrator_status／orchestrator_list_tasks／orchestrator_get_taskの読み取り3件です。
返すのは状態・ID・検証metadataで、タスク本文・自由文結果・ログ・設定は出しません。

書込みのorchestrator_submit_task／orchestrator_cancel_taskは、ローカル試験でのみ`--allow-writes`を指定して使います。
runtime/mcp-task-policy.json（0600）でoperatorがworkspace／acceptance／timeoutを固定し、clientはprompt／request_keyだけを渡します。
ポリシー例、範囲、監査ログ、期限、将来のTunnel導入案は[接続設計](docs/chatgpt-connection-design.md)を参照してください。
ChatGPTからの書込みは別承認まで有効にしません。
端末から独立したPoC daemon＋読み取りTunnelの手動管理は[poc-services](docs/poc-services.md)を参照してください。
2026-10-29に承認済みの2 transient user unitを起動し、実コネクターのstatus/list/getを確認しました。ログイン時自動起動やlingerは変更していません。
E2では利用者端末の実Tunnel接続・readinessを確認しました。
E3では実tunnel-client v0.0.18＋模擬control plane＋実adapterのMCP転送を確認しています。
現在の実PoCサービスではTunnel readinessと実コネクター往復を確認済みです。ChatGPT画面での回答表示は別途確認が必要です。

```bash
# 模擬MCP client＋専用daemon＋模擬App Server。モデル生成なし
python3 -B -m unittest discover -s tests -p test_phase2_e1.py -v
# 要求単位のdiscovery、権限境界、実tunnel-client＋模擬control plane（モデル生成なし）
python3 -B -m unittest discover -s tests -p test_mcp_discovery.py -v
```

- [全体設計](docs/architecture.md)
- [復旧設計](docs/recovery-design.md)
- [完了判定設計](docs/verification-design.md)
- [Phase 2-A/B報告](docs/phase2-ab-report.md)
- 過去の[Phase 1比較](docs/runtime-comparison.md)・[Phase 1報告](docs/phase1-report.md)

- [Workspace隔離](docs/workspace-isolation.md)
- [並列・DB・復旧設計](docs/concurrency-design.md)
- [Phase 2-C報告](docs/phase2-c-report.md)

- [Phase 2-D1報告](docs/phase2-d1-report.md)

- [Phase 2-D2報告](docs/phase2-d2-report.md)
- [ChatGPT接続設計・認証／課金の未確認条件](docs/chatgpt-connection-design.md)
- [Phase 2-E1報告](docs/phase2-e1-report.md)


## systemd user serviceの導入案（Phase 2-E3、未導入）

- [daemon unit](systemd/agent-orchestrator.service)
- [読み取り専用Tunnel unit](systemd/aceis-mcp-tunnel.service)
- [秘密値なしのservice用profile](systemd/aceis-mcp-tunnel.yaml)
- [導入・credential・停止・復旧・ChatGPT接続手順](docs/systemd-deployment.md)
- [E3実施報告と現在のAppArmorブロッカー](docs/phase2-e3-report.md)

```bash
systemd-analyze --user verify systemd/agent-orchestrator.service systemd/aceis-mcp-tunnel.service
# 実systemd user transient fixture、synthetic credential、loopback模擬control plane
# 現ホストでは実行中タスクの停止・復旧2試験がnamespace拒否で失敗する（skipしない）
python3 -B -m unittest discover -s tests -p test_phase2_e3.py -v
```

実サービスの手動導入はStage A全成功後の別承認を必要とします。
自動起動は有効化しません。unitにInstall sectionを設けていません。


## 開発専用PoCモード（Phase 2-E3.2）

systemd配下での実Codex 1 turn・コード生成・3テスト・独立検証・daemon再起動後のDB保持を確認しました。
[実施報告・制限・実Tunnelの手順案](docs/phase2-e3-2-report.md)を参照してください。
通常の隔離モードは既定のままで、E3の既知2失敗も未解消です。

PoCは明示した`--development-poc`だけで有効にし、空の`.runtime/development-poc-NAME`にmode宣言を保存します。
既存runtimeの変換や、flagを省いた実行は拒否します。名前付き専用fixtureのimplementerだけを対象とし、resume/reviewerは提供しません。
既存bwrapの一段構成とCodexのexternalSandboxを使い、元のunshare bootstrapや内部sandboxへのfallbackはありません。
これは本番用ではなく、信頼する小さなfixtureで動作確認するためのモードです。

```bash
# CODEX_BINARYには調査済みのinstalled binary絶対パスを指定する
python3 -B src/runner.py --runtime .runtime/development-poc-demo \
  --codex "$CODEX_BINARY" --development-poc daemon
# 同じ専用runtimeに接続。adapterはdaemonを自動起動せず、読み取り3ツールだけ
python3 -B src/mcp_adapter.py --runtime .runtime/development-poc-demo
# 再現用: 新runtimeのtransient user unitで実Codex 1 turnを消費する
python3 -B tests/live_phase2_e3_2.py --execute
```

実運用unitのinstallやenableは行いません。実Tunnelの書込み公開には進みません。
実client＋模擬control planeに加え、承認後に実OpenAI Tunnel経由の読み取りstatus/list/getを確認しました。


## dot ↔ Chat mailbox PoC（実装済み・既定OFF）

`src/chat_mailbox.py`は、既存runtime内だけで固定IDの要求と回答を受け渡す小さなmailboxです。ChatやCodexのtaskを起動する機能、URL/shell/任意pathの指定・ファイル読取り機能はありません。既存7toolの公開・稼働サービスは今回変更していません。

追加する4操作（`--allow-chat-mailbox`で明示した場合だけ）:

| 操作 | 用途 |
|---|---|
| `chat_mailbox_register_request` | dotが許可済みID・要求hash・正確な本文を登録 |
| `chat_mailbox_get_request` | 起動済みChatが同じID・要求hashで本文を取得 |
| `chat_mailbox_save_answer` | Chatが要求hashに対応する回答を保存。回答hashはサーバーが計算し、任意で渡したhashも照合 |
| `chat_mailbox_get_answer` | dotが回答を取得。応答喪失時も保存結果を照合。任意の期待回答hashと不一致なら拒否 |

operatorが既存専用runtimeの`chat-mailbox-policy.json`（UID所有・0600・非symlink）へ、許可するtask_id/request_id/request_sha256の組を固定します。例は[policy example](examples/chat-mailbox-policy.example.json)。exampleのUUIDは実会話のIDではなくdemoのslot名です。対応する固定質問は次の文字列です（改行を含めない）:

```text
Give a short non-secret recommendation for a reversible documentation change.
```

IDの組は最大32、要求・回答はそれぞれUTF-8で最大4096 bytes。DBは同runtimeの`chat-mailbox.sqlite`に固定し、SQLite transaction・FULL同期・サイズ上限1 MiBを使います。rollback journal等は別途作成され得るため、ディスク全体quotaの保証ではありません。既存の専用runtime以外を一般保存先として公開せず、`~/storage`の権限制限を別の保存先で回避する仕組みでもありません。policyの設定はadapter起動時のsnapshotで、変更は意図的な再確認が必要です。

同じ要求・回答の同一内容再送は冪等です。要求本文はpin/hashと照合して不変、回答は最初の1件だけを保存し、別内容への上書きを拒否します。保存途中や応答喪失でUNKNOWNになった場合は自動再送せず、get_request/get_answerでID・要求hash・既存本文または回答hashを照合します。NOT_FOUND/PENDING/ANSWEREDと競合を区別します。未確認のまま別request_idを作ってやり直しません。

**ID/hashはChat会話のidentity・回答者・内容の正しさを証明しません。** dot/Chatという呼び分けは運用上の役割で、このPoCは同じ許可scopeを持つclientを本人認証で区別しません。返却値もidentity_verified=falseです。MCPの許可対象を限定し、key・token・個人情報・機密source等は本文に入れません。既存監査ログには本文や回答を記録せず、ID/呼出しmetadataだけを残します。本文はmailboxのprivate DBに残り、allowlistの範囲でMCPへ返されます。任意の機密を安全に保存するvaultではありません。

### 起動経路の正確な残件

確認済み経路はdot → ローカルCodexの正規send_message_to_thread → Chatへの投稿と回答生成です。read_threadによる回答本文の回収はできていません。親dotは同じsend/read手段を持たず、mailboxを導入しても休止したChatを起動できません。

mailboxを使う場合も、起動済みChatに「既存connectorのget_requestで要求を読み、save_answerで保存する」と伝える正規経路が必要です。新toolsへChat側が実際にアクセスできるか、write呼出しの承認が通るか、dotがget_answerで回収できるかは未検証です。Chatのtool操作が使えない経路へ画面/別API等で迂回する実装はしていません。自律連携の完成とは扱いません。

### 一度に確認する有効化案（今回は未実施）

1. 非機密の質問本文、使用するtask/request IDとhash、対象の既存runtimeを具体的に固定し、0600 policyを置くことを確認する。DB以外の任意ファイルを扱う権限は付けない。
2. 新しい4tool（うち要求登録・回答保存の2write）の公開と持続scope拡大、dot/Chat双方のアクセス対象をaction-timeで確認する。同じ既存credentialを使い、新規key/従量課金APIは追加しない。
3. 仕事0件を確認したうえで専用2サービスを既存手順で再起動し、既存flagに`--allow-chat-mailbox`だけを追加する。一時MCP切断を確認し、既存7toolに4toolを足した11toolをdiscoveryで照合する。submit/cancelは有効化しない。現在のコード適用だけでは登録・再起動しない。
4. 既に承認された正規経路でChatを1回起動し、固定非機密質問の取得→回答保存→dotの回収を確認する。UNKNOWNは再送せず保存状態を照合する。Chat起動経路自体が使えなければその部分はBLOCKEDとして止める。

CLIの新flagはMCP adapterと`poc_services.py`の双方にありますが、実サービスでの有効化・登録・restartは別確認まで保留です。通常テストはmock/専用fixtureだけで行い、モデル・namespace・現用mailboxへ送信しません。

### サービス停止前のpreflight

既存起動時の`--key-file`明示指定とlocal-llm/local-models両flagを引き継ぐ。CLIの既定キー参照が現用参照と同じとは限らない。正規の非機密起動記録にある既存credentialのpathを`CREDENTIAL_FILE`に設定し、値を読まずに次を先に実行する（`<EXISTING_CREDENTIAL_FILE>`は実在する既存参照へ置き換える）。

```bash
CREDENTIAL_FILE="<EXISTING_CREDENTIAL_FILE>"
python3 -B scripts/poc_services.py preflight --key-file "$CREDENTIAL_FILE" --expected-key-file "$CREDENTIAL_FILE" --allow-local-llm --allow-local-models --allow-chat-mailbox
```

preflightは既存stateとの参照・flag継承、キーmetadata、binary/helper/profile、未完了仕事、policyと生成引数を検査する。stop/start、mailbox DB作成、キー内容の読取りは行わない。passedと固定policy/hash・期待11toolsを照合し、READY/所有権と稼働・復旧待ち0も別途確認してから、承認済み停止・再起動へ進む。startも同じ参照と3flagを明示する。新しいstateにはキー/codexのファイル参照を保存し、次の起動で参照や既存capability flagの継承漏れを拒否する。秘密の値はstateへ保存しない。rollback時は同じ既存参照と旧2flagを維持し、mailbox flagだけを外す。

## 日常運用の追加整備

保存済み参照からの`resume-plan`/`rollback-plan`、停止直前の未解決仕事検査、有限timeout・getter-only再接続を備えた`mailbox_ops.py`、入力拒否と保存状態の監査を追加した。完全な手順と確認範囲は[mailbox運用](docs/mailbox-operations.md)を参照。prepareは承認候補の生成だけで、現用allowlistを変更しない。新しいslotやChat送信は個別の承認範囲で扱う。

## 固定した共通正本とローカル参照

共通指示は[i-pp/ai-orchestration](https://github.com/i-pp/ai-orchestration)のcommit/hashで固定し、既存local checkoutのGit内容から読む。~と相対pathの解決、HEAD/dirtyの記録、旧v1.0の履歴保全と優先順位は[共通policy読込み](docs/common-policy.md)を参照。正本repoへのpull/resetや本文の重複管理はしない。

固定slot毎の追加を避ける[限定grant案](docs/mailbox-grants.md)もコード/fixture試験まで準備した。現用MCPには接続しておらず既定OFF。公開11操作・live固定1slotは変わらない。
