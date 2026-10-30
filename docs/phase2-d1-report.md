# Phase 2-D1 実施報告

パスは[表記規約](path-conventions.md)の疑似パスです。シェル例は同ページの変数を設定してから使用します。原本ログは変更していません。

実施日: 2026-10-28（Asia/Tokyo）。AIによる実測の要約。原出力・除去済みログ・集計を区別する。
対象は`<PROJECT_ROOT>/`のみ。Phase 2-D2のdaemon化には着手していない。

## 実装結果

既存の独立Gitリポジトリを再利用し、namespaceを保った資源制限、保存量確認、保持方針を追加した。
既存83件をすべて保持し、18件を追加した計101件のテストを実行した。
実Codexの2並列実行で専用coordinatorをSIGKILLし、lease解放後のUNKNOWN、子・scope解放、DB整合性、同一task再実行拒否を確認した。
別の新規タスクは既定制限下でCOMPLETEDとなった。UNKNOWNタスクを再実行したり初期化したりしていない。

**ディスク総量は監視予算であり、厳密なfilesystem quotaではない。** 1ファイルのkernel強制上限とは区別する。
厳密な総容量を必須にする常駐運用は、別途quota／filesystemの設計・承認・実測が必要であり、今回完了したと主張しない。

## Git管理と安全性

- 開始時は独立した.gitが存在し、mainはNo commits yet、全作業成果が未追跡、remoteなしだった。git initを再実行していない。
- HOME／~/.codexはGitリポジトリにしていない。既存成果物を削除・移動・初期化していない。
- .gitignoreで.runtime、SQLite／journal、JSONL／ログ、認証／秘密鍵、cache、backup等を除外する。
- scripts/git_audit.pyは登録済みindexのblob自体を検査する。パス、非通常mode／symlink、binary、大きいblob、既知の秘密形式を確認し、値は表示しない。
- source／docs／tests／examples／設定ファイルを用途ごとにレビューする。実認証、実行ログ、SQLite、生成schema、退避tarを登録しない。
- repo外ファイルや第三者の実装・依存をvendoringしていない。公開ライセンスの選択・外部公開は今回行わない。
- 初回commitでは既存Git author／SSH署名設定を尊重する。署名の無効化やglobal設定変更は行わない。

index監査の結果は`.runtime/phase2-d1-evidence/git-index-audit.json`、commit結果は同ディレクトリの`commit-result.json`と実際のGit履歴で確認する。
この報告ファイル自身を含むcommitの識別子は、循環参照を避けて本文へ埋め込まない。
GitHub作成、remote登録、push、公開は実施しない。
自動検査は機密性の万能判定ではない。今回はローカル登録であり、将来の公開前には別途審査する。

## 資源制限の方式と実測

Python 3.14標準ライブラリ、既存Linux namespace、systemd 259.9のuser scopeを使用する。
user@1000.serviceにcpu/memory/pidsが委譲済みで、専用scope作成・実効値照合が成功した。
既存app.sliceやhostのcgroup設定を直接変更せず、専用agentorchestrator.slice配下だけを使用した。
既存Codex daemon、IPC、設定・認証を操作しない。sudo・権限拡大・依存導入は行っていない。

| 制御 | 既定値・結果 |
| --- | --- |
| CPU帯域 | 100%＝1 CPU相当。25%のfixtureではnr_throttled増加を実測 |
| scope CPU予算 | 60,000 ms。小さい予算で停止・解放を実測 |
| memory.max | 2 GiB、swap上限0。64 MiB fixtureでmemory.eventsとOOMを実測 |
| pids.max | 512 TID。12 TID fixtureでfork拒否と解放を実測 |
| RLIMIT_FSIZE | soft/hard 64 MiB。64 KiB fixtureで強制上限を実測 |
| runtime保存量 | 2 GiBの監視予算。1 MiB fixtureで停止、証拠は残存 |
| filesystem空き | 1 GiBの開始／継続条件。環境全体を容量枯渇させる試験はしない |
| 通常JSONL | 32 MiB上限。完全行prefixと上限markerを保持し、失敗を記録 |

実Codex正常タスクのApp Server scopeでmemory.peak=77,529,088 bytes、pids.peak=89を観測し、終了後の解放を確認した。
設定値はresource-limits.jsonから固定し、execution.owner、cleanup、resource receiptへ保存する。
実効値が違う、user busに接続できない、receiptが壊れている場合は正常完了にしない。制限を外して再実行するfallbackはない。
CPU throttlingだけを失敗とはせず、予算超過・memory/pids制限到達・制限による終了はFAILED、監督／解放不明はUNKNOWNとする。
独立検証コマンドにも同じ制限を適用する。coordinator／Python worker自体はscope外である。

schemaはv3のまま。既存JSON列へのpolicy／receipt参照追加なので既存行の破壊・再初期化・schema移行は不要。
詳細と公式資料は[資源制限](resource-limits.md)を参照。

## 障害注入とテスト

```bash
python3 -B -m unittest discover -s tests -v
python3 -B -m unittest discover -s tests -p test_phase2_d1.py -v
# 実Codexを利用する明示的な統合試験（通常3モデルturn）
python3 -B tests/live_phase2_d1.py
```

既存83テストの名前・検証意図を保持した。削除やskipで成功にしていない。
追加18件はkernel制限、異常時の状態、保存量・保持、壊れたreceipt、PATHの引渡し、index上の秘密情報検査を対象とする。
模擬server＋実cgroup／namespaceのテストと、実Codexの統合試験を区別する。

