# Phase 2-D2 実施報告

パスは[表記規約](path-conventions.md)の疑似パスです。シェル例は同ページの変数を設定してから使用します。原本ログは変更していません。

実施日: 2026-10-28（Asia/Tokyo）。対象は`<PROJECT_ROOT>/`。
本書はAIによる実測の要約であり、試験原出力・除去済みログ・集計JSONと区別する。

## 結果

既存runnerを使う前景daemon、永続FIFO、Unix socket制御、daemon／workerの資源制限を実装した。
123件の自動テストが成功した（既存101件＋新規22件）。既存assertの削除・緩和、skipによる成功扱いはない。
実Codexでは3 turnで、daemon経由の正常完了、2並列、個別中断、daemon SIGKILL、再起動後のUNKNOWN照合・QUEUED保持を確認した。
更新後コードの再起動と既存CLIによるstatus／list／showも、追加生成なしで確認した。

**service定義は作成・構文検査だけで、インストール・enable・startしていない。**
実際のsystemd user serviceによる自動Restart、ログイン時起動は未検証。前景再起動試験で代替実測したと主張しない。
root、system service、linger変更、既存daemon／Codex IPC操作、外部公開、Phase 2-Eには着手していない。

## 開始時点とGit

開始commitは`2f404750f59b2d3cd645b29c2c97f8fa9f7beca9`。作業ツリーはclean、remoteなしだった。
既存独立リポジトリを使用し、HOMEや~/.codexをGit管理していない。
開始時49ファイルのhashとソース退避を記録した。
テスト後に登録予定indexのblob・パスを監査し、既存SSH署名設定でローカルcommitを作成する。
最終commit／署名結果は`.runtime/phase2-d2-evidence/commit-result.json`とGit履歴を参照する。
本ファイルを含むcommit自身のhashは循環参照を避け本文に埋め込まない。
remote登録、push、GitHub作成・公開は行わない。

## 実装

| ファイル | 内容 |
| --- | --- |
| src/daemon.py | 前景イベントループ、socket、worker監督、正常停止 |
| src/scheduler.py | FIFO、追加schema、一回限り予約、handoff、回復・保護条件 |
| src/control_socket.py | Unix socket、owner／mode／peer UID、有限JSONL client |
| scheduler-config.json | daemon／worker制限、poll、task期限、停止猶予 |
| systemd/agent-orchestrator.service | 将来の手動導入用user unit |
| tests/daemon_fixture.py | 実process／namespace／cgroupと模擬App Serverの隔離fixture |
| tests/test_phase2_d2.py | 新規22件 |
| tests/live_phase2_d2.py | 実Codexの明示的統合試験 |
| docs/daemon-design.md / scheduler-design.md | 境界、状態、導入・回復・未検証事項 |

既存runner CLIへdaemon／socket操作を追加し、実行の取得をdaemon所有権と分離した。
resources.pyの既定タスク制限を残し、daemonの保存量判定はschedulerのdispatch admissionで行う。
storage.pyはtimeoutの原子的保存と、永続キューを捨てるrollbackの拒否を追加した。
旧read-only namespaceとlegacy verifierでも/run/userを隠し、同じUIDのモデルから制御socketを見せない。
既存Phase 2-CテストのSQLite contextだけを正しくcloseする変更も行い、assertは保持した。
README、現行設計、保持方針、Git監査の許可パスと.gitignoreを更新した。過去の実施報告は保持した。

## キューと状態

SQLite挿入順でQUEUEDを取得し、最大2 workerを起動する。createdの時計後退でもFIFOを変えない。
旧共有workspaceは直列、独立workspaceは最大2。同じworkspace／threadは同時利用しない。
task／execution／thread／turn／workspace／snapshotの既存ID分離は維持した。

daemon.lock、dispatch journalのtask_id UNIQUE、一回限りhandoff、既存task/workspace/slot lease、core CAS claimを重ねる。
workerのPID/start/bootと祖先関係を確認するため、同じtokenを別processから再利用できない。
worker終了コードやstdoutを成功判定にしない。Codex結果・独立検証・人間判断は従来どおり分離する。
CoreのCOMPLETED checkpoint後もworker監督が未完了なら、socketのcompletion_verifiedはfalse。

core user_version=3は維持し、初回daemon起動にSQLite backup＋transactionで3表だけを追加する。
scheduler_stateの拡張version=1、pause、timeout、予約・process・scope・cleanupを永続化する。
timeoutとrequest_keyの条件はtask投入と同じtransactionで保存し、切断後の再送で二重投入しない。
既存記録を消すdb-rollbackは拒否する。詳細は[scheduler設計](scheduler-design.md)。

