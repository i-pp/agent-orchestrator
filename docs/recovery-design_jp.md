# 障害復旧・照合設計

「何が起きたかの確認」と「次の処理の実行」を分離し、UNKNOWNを勝手に実行しないことを優先する。

## IDと記録

| ID | 意味 |
| --- | --- |
| task_id | 目的、プロンプト、受入条件 |
| execution_id | 一度取得した実行。task_idとは別UUID |
| thread_id/session_id | Codex保存会話。従来のtask.session_idも保持 |
| turn_id | 会話内の1回の生成 |
| reconciliation_id | 特定時点の照合根拠 |
| decision_id | actorと理由付きの判断 |

executions.task_idは一意で、同じtaskへの二度目の実行をDBでも拒否する。
再開は新しいtask/executionで行い、元の実行を復元・反復しない。clientUserMessageIdにもexecution_idを指定する。

Phase 2-A/Bのschemaはv2。現在はPRAGMA user_version=3で、workspace/role/access/snapshotを追加する。
追加のlease・v2移行・rollbackは[並列設計](concurrency-design.md)を参照。

- tasks: 旧17列を保持し、acceptance、active_execution_id、codex_status、verification_status、human_status、resume_approvalを追加。
- executions: ownerのboot/PID/start、processes、launch_pending、送信意図、thread/turn、Codex結果、実行前manifest、cleanup、時刻。
- transitions: 状態変更、理由、execution_id。
- reconciliations: 照合facts。
- verifications: 判定・根拠・workspace fingerprint。
- decisions: 判断種別、actor、理由、参照した照合。

旧COMPLETEDは履歴を改変せず保持し、LEGACY_UNVERIFIEDとする。completion_verifiedはfalseであり、新しい検証済み成功に数えない。

## 起動・異常停止・解放

ロックを取得できない間はライブownerの実行を復旧しない。取得した新しいrunnerは残ったRUNNING/VERIFYINGをUNKNOWNへ移すだけで、自動投入しない。
VERIFYINGで保存済みのcodex_status=SUCCEEDEDは保持する。送信中・実行中だけをUNKNOWNへ変更する。

起動直前にlaunch_pending、起動後にPID/startを保存する。起動と記録の隙間は「解放済み」と推測しない。
boot IDとPID/startで再利用PIDを区別する。古いcleanupがあっても、その後の検証プロセスが生きていれば解放済みではない。
parent-death guard、プロセスグループ、unshare kill-child、PID namespaceを使う。
中断・期限ではまずturn/interruptを送り、上限付きで解放する。稼働中残存や未確認checkpointがあればUNKNOWN。
これらは副作用の巻戻しではない。

## 照合

recoverは不確実性の記録だけ。reconcileは次をreconciliation_idに保存する。

- 所有PID/start/bootと稼働中残存。
- workspaceの通常ファイルmanifestと実行前との差。
- ランナーJSONLの完全行、破損、末尾切断。
- 専用stdioから公式thread/read、thread/turns/listで得た対象turn・最新turn・セッション状態。

