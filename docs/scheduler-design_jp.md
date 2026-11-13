# 永続スケジューラー設計 — Phase 2-D2

## 取得順序と実行権限

SQLiteのQUEUED taskを自動取得し、最大2 workerを実行する。
FIFOはtasksのSQLite挿入順（rowid）。createdは観測用時刻であり、時計の後退で取得順を変えない。
自動削除／VACUUM／外部DB編集で順序を再構成する運用は今回対象外。
順番の先頭がworkspace競合で取得できなければ、その後を勝手に追い越さない。
旧共有workspaceは1件だけ、独立workspaceは最大2件。同じworkspace／threadの同時resumeを禁止する。

daemonはdaemon.lockをexclusiveに保持する。通常run／run-pairはshared取得が必要なのでdaemon実行中は拒否する。
submitやshowは実行の取得と別操作。Socket以外で保存されたQUEUEDも対象とする。
daemon開始前から通常runnerが動いていれば、daemonの所有権取得を拒否し、そのprocessを停止しない。

取得手順:

1. recover／offline reconcile、容量、pause、既存owner、workspace境界を確認する。
2. BEGIN IMMEDIATEでQUEUEDを再確認し、timeoutと一意なdispatch予約を保存する。
3. 専用資源scopeにworkerを起動し、PID/startを保存してSTARTEDへ進める。
4. workerは有限時間だけhandoffを待つ。daemonのboot/PID/start、nonce、記録processからの祖先関係を検査する。
5. 条件付きUPDATEでnonceを一度だけ消費しAUTHORIZEDへ進む。
6. 既存runnerのtask/workspace/slot leaseとQUEUEDのCAS claim、executions.task_id UNIQUEで実行を一度に限定する。
7. 実行・独立検証・資源解放は既存runnerが行う。daemonはstdoutや終了コードだけでタスク成功を作らない。

予約を保存した後にworkerを起動するため、forkとDBの隙間も再実行で埋めない。
失われた予約はUNKNOWNへ移し、自動再dispatchしない。予約前の未実行QUEUEDは保持する。
nonceは内部の相関・一回限りhandoff用で、Unix peer認証の代わりではない。モデルには制御socket／DBを見せない。

## 永続化と互換性

既存task DBのuser_version=3、旧task/execution/thread/turn/workspace/snapshotを維持する。
daemon初回に排他とSQLite backupを取り、追加表だけをtransactionで作成する。

| 表 | 記録 |
| --- | --- |
| scheduler_state | 拡張schema_version=1、pause、更新時刻 |
| scheduler_options | task_idと取得時に固定したtimeout |
| scheduler_dispatches | task_id UNIQUE、nonce、daemon owner、phase、processes、worker owner、scope記述、cleanup、時刻 |

pauseは再起動後も保持する。shutdown自体はpause値を書き換えず、次の明示起動では保存済みpauseに従う。
timeoutはSocket submitと同じtransaction、旧方式投入の場合はdispatch予約と同じtransactionで固定する。
request_keyの再送は同一taskを返し、異なる条件／timeoutの同じkeyは拒否する。

追加schema作成前backupはdb-backups/before-scheduler-v1-UUID.sqlite。
途中失敗はtransaction rollbackする。既存行を削除・初期化せず、未認識schemaを推測で変換しない。
既存db-rollbackはdaemon leaseとscheduler表の存在を確認し、永続キュー記録を捨てる復元を拒否する。
コードを戻す場合も先にdaemonとworkerを停止し、recover／backupと対応ソースを確認する。
古いD1コードのdb-rollbackをD2のDBに対して実施しない。表を自動DROPする手順は用意しない。

## 回復とUNKNOWN

起動時とworker終了時に既存のrecover_abandonedを使い、不在ownerのRUNNING／VERIFYINGをUNKNOWNへ移す。
保存したdispatch予約、worker PID/start/bootとcgroup、Codex所有process／scope、workspace manifest、ログを照合する。
workerがまだ停止していなければ、その間はrecovery_pendingとして新規dispatchを止める。
所有daemonが死んだ後の起動前隙間には、事前に記録した専用cgroupパスも使う。推測の成功／失敗は作らない。
解放を証明できない予約はUNKNOWNのまま全体を止め、人間の調査対象とする。

UNKNOWNは新規投入扱いに戻さない。WAITING_FOR_HUMAN／FAILEDも自動再実行しない。
会話再開は明示的な新taskと既存のallow_resume判断を必要とし、処理途中の復元とは区別する。
現在executionと一致する明示resume許可がある場合だけ、そのUNKNOWN workspaceを新taskが使用できる。
runnerは直前にも公式履歴・資源・副作用・許可の鮮度を照合する。

| 条件 | 新規dispatchへの影響 |
| --- | --- |
| worker／Codex資源の解放不明、失われたhandoff | 全体を停止 |
| 旧共有workspaceのUNKNOWN | 全体を停止 |
| UNKNOWNのworkspace検証／offline照合ができない | 全体を停止 |
| UNKNOWNの資源解放・照合済み、独立workspace | そのworkspaceを保護。他の独立workspaceは可能 |
| 同一workspace／threadが使用中 | FIFO先頭で待機 |
| 現在executionに対応した明示resume許可 | runnerの再照合を条件に新taskとして実行可能 |
| 容量・空き予算超過 | 新規dispatch停止。制御socketは維持 |
| SQLite競合 | 期限付きで延期／エラー。再取得や成功推測をしない |

Task DBのCOMPLETED直後もworker監督が完了していなければ、Socket statusのworker_pending=true、completion_verified=falseとなる。
worker資源超過／監督喪失が後から判明した完了checkpointはUNKNOWNへ戻し、既存の検証根拠を保持する。
scheduler_phase、scheduler_timeout、core成否・受入検証・人間判断を分けて監視する。
phase=FINISHED／ORPHANEDはworker journalの状態であり、タスク成功を意味しない。

## 停止・保全

pauseは新規取得だけを止める。cancelは指定taskだけ。shutdownはdaemonが取得したtaskだけを中断する。
停止時の未取得QUEUED、UNKNOWN、受入根拠、ログ、snapshot、backupを保存する。
強制解放まで進んだ場合もcleanupとその理由を記録し、未確定タスクを成功にしない。
同時数の既存slot leaseとcore UNIQUEは残し、スケジューラーだけを信用する設計にしない。

systemd user serviceのRestart設計は[daemon設計](daemon-design.md)を参照。
今回の実測は前景daemonの再起動であり、unitをinstall/startしていないためsystemdの自動Restartそのものは未検証。
日単位のsoak、高負荷、厳密disk quota、OS再起動・停電、他OS／CLI版は未検証。
