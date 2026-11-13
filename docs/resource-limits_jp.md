# 資源制限 — Phase 2-D1

## 境界と調査結果

Codex App Serverとその子、履歴照合probe、許可済み検証コマンドを、各々新しいsystemd user scopeへ入れる。
既存のuser/mount/PID/IPC namespace、専用CODEX_HOME、認証のread-only bind、sandbox方針を維持する。
coordinator／Python worker自体はscope外の制御側であり、この上限の対象ではない。
daemon・常駐scheduler・新しいSDKは追加しない。

現在のuser@1000.service配下にcpu/memory/pids controllerが委譲済みであることを読み取りで確認した。
systemd 259.9の`systemd-run --user --scope`で新しい専用scopeの制限値を設定できた。
既存app.sliceのcontroller設定を直接変更せず、新しいagentorchestrator.slice配下にscopeを作る。
ホスト全体のcgroup、ユーザーの既存service、権限・sudo設定は変更しない。
rootのcgroup.subtree_controlへの直接書込み、Delegate設定変更、パッケージ導入は行わない。

[cgroup v2の公式仕様](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)と
[systemd resource-control](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)を参照した。
現環境で使用する項目はインストール済みsystemdで実測し、文書の新しい版だけを根拠に採用していない。

## 設定と既定値

operatorがリポジトリのresource-limits.jsonを管理する。投入prompt／モデル出力は設定に使用しない。
各workerの開始時に固定し、execution.owner.resource_policyとresource_scopesへ保存する。
CLIに制限を無効化するオプションはない。未知キー、0、負値、無効型は拒否する。

| 設定 | 既定 | 意味 |
| --- | --- | --- |
| cpu_percent | 100 | scope全体で1 CPU相当。cpu.max=100000 100000 |
| cpu_budget_ms | 60000 | そのscope内の累積CPU使用時間。監視で超過停止 |
| memory_bytes | 2147483648 | scope全体のmemory.max=2 GiB |
| pids | 512 | scopeのTID／process数上限 |
| file_bytes | 67108864 | 子孫へ継承するRLIMIT_FSIZEのsoft/hard上限64 MiB |
| runtime_bytes | 2147483648 | runtime全体の保存量2 GiBで投入拒否／実行停止 |
| free_bytes | 1073741824 | 同じfilesystemの空き1 GiB未満で投入拒否／実行停止 |
| log_bytes | 33554432 | 通常実行JSONLの保存上限32 MiB |

memory.swap.max=0、core dump上限=0も固定する。
CPU比率は1〜200%、pidsは8〜4096を受け付ける。memoryは32 MiB以上、logは4 KiB以上。
値を緩和する場合はhost容量と2同時実行を再評価する。既定値は今回の実Codex正常実行と並列障害注入で検証した小規模fixture用の値であり、任意のビルド負荷への保証ではない。

## 強制と記録

1. runtime保存量／空きを確認し、新しいUUID付きscopeだけを起動する。
2. trusted resource_entryが実際のcgroupパス、cpu.max／memory.max／swap／pids.maxを照合する。
3. RLIMIT_FSIZEとRLIMIT_COREを設定し、READY receiptをfsyncしてからnamespace子を起動する。
4. 親PIDの連鎖とparent-death signalを確認する。子の起動にも既存child_entryを使う。
5. controllerはcpu.stat、memory.events、pids.eventsと保存量を監視する。
6. 子終了後にFINISHED receipt、終了コード、最終counterを保存する。
7. Process.closeは所有PID/startとscopeの解放を確認し、cleanupへ結果を追加する。

scopeは`agentorchestrator-<UUID>.scope`、receiptはruntime/resource-events/内。
receiptに要求値・実効値・CPU／memory／pids counter・file limit・終了状態を保存し、owner／cleanup／ログから追跡できる。
元のCodex内部DBや既存daemonは操作しない。DBスキーマはv3のまま、既存JSON列へ追加して旧行を保持する。
scopeはcollectされる。共有の専用sliceが空で残る場合も、稼働processやmemory課金が残るという意味ではない。

[systemd-runの公式説明](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html)に従い、scopeを使って呼出元との親子関係を保つ。
serviceとしてdetachする経路は使わない。user bus不可、controller不足、実効値不一致では子を起動せず拒否する。通常起動へのfallbackはない。

## 成否と復旧

CPU帯域のthrottling自体は通常の制御であり失敗ではない。
CPU累積予算、memory.max、pids.max、ファイル上限による終了、保存量／ログ上限はタスク成功にしない。
観測された制限超過と解放が確定すればFAILED。dispatch後の監督や解放が不明ならUNKNOWN。
Codexが正常終了しても、資源条件を満たさなければタスクをCOMPLETEDにしない。
検証コマンドの制限超過も受入不合格。未解放scopeがあればUNKNOWNとする。

親停止ではREADY receiptだけが残り得る。再起動時は生きたleaseを尊重し、消失ownerのRUNNING／VERIFYINGだけUNKNOWNへ移す。
PID/start/bootに加えて、receiptが示す専用cgroupの不在／populated=0も照合する。
親死亡とlease解放は非同期なので、終了途中のworkerをUNKNOWNへ先走って変更しない。必要ならrecoverを再度行う。
UNKNOWNを自動再実行しない。receiptの欠落・破損を正常完了と推測しない。

## ディスク容量の限界

cgroup v2のio.maxはI/O速度・回数の制限であり、filesystem保存容量のquotaではない。
今回のdisk制限は次の組合せで、厳密な総容量quotaとは異なる。

- RLIMIT_FSIZE: kernelによる**1ファイルごと**の強制上限。子孫はhard limitを上げられない。
- runtime合計とfilesystem空き: 起動前と処理中のサンプル監視による拒否／停止。通常約0.2秒単位で、長いOS処理中は遅れ得る。
- JSONL: 完全行のprefixを保持し、上限markerを保存して停止。既存ログを削除・ローテートしない。cleanupはSQLiteとresource receiptに保持する。

[getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html)のRLIMIT_FSIZEを使用する。
多数ファイルへの高速書込みは監視の間に予算を超過し得る。保存量はapparent bytesとst_blocks*512の大きい方で判定し、filesystem metadata分は厳密には含まない。
厳密な総容量保証を必要とする運用は、現実装だけでは開始しない。
project quotaや専用filesystem等の導入には別途実機設計と、host設定・権限変更が必要なら人間の承認が必要。今回は変更・導入していない。

## 検証範囲

実cgroupでCPU throttling／CPU予算／memory OOM／fork拒否、実RLIMIT_FSIZE、保存予算停止を検証する。
異常系は専用fixtureだけを使い、host全体の制限や既存processを変更しない。
実Codexの2セッション＋coordinator SIGKILLと、既定値下の別正常タスクを確認した。
coordinator／worker自身のkernel資源上限、厳密な総disk quota、停電／OS再起動、高負荷・他環境は未検証。

## Phase 2-D2の制御側制限

新しいdaemon／workerはscheduler-config.jsonの別scopeでCPU／memory／TIDを制限する。既存タスクのresource-limits.jsonは維持する。
daemonの長寿命CPU予算とタスクの短い予算を分け、容量超過時はschedulerが新規dispatchを止めて制御socketを保つ。
実効値・停止・回復は[daemon設計](daemon-design.md)と[D2報告](phase2-d2-report.md)を参照。