照合でthread/resumeやturn/startは呼ばない。Codex内部DB・履歴を直接読書きしない。
履歴は最大10ページ・各20turn。見つからないものを推測で補わない。
APIは[公式App Server](https://learn.chatgpt.com/docs/app-server)とインストール済みバイナリの生成schemaを照合した。

manifestは通常ファイルのサイズとSHA-256だけ。内容は保存しない。
上限は通常ファイル1,000件・各16 MiB・合計約64 MiB・ディレクトリ1,000件。上限、symlink、読取不能は不明扱い。
UNCHANGEDはこのmanifestの一致のみで、外部・一時的・ネットワーク等の副作用がなかった証明ではない。

## 明示判断

| 操作 | 条件・効果 |
| --- | --- |
| resolve verify | UNKNOWN/判断待ちの保存turnに正常結果があり、解放確認済み。Codex生成なしで受入検証する |
| resolve fail | UNKNOWN/判断待ちを理由付きで失敗として閉じる |
| resolve allow_resume | 終了済み・最新turn一致・解放確認済み。変更や不明な副作用には明示ackが必要。投入しない |
| submit --resume-of | 明示的な新規投入。UNKNOWN/判断待ちは許可decision_idが必須 |
| run | 新しいtaskを一度だけ実行し、resume直前にも照合する |

再開許可は一度だけ使用でき、workspace変更で失効する。履歴が進んでいれば再開を拒否する。
会話resumeは最新会話への新入力であり、途中のコマンド復元ではない。
旧データで記録が不足すれば不足を明示し、自動承認しない。

## 移行とロールバック

認識する旧schemaはPhase 1のtasks/transitions。独自index/trigger/viewや未認識版は推測で変更せず拒否する。
排他取得後、SQLite backupでdb-backups/phase1-UUID.sqliteへ退避する。
外部キー設定をトランザクション外で切替え、別表へコピー、旧表削除、名称変更、追加表作成を1トランザクションで行う。foreign_key_check後にcommitし、途中失敗はrollbackする。
[SQLite公式のschema変更手順](https://www.sqlite.org/lang_altertable.html)、[Python sqlite3のbackup・トランザクション](https://docs.python.org/3.14/library/sqlite3.html)を参照した。

```bash
python3 src/runner.py --runtime .runtime/<対象runtime> db-rollback <phase1-backup-filename.sqlite>
```

全クライアントを止めて実施する。コマンドは排他取得し、Phase 2の実行・照合・検証・判断がないことと、旧tasks列・transitionsがbackupに一致することを確認する。
新規タスクを含む変更があれば拒否し、データを捨てて戻すオプションは用意しない。
現DBも別退避し、SQLite backupで復元、integrity_checkを確認する。強制ファイル上書き・初期化はしない。

復元後に新コードへ接続すれば再移行される。旧コードへ戻す場合は、退避したPhase 1ソースもレビューして復元する。
今回のソース退避は.runtime/phase2-evidence/phase1-source-backup.tar.gz。作業ツリーへの自動展開はしない。
実Phase 1データ12件のコピーと専用fixtureで、行・外部キー・復元を検証した。元DBは移行試験で変更していない。

## 限界

模擬server異常終了、親SIGKILL（RUNNING/VERIFYING）、JSON末尾切断、起動checkpoint、二重実行、移行途中失敗を試験する。
実Codexでも親SIGKILL後のUNKNOWN、公式履歴、解放を確認した。
OS再起動・停電、ディスク故障、実ネットワーク断、任意の外部副作用の復元は未検証。

## Phase 2-Cでの適用

照合・判断・再開検証はtask.workspace_idから所有workspaceを選択する。別workspaceの実行に復旧を波及させない。
起動時は各task/workspace leaseを確認し、生きたworkerはそのまま、不在のRUNNING/VERIFYINGだけUNKNOWNへ移す。
resumeの照合probeも現在executionのプロセス記録に含め、probe終了後に実行serverを開く。
release済みや境界検証に失敗するworkspaceはアクセスを拒否し、人間の調査対象とする。

## Phase 2-D1のscope照合

execution.owner.resource_scopesに記録した自分のreceiptとcgroupの不在／populated=0も解放根拠へ加える。
receiptはresource-events配下の正規UUID unitだけを対象とし、既存Codex DB・既存daemon・任意のunitを操作しない。
親死亡時のREADYだけで正常終了を推測しない。UNKNOWNは自動再実行せず、証拠は保持する。

## Phase 2-D2の永続dispatch

起動時はworkerの一回限り予約も照合し、未取得QUEUEDと不明なdispatchを区別する。
追加schemaがあるDBを古いbackupへ戻してキュー記録を捨てる操作は拒否する。
詳細は[scheduler設計](scheduler-design.md)を参照。
