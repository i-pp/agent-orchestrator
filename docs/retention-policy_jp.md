# 保持期間と容量管理 — Phase 2-D1

パスは[表記規約](path-conventions.md)の疑似パスです。シェル例は同ページの変数を設定してから使用します。原本ログは変更していません。

## 現在の動作

自動削除・prune・圧縮・ローテーションは実装しない。
workspaceのreleaseも再利用を停止するだけで、ファイルを削除しない。
資源上限に達した場合は新しい実行を拒否／実行を停止し、証拠を消して空きを作らない。

```bash
cd "$PROJECT_ROOT"
python3 -B src/runner.py --runtime .runtime/<runtime> usage
```

totalとlogs／snapshot／workspace／DB backup／resource-events等の分類ごとに、見かけのbyte数、割当byte数、ファイル数をJSONで取得する。
lstatとmountinfoだけを使い、認証やCodex履歴の内容を読まない。symlinkや内部mountを追跡せず、測定が不完全なら問題として返す。
DBはランナー自身のtasks.sqliteだけを参照し、UNKNOWN／WAITING_FOR_HUMAN／QUEUED／RUNNING／VERIFYINGの保護対象を列挙する。
既存と同じ起動時照合は行うため、不在ownerの状態がUNKNOWNへ更新される場合がある。inventory処理自体は削除・移動しない。

## 将来の方針案（まだ自動適用しない）

| 対象 | 提案期間 | 整理の前提 |
| --- | --- | --- |
| 通常完了ログ・resource receipts | 完了後30日以上 | 結果／検証／判断と関連IDを退避・確認 |
| snapshot | 最終参照の終了後30日以上 | 未完了Reviewer、再開判断、未解決sourceから参照なし |
| workspace／専用Codex home | 終了・release後30日以上 | 全派生task終了、再開不要、資源解放済み |
| DB migration backup | 90日以上・最新の有効世代を保持 | schemaと対応するソースを記録、復元可否を確認 |
| UNKNOWN・人間判断待ち・未解決障害 | 解決するまで無期限 | 期間だけを理由に整理しない |

runtime全体2 GiB、filesystem空き1 GiBという現在の予算は停止境界であり、自動整理の許可ではない。
他runtimeと同じfilesystemを使うため、host全体の容量保証でもない。snapshot複製分・backup分も保存予算に含む。
将来は容量警告、保護参照の依存閉包、保存先の空き容量、整理候補のdry-runを定義してから自動化を検討する。

## 人間による整理手順案

1. 対象runtimeのworkerを止め、recover／reconcileで資源と未解決状態を確認する。既存Codex daemonを停止しない。
2. usageとshowを保存し、UNKNOWN／判断待ち／進行中のtask・execution・workspace・snapshot・resource receiptを保護する。
3. resume_of、snapshot.source_task_id／source_execution_id、Reviewerのsnapshot_idをたどり、依存する証拠も保護する。
4. SQLite backup APIでランナーDBを退避し、必要な成果物・ログ・設定・ソースcommitを対応付ける。認証ファイルはコピーしない。
5. 秘密情報を除去した整理計画と対象一覧をレビューする。認証やCodex内部DBを個別編集して整合性を作らない。
6. 別途許可された手動整理だけを実施し、操作・理由・復元先を記録する。今回この削除手順は実装・実行していない。

tmp/jailは通常空mountpoint、resource scopeはprocess終了に伴うkernel資源であり、保持する成果物の削除と区別する。
テストのTemporaryDirectoryは専用fixtureの終了処理で、実運用runtimeの保持処理ではない。
容量・復旧確認は任意の機密内容の自動判定を保証しない。外部保管・公開は別途承認・秘密情報確認を必要とする。

## Phase 2-D2の追加証拠

scheduler_state／options／dispatches、daemon-<boot>.jsonl、daemon／workerのresource receiptも保持対象とする。
失われた予約やUNKNOWNではworker scopeとtaskの両方を照合する。予約・nonceを消してtaskを再取得しない。
自動prune／table削除は追加しない。新規dispatchは保存予算で停止し、既存証拠を残す。