UNKNOWN／WAITING_FOR_HUMAN／FAILEDを自動再実行しない。
起動時は先にrecoverとoffline reconcileを行い、leaseと資源の解放待ちを新規dispatchへ進めない。
失われた予約はUNKNOWN、予約前の未実行QUEUEDは保持する。
未解放／未照合UNKNOWNは全体停止、解放・照合済みの独立UNKNOWNはworkspace単位で保護する。
明示resume許可は現在executionと照合し、実行直前にもrunnerが履歴・副作用を再確認する。

## 制御・資源・停止

Unix pathname socketは/run/user/<uid>/agent-orchestrator配下。親0700、socket0600、owner UIDとSO_PEERCREDを双方で検証する。
別runtimeは別socket hash。stale endpointは所有・type・mode・接続不能・inodeを確認したものだけを置換する。
submit／status／list／cancel／pause／resume_scheduler／shutdownを実装した。
既存CLIの--via-daemon submit/show/cancelと、status/list/pause/resume_scheduler/shutdownで操作できる。
不正・途中JSON、過大frame、切断、接続混雑は有限に処理する。送信後のsubmitを盲目的に再送しない。

daemonはCPU50%・512 MiB・64 TID・24 CPU時間／起動、workerはCPU100%・256 MiB・32 TID・60 CPU秒。
タスク本体／verifierのresource-limits.jsonは変更していない。設定不能時の無制限fallbackはない。
daemon／workerの実効cgroup値を試験で確認した。
総ディスク量は引き続き監視予算でhard quotaではない。容量超過ではdispatchを止め、制御socketを維持する。
自動削除／prune／snapshot整理は追加していない。厳密quotaには別途filesystem・権限・導入承認・実測が必要。

正常停止はdispatch停止→所有task cancel→runnerのinterrupt／記録／解放待ち→必要時だけ強制解放。
未取得QUEUEDは残し、中断結果が不明ならUNKNOWN。別runtimeや既存processを停止しない。
前景helperのSIGTERMも先にsocket shutdownを送り、scope supervisorの早期終了で記録を失わないようにした。

## テスト結果

```bash
python3 -B -m unittest discover -s tests -v
python3 -B -m unittest discover -s tests -p test_phase2_d2.py -v
# モデル生成を伴う専用runtimeでの検証
python3 -B tests/live_phase2_d2.py
systemd-analyze --user verify systemd/agent-orchestrator.service
```

最終自動テスト: **123件、120.025秒、成功**。元101件の名前とassert ASTを照合し、欠落・変更なし。
その後のCLIのEOF報告・ローカルsubmitの未対応timeout拒否は、対応する既存2テストを拡張して再実行し、2件とも成功した（1.038秒）。

| 試験 | 結果 |
| --- | --- |
| 手動daemon起動・socket制御 | 全7 method、CLI status/list/show、権限・peer UIDを確認 |
| FIFO／並列 | 時計後退fixtureでも取得順維持、最大2、独立thread、重複executionなし |
| pause／shutdown | pause永続、個別cancelは他方を維持、正常停止は未取得QUEUEDを保持 |
| daemon SIGKILL／再起動 | 所有workerの終了、UNKNOWN照合、QUEUED保持、自動再実行なし |
| worker SIGKILL | 対象UNKNOWN、他方継続、DB整合性保持 |
| 切断・不正frame・接続混雑 | daemon継続、切断したsubmitも同じkeyで1件 |
| SQLite競合 | lock timeoutを安全に報告、同じkeyの再投入で1件、重複dispatchなし |
| 起動前handoff喪失 | UNKNOWN・保護停止。未取得の別QUEUEDを保持 |
| 容量超過 | sparseな専用fault fixtureでdispatch停止、証拠のpruneなし |
| 制御socket境界 | legacy／isolated／verifier namespaceからsocketが見えない |
| resource／schema | daemon・workerの実効値、追加schema、破壊的rollback拒否、timeout原子性を確認 |
| 継続試験 | 同一daemonで24 task＋40接続切断、60秒監視、FD数の増加上限を検査 |

継続試験は日単位のsoakではない。模擬App Server＋実OS process／namespace／cgroupと、実Codexを区別する。
systemd-analyzeはexit 0。既存spice-vdagent.serviceの既存設定警告が出たが、当該unitは変更していない。
agent-orchestrator.serviceはLoadState=not-found／inactiveで、serviceを起動していない。

## 実Codexの実測

専用runtime: `.runtime/phase2-d2-live-be863b50/`。
3回のモデルturnで、normalとcancelを同時に開始した。normalは成果物hashと許可済みテストに合格してCOMPLETED。
もう一方は個別cancelでCANCELLED。その後、別taskの実コマンド開始を確認して専用daemon PIDだけをSIGKILLした。
pause後に投入した未取得taskを残し、同じruntimeを再起動した。