| 実測 | 結果 |
| --- | --- |
| 正常実行・独立検証 | 制限が有効なままCOMPLETED |
| CPU／memory／pids超過 | 制限を観測しFAILED、scope・所有process解放 |
| per-file上限 | kernelが書込みを制限。SIGXFSZを終了根拠へ記録 |
| 保存予算／ログ上限 | 失敗停止。証拠の自動削除なし |
| user bus不通 | 実験用の接続先を変えて拒否を確認。host権限変更なし、対象コマンド未実行 |
| receipt破損 | confirmed=false、正常完了と推測しない |
| 並列実Codex coordinator SIGKILL | 両方のsleepコマンド開始を確認後、Popenで所有するcoordinatorのみをkill |
| 再照合 | 両方UNKNOWN、2つの専用scopeが消失、所有PID残存なし |
| DBと再実行防止 | integrity_check=ok、foreign_key_check=0、再実行2件拒否、execution件数不変 |
| Git監査 | staged blobを検査。合成秘密・binary・symlinkを拒否し、値を出力しない |

実Codexは3モデルturn。最初のPATH拒否試行ではturn/startに至らず、モデル生成0回だった。
中断分の利用量・subscription残枠の完全な測定はできない。APIキー・従量課金APIは使用していない。
停止したのは専用実験processだけ。OS再起動・停電は実施していない。

## 検出した失敗と対応

1. 既存coordinatorがworkerのPATHを/usr/binへ固定していたため、HOME配下の正規Codexを隔離の実行資源検査で拒否した。
   PATHとLANGだけを引き継ぐ修正を行い、認証等の環境変数は追加しなかった。.runtime内の実行ファイルを信頼資源にする経路も拒否する。
2. その拒否メッセージを変更した際、既存の正規表現assertが1件失敗した。拒否処理を維持してメッセージ契約を戻した。assertを削除・緩和していない。
   この中間回帰ログは同名の途中ログ更新で原本を保持できず、regression-failure-summary.jsonへAI要約として記録した。原本と偽っていない。
3. 初回native SIGKILL試験の検査がworkerのlease解放より早く、RUNNINGを見てバッチ判定はfalseになった。
   生きているleaseを尊重するruntimeの動作は正しく、試験側にbounded recover待機を追加した。
   追加生成せず同じ停止済みfixtureを再照合し、UNKNOWN／解放／再実行拒否を確認した。初回false結果は保存した。

UNKNOWNの処理を繰り返したり、失敗した試験を削除して完了と扱ったりしていない。

## 保持期間・容量管理

usageで分類別のapparent／allocated bytesと保護対象taskを表示する。
例の実機runtimeの割当量は14,647,296 bytes（取得時点のサンプル）。
UNKNOWN・人間判断待ち・進行中の証拠は保護し、snapshot／resumeの依存も整理判断時に追跡する。
30日／90日等の提案、手動整理前の照合とbackup手順は[保持方針](retention-policy.md)へ記載した。
自動削除・prune・圧縮・既存データの整理は実装・実行していない。

## 主な変更ファイル

- 新規: resource-limits.json、src/resources.py、src/resource_entry.py、src/retention.py。
- 新規: scripts/git_audit.py、tests/test_phase2_d1.py、tests/live_phase2_d1.py。
- 新規資料: docs/resource-limits.md、docs/retention-policy.md、本書。
- 更新: .gitignore、README.md、src/runner.py、transport.py、verification.py、recovery.py、coordinator.py、isolation.py、tests/fake_codex.py。
- 更新資料: architecture、concurrency、recovery、workspace-isolation、verificationの各設計書。

Phase 0資料、過去の実施報告、既存テストは保持した。開始時ソースはphase2-c-source-backup.tar.gzに退避した。

## 証跡と既存環境

証跡はすべてGit対象外の`.runtime/phase2-d1-evidence/`へ保存する。

- baseline.json／phase2-c-source-backup.tar.gz: 開始時manifestと保護対象metadata。
- baseline-tests.txt／final-tests.txt／regression-check.json: 既存83と最終結果。
- kernel-measurements.jsonl: 専用fixtureの除去済みProcess結果・kernel counter。
- native-c094b636.json／native-run.txt: PATH拒否。モデル生成なし。
- native-40e4c341.json／native-run-2.txt: 実Codex同時開始・SIGKILLと初回検査false、正常タスク。
- native-reconciliation-followup.json: 追加生成なしの再照合成功。
- usage.json／preservation-check.json／git-index-audit.json: 保存量・環境保持・Git登録検査。

~/.codexのauth/config/AGENTS/Phase 0資料/history/session indexは、size・mtime・mode・inodeが開始時と一致。config hashも一致。
auth内容はPythonで開かず、read-only mountをCodex標準認証へ渡した。既存Codex内部DB全体のbit比較はしていない。
Gitのglobal設定、既存Codex設定・認証、製品リポジトリ、既存daemonを変更していない。

## 未検証と承認事項

厳密なdisk総量quota、coordinator／worker自身のkernel資源制限、長期運転、高負荷、他OS／CLI版、停電・OS再起動は未検証。
容量予算にはサンプル間の超過余地がある。これをhard quotaとして運用しない。
host全体のcgroup設定変更・委譲変更・filesystem quota導入が必要になれば、対象と影響を示して承認を求める。今回は実施していない。
次工程では保護証拠の依存管理、長期容量、制御側資源境界、障害時のquota要件を先に確定することを提案する。
Phase 2-D2のdaemon化、ChatGPT／ローカルLLM接続、GitHub公開には着手していない。
