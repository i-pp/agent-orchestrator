# Phase 2-C 実施報告

パスは[表記規約](path-conventions.md)の疑似パスです。シェル例は同ページの変数を設定してから使用します。原本ログは変更していません。

実施日: 2026-10-28（Asia/Tokyo）。以下はAIによる実測結果の要約であり、無加工の原本ログではない。
対象は`<PROJECT_ROOT>/`。Phase 2-Cのみを実装した。

## 結果

最大2つの独立したCodexセッション、workspaceのOS境界、独立したImplementer/Reviewer、SQLite整合性と安全な復旧を実装した。
既存57件を含む83件の自動テストが成功した。実Codexで2件のworkspace-write同時実行・内容検証・独立レビュー・保存再開・個別停止を確認した。
既存共有workspaceのread-only動作、UNKNOWN自動再実行禁止、受入検証とCodex成否の分離は保持した。

初回の実Codexバッチはresume不具合により全体成功ではなかった。修正後に新規の独立セッションで再開を検証し、server重複防止修正後も継続を確認した。
初回UNKNOWNタスクはUNKNOWNのまま保存し、自動再実行・失敗記録の削除はしていない。

## 環境と方式

| 項目 | 実測 |
| --- | --- |
| OS / Python | Ubuntu 26.04.1 LTS / Python 3.14.4 |
| Codex | codex-cli 0.164.0-alpha.11.1 |
| 実パス | <CODEX_BINARY> |
| SHA-256 | e6d386e8822288f6d5f327e966a5a5e1de928de8795bb6149716005a410f38f2 |
| 制御 | 専用App Server stdio。既存daemonへ接続しない |
| 認証 | 既存ChatGPTファイル認証をread-only mount。APIキー／従量課金APIなし |
| 並列 | runtime単位で最大2。shared admission、task/workspace lease、2 slot |
| 隔離 | user/mount/PID/IPC namespace、最小root、pivot_root、旧root detach、Codex sandbox |

