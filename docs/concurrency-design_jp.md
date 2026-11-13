# 並列実行・永続化・復旧 — Phase 2-C

## 前景実行と上限

1 taskは1 worker、1 workerは同時に1つの専用App Server stdioを使う。接続・JSONL・監視・解放は共有しない。
`run-pair`は2件を起動して待つだけで、daemon・バックグラウンドschedulerではない。
別々に起動した`run`にも同じlease制御を適用する。
上限は**同じruntimeの2セッション**。別runtimeや外部から直接起動したCodexの数は制御しない。

| lease | isolated | 旧共有workspaceモード |
| --- | --- | --- |
| runner.lock | shared | exclusive |
| task UUID | exclusive | 全体lockで直列 |
| workspace UUID | exclusive | 全体lockで直列 |
| slot-0 / slot-1 | 空いている1つ | 全体exclusiveにより他実行を排除 |

flockはnonblocking。競合時にモデルを起動せず、タスクはQUEUEDのまま返す。自動再試行しない。
順序はadmission→task→workspace→slotで固定し、FDはclose-on-exec。worker死亡でleaseを解放する。
resumeは同じworkspace leaseを使うため、同一threadの同時resumeも拒否する。
履歴照合probeを終了してから実行serverを開き、一時的にserverを二重にしない。

## ID・イベント・結果

task、execution、thread、turnに加え、workspaceとsnapshotを独立IDとして保存する。
各実行ログにtask/execution/workspace/role/snapshotを付け、thread確定後はthreadも付ける。
通知のthreadと結果を生んだturnを検査し、他セッションの結果は使用しない。
resume時に届く既知の過去turnの利用量通知だけは、historical_usage_ignoredとして記録し、現在turnのusageへ混ぜない。
未知turnや別thread、別turnのitem/completedは従来どおりProtocolErrorとする。

一方の失敗・タイムアウト・cancelは自身のProcess群だけを解放する。
個別cancel、runtime全体のcancel-all、run-pair自身への停止要求を区別する。
cancel-allはBEGIN IMMEDIATE内で対象集合を確定し、QUEUEDをCANCELLED、実行中をcancel_requestedへ更新する。
run-pairのSIGINT/SIGTERMはその2件のみへ要求を送る。SIGKILLでは親死亡guardがworkerとその子へ伝播する。
副作用の巻戻しやキャンセル時点以前の書込み消失は保証しない。

## SQLiteと移行

標準sqlite3、接続ごとのforeign_keys=ON、synchronous=FULL、timeout=5秒を維持する。
WAL等の新しい運用設定は追加しない。短いトランザクションとCAS／一意制約で同時更新を守る。
claimはQUEUEDを条件とするUPDATEとexecution挿入を一体化する。executions.task_idのUNIQUEも維持する。
request_keyの同時投入はBEGIN IMMEDIATEで直列化し、同一入力なら同じtask/workspaceを返す。

schema v3の追加:

| 表 | 追加・内容 |
| --- | --- |
| tasks | workspace_id、role、access、snapshot_id、workspace_request |
| executions | workspace_id、role、access、snapshot_id |
| workspaces | id、owner_task_id、access、state、snapshot_id、initial_fingerprint、created、released |
| snapshots | id、source_task_id、source_execution_id、workspace_id、fingerprint、created |

旧行のworkspace_id/snapshot_idはNULL、role=implementer、access=read-only。旧workspaceと保存履歴の場所は変えない。
旧COMPLETEDや受入根拠・判断・IDを保持する。新しい成果物の成功を推測して追加しない。
v0は既存v2移行後にv3へ、v2は追加列・表のみのトランザクションでv3へ移す。
移行前にruntime排他とSQLite backupを取り、foreign_key_checkしてcommitする。途中失敗はschemaを含めrollbackする。
v2の独自列が追加名と競合する場合はエラーで止め、既存列を上書きしない。

```bash
# 全worker／クライアントを止め、バックアップ名を確認して実行
python3 src/runner.py --runtime .runtime/<runtime> db-rollback <phase2-ab-backup.sqlite>
```

v3→v2はworkspace/snapshotレコードがなく、旧全表の旧列・全行がbackupと一致する場合だけ許可する。
現DBも退避する。変更を捨てる強制復元はない。v0への既存rollbackも保持する。
新コードで開くと再移行するので、旧コードへ戻す場合はコード退避もレビューして戻す。
今回の開始時ソース退避は`.runtime/phase2-c-evidence/phase2-ab-source-backup.tar.gz`。
workspace利用後は過去DBへ戻さず、現DBの保全と前進修正を選ぶ。

## 復旧と受入検証

起動時はRUNNING/VERIFYINGを列挙し、タスクとworkspaceのleaseを取得できたものだけUNKNOWNにする。
生きている他workerのRUNNINGを変更しない。UNKNOWNを起動しない。PID/start/bootと起動checkpointの既存照合を保持する。
照合・明示判断・snapshot・releaseもworkspace leaseを取るため、実行中の別resumeと競合しない。
reconcileは対象workspaceのmanifestとそのCODEX_HOME内の公式thread/read・thread/turns/listだけを確認する。
Codex内部DB、既存daemon、IPCは直接操作しない。

再開許可と新task投入は別操作。最新turn、資源、副作用、許可fingerprintを再照合する。
workspace-writeで変更があれば、従来同様に副作用の明示確認が必要。
受入検証は同じ所有workspaceへ限定し、Codex実行成功とタスク成功を分けたまま維持する。
Reviewerの検証はsnapshotの独立コピーを使い、sourceの判断は変更しない。

## 実測と未検証

2 worker、3件目拒否、同一task排他、request_key同時投入、片方だけの停止、親／coordinator SIGKILLを専用fixtureで検証する。
実Codexでは2つのworkspace-write、独立review、同一session resume、片方のtimeout／cancelと他方の完了を検証した。
詳細・初回失敗・利用量は[実施報告](phase2-c-report.md)を参照。
高負荷・多ホスト・共有ネットワークFS・OS再起動・停電は未検証。2を超える並列、daemon、外部接続は実装対象外。

## Phase 2-D1の追加

Codex process tree・probe・verifierは専用user scopeで制限する。モデルsession数の上限2はそのまま。
coordinatorはworkerへ実行ファイル探索用PATHとLANGだけを渡し、正規バイナリの検査を維持する。
実Codexのcoordinator SIGKILLを専用fixtureで確認した。親死亡とlease解放が完了するまでlive ownerを尊重し、その後recoverでUNKNOWNへ移す。
詳細は[資源制限](resource-limits.md)と[D1報告](phase2-d1-report.md)を参照。