最終状態は**COMPLETED／CANCELLED／UNKNOWN／QUEUED**。executionは3件、dispatch順は投入順、integrity_check=ok、foreign_key_check=0。
UNKNOWNとQUEUEDを再実行していない。正常shutdownはexit 0。
後続の安全性修正は自動テストで確認し、最終コードの再起動・CLI確認は追加モデル生成0回で実測した。
旧read-only namespaceの/run/user遮蔽後も、実App ServerのChatGPT認証・thread/start/read/unsubscribe・正常終了と資源解放を追加生成0回で確認した。
subscription残枠や中断時を含む完全な利用量は未測定。APIキー・従量課金APIは使っていない。

## 検出した失敗と修正

1. 初回daemon試験の正常停止でscope supervisorへ早期SIGTERMを送り、実行中taskがUNKNOWNになった。
   先にcancelを保存し、runnerがinterruptと記録を終える順へ修正した。fixtureもproductionと同じsignal処理に揃えた。
2. 多数の接続・切断後にAF_UNIX backlogが満杯となりclientのconnectがEAGAINになった。
   acceptを有限batchで処理し、接続数を制限、送信前だけ期限付きで混雑を待つよう修正した。
   送信後の再送は追加していない。初回の失敗・error原出力はdaemon-tests-1.txtに保持した。
3. 既存Phase 2-CのテストにcloseされないSQLite contextがあり、追加試験のGCでResourceWarningを観測した。
   transactionとconnectionの両方を閉じる修正のみを行い、既存assertを変えていない。
4. レビューで同じUIDのモデルからuser socketへの参照経路を確認し、旧namespace／verifierでも/run/userを隠した。
   実OSの不可視試験を追加した。

## 証跡

すべてGit対象外の`.runtime/phase2-d2-evidence/`へ保存する。

- baseline.json／phase2-d1-source-backup.tar.gz: commit、開始時clean状態、49ファイル・保護対象metadata。
- baseline-tests.txt／regression-1.txt／final-tests-7.txt: 既存回帰と最終結果。
- final-cli-tests.txt: 最終CLI修正の対象2件の再検証。
- daemon-tests-1.txt〜4.txt: 初回失敗・修正後結果。削除していない。
- regression-check.json: 既存101件とassertの一致。
- native-be863b50.json／native-run-1.txt: 実Codex統合結果。
- current-code-control-followup.json: 最終コードのCLI・再起動確認、追加生成0。
- legacy-native-handshake.json: 旧read-only経路の実App Server接続・資源解放、追加生成0。
- service-verification-final.txt／service-state.txt: unit構文・未導入状態。
- preservation-check.json／config-change-observation.json: 保護対象照合と下記の差分。
- git-index-audit.json／commit-result.json: 登録・署名・最終作業ツリー。

daemon／workerのresource receipts、task DB、JSONLは各専用runtimeにも保持する。
ログは秘密形式除去済みで無加工wire原本ではない。環境変数全体、認証内容、SSH秘密鍵は記録しない。

## 既存環境の確認と未解決事項

auth.json、AGENTS.md、Phase 0資料、history.jsonl、session_index.jsonlはsize・mtime・mode・inodeが開始時と一致した。
auth内容はPythonで開かず、Codex標準認証へread-only mountした。既存Codex内部DBやIPCを直接変更していない。
SSH秘密ファイル、既存systemd設定、製品リポジトリ、外部remoteは操作していない。

**~/.codex/config.tomlは内容一致を確認できなかった。** 作業中の17:56:04 JSTに更新され、2759→2988 bytes、inode／hashも変化した。
開始hashは50f0af67b900b4604890cbd22d75e2eab21fd21e4501b1abc3201f89494d8c87、観測後hashは9f738bea54393005f843174f3f64b86f5e36c0ad30e098461d1a935bf40a6d96。
利用者へ確認した回答は「変更していない／不明」。原因は未特定で、外部変更と断定しない。
実装内の設定書込み先は専用runtime/codex-home/config.tomlであることを確認した。元設定の復元・上書きはしていない。
このため「全保護対象が開始時と同一」とは報告しない。元設定の差分・更新元の確認は未解決事項として残す。

ほかの未検証事項: 実user serviceの自動Restart／enable／ログイン時起動、日単位soak、高負荷、他OS／CLI、厳密な総容量quota、停電／OS再起動。
実serviceの導入やhost権限・quota変更は本作業で実行していない。必要な場合は具体的内容をレビューして別途判断する。
Phase 2-EのChatGPT・ローカルLLM接続、HTTP／MCP公開には着手していない。