CLI生成schema 320ファイルと[公式資料](https://learn.chatgpt.com/docs/app-server)を照合した。
公式資料の新しいreadOnlyAccessは現schemaに存在しないため使わず、mount境界で読取りを制限した。
App Serverはalpha／実験的な基盤だが、experimentalApi=falseを維持した。SDKやフレームワークは追加していない。

## 実装ファイル

新規:

- src/concurrency.py — admission、2 slot、task/workspace lease、所有者不在時の復旧。
- src/coordinator.py — 前景の2 workerと組単位の停止。
- src/workspaces.py — 専用fixture、所有権、境界検証、snapshot、role。
- src/isolated_entry.py — 最小rootを組み立てるnamespace bootstrap。
- tests/test_phase2_c.py — 新規26件。
- tests/concurrent_driver.py、tests/pair_crash_driver.py — 隔離した障害注入fixture。
- tests/live_phase2_c.py — 実Codex統合検証（通常の全ケース実行は8モデルturn）。
- examples/workspace-fixture.acceptance.json。
- docs/workspace-isolation.md、docs/concurrency-design.md、本報告。

更新:

- src/runner.py — task単位のlayout／lease、role・権限、CLI、既知の過去usage通知の処理。
- src/storage.py — schema v3、同時投入／中断、移行・rollback、workspace識別子。
- src/isolation.py — 新しい隔離起動・preflight。従来起動は保持。
- src/recovery.py — 所有workspaceの照合、resume probeの資源記録。
- src/verification.py — 専用namespaceによる検証と安全側の判定。
- src/transport.py — chatgptAccountIdもログ除去の対象へ追加。
- tests/fake_codex.py — 独立した保存履歴、書込みsandbox応答、遅延・過去usage通知fixture。
- tests/test_phase2.py — 既存の移行成功テストのversion期待値を2から3へ更新。検証条件・テスト名は維持。
- verification-commands.json — 固定argvのworkspace-result-checkを追加。
- README.md、docs/architecture.md、docs/recovery-design.md、docs/verification-design.md。

Phase 0資料、Phase 1比較・報告、Phase 2-A/B報告、既存22件のtest_runner.pyは変更していない。
Gitは開始時から未コミット・未追跡の状態だったため、元ソースのSHA-256と退避tarを差分の基準にした。commit／pushは実施していない。

## 検証結果

```bash
python3 -B -m unittest discover -s tests -v
python3 -B -m unittest discover -s tests -p test_phase2_c.py -v
# モデル生成を伴う、明示的な実機検証
python3 -B tests/live_phase2_c.py
```

83件 = 既存57件（名前を照合し全件保持）＋新規26件。削除・skipによる成功扱いはない。
オフライン試験は模擬App Serverと実際のSQLite／プロセス／Linux namespaceを使用した。実Codexの成功と区別する。

| 要件 | 結果と根拠 |
| --- | --- |
| 2セッション同時実行 | 模擬・実Codexで成功。実行時間17.221秒／16.535秒、execution区間の重なり16.535秒 |
| thread／turn／イベント独立 | 異なるthread・turn・execution・workspace、各ログと結果の対応を検査して成功 |
| workspace間の書込み／読取り拒否 | 実namespaceで両方拒否。絶対path、相対traversal、proc rootでも拒否 |
| symlink／mount逸脱 | symlink／hardlink fixture拒否、実mount境界拒否、入れ子namespaceから/usrのRW remount拒否 |
| read-only／workspace-write | 実mountで前者の書込みを拒否、後者だけ自分の成果物作成に成功 |
| 一方のtimeout | 模擬・実CodexでTIMED_OUTと他方COMPLETED。残存なし |
| 個別cancel／全体停止 | 模擬で個別と全体を検証。実CodexでもCANCELLEDと他方COMPLETED（中断要求時に他方はRUNNING） |
| SQLite／二重実行 | 同時request_key投入6 workerで1 task／workspace。3件目のsession拒否、同一taskのdispatch重複なし、integrity/FK検査成功 |
| 独立snapshotレビュー | 実装終了・資源解放後にsnapshotを作り、別thread・別workspaceでread-only検証。sourceを後で変更してもコピーが不変（模擬）。実Codexの独立レビューも成功 |
| 親・coordinator SIGKILL | 専用fixtureで実OS障害注入。片方死亡では他方RUNNINGを保持。coordinator死亡では両方UNKNOWN、資源解放、新dispatchなし |
| 旧DB移行／回帰 | v0/v2移行、途中失敗rollback、変更後の破壊的rollback拒否。実v2所有DBのコピーも6表の行を全保持しv2復元成功 |

実v2コピーの保持件数: tasks 5、transitions 18、executions 5、reconciliations 2、verifications 2、decisions 1。
元の所有DBは変更せず、Codex内部DBは直接操作していない。

## 検出した問題と対応

1. 新rootのread-only remount指定が初期案で失敗。bind remountへ修正し、mount成功を確認した。
2. chroot内の入れ子user namespace作成が拒否された。`pivot_root`＋旧root detachへ変更した。nested bwrap preflightで必要な/dev/full等も揃えた。
3. 実resumeで、前turnのthread/tokenUsage/updatedが新turn応答より前に届いた。厳密なturn照合によりUNKNOWNとなった。
   既知の過去turnのusage通知だけを記録して現在usageと分離した。別turnの完了結果・未知turnは引き続き拒否する。再現fixtureと実Codexの新規独立セッションで修正を検証した。
4. namespace preflight／version取得のTimeoutExpiredをTIMED_OUTへ分類し、resume照合にも残り期限を伝播した。これらの期限・後続境界修正は最終自動テストで確認した。
5. resumeの履歴probeと実行serverが一時的に重なる経路をレビューで修正。probe終了後に実行serverを開き、起動checkpointとプロセスを現在executionへ記録した。
6. 認証内容ではないchatgptAccountIdが初期の除去規則から漏れていた。除去対象へ追加し、今回のランナーJSONLも除去済みへ更新した。無加工原本は保持していない。

失敗した試験・初回バッチ結果は保持している。初回UNKNOWNの同じtask_idを再実行していない。

## 証跡と利用量

専用runtimeは`.runtime/phase2-c-live-f50c4875/`。DB・ログ・fixture・snapshotはGit対象外。
`.runtime/phase2-c-evidence/`内の主な証跡:

- baseline.json、phase2-ab-source-backup.tar.gz — 開始時のsource・保護対象metadata。
- final-tests.txt、isolation-and-concurrency-tests.txt、regression-check.json — テスト原出力と件数照合。
- schema/、schema-check.json — インストール済みCLIの生成schema・実パス・hash。
- live-f50c4875.json、live-run.txt — 初回バッチ。全体passed=falseを保持。
- native-resume-regression.json、final-native-resume.json — 修正後の独立セッションと継続成功。
- native-summary.json — 各executionのイベント対応、資源解放、利用量の集計要約。
- actual-v2-migration.json、migration-copy/ — コピー上の移行・復元。
- preservation-check.json — 既存環境の保持確認。

モデルturn/startは計11回（初回8、新規レビューと再開2、server重複防止修正後の継続1）。自動再試行はない。
10タスクで利用量通知を観測し、thread別の最後の累積totalTokensを合算すると403,058。
これはキャッシュ入力を含む観測値で、サブスクリプション残枠・課金額・中断された全処理の完全な利用量を示さない。
ログは秘密形式除去済みJSONLで、無加工のwire原本ではない。要約JSON・本報告と区別する。

## 既存環境の保持

~/.codexのconfig.toml／AGENTS.md／Phase 0資料／auth.json／history.jsonl／session_index.jsonlは、開始時と終了時のsize・mtime・mode・inodeが一致。
config.tomlのSHA-256も一致。auth内容はPythonで読取・記録・複製していない。
既存Codex内部DB・既存daemon／IPCを直接操作していない。新規CODEX_HOMEとread-only認証mountで実行した。
Codex内部DB全体のbit単位比較はしていない。既存製品リポジトリ、設定、認証を初期化・削除していない。

## 未検証事項と次工程への提案

- 他OS／他kernel／別CLI版、keyring認証、認証更新が必要な場合、共有FS、高負荷、OS再起動・停電は未検証。
- 並列中の実Codex coordinator SIGKILLは未実測。今回は模擬server＋実OSプロセスで障害注入した。
- namespaceはVM・cgroupの代用ではなく、CPU／メモリ／総ディスク量の強制上限は未実装。
- snapshot fingerprintは通常ファイル内容を対象とする。ACL/xattr・実行bit・空directoryの完全な同一性保証ではない。
- roleの認可主体はローカルoperator。外部利用者認証・複数ホスト・3セッション以上は対象外。
- Git worktreeは将来の入力方式としてのみ設計。既存製品checkoutへの接続は実装していない。

次工程では対象CLI版を固定し、資源上限・保持期間・snapshot対象仕様・追加の実Codex障害注入を先に定義することを提案する。
daemon化、ChatGPT接続、ローカルLLM接続、GitHub公開には着手していない。今回のローカル実装を完了するための追加承認事項はない。
