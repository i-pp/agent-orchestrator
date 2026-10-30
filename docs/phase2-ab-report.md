# Phase 2-A/B 実施報告

パスは[表記規約](path-conventions.md)の疑似パスです。シェル例は同ページの変数を設定してから使用します。原本ログは変更していません。

実施日: 2026-10-28 / Asia/Tokyo。対象: `<PROJECT_ROOT>/`。
Phase 2-A/Bの復旧・照合・受入検証を実装した。Phase 2-C以降、daemon、複数エージェント、ChatGPT接続は実装していない。
本書はAI生成の要約であり、仕様・除去済みログ・実測結果と区別する。

## 実施内容

開始時にグローバルAGENTS、Phase 0資料、Phase 1資料・ソース・テストを確認した。
作業ツリーは未コミットのPhase 1成果物で、前回manifestからの変更はなかった。
既存15ファイルをprivateなソースarchiveへ退避し、Phase 1基準22件を実行してから変更した。

主な変更:

- task_id、execution_id、Codex thread/turnを分離し、起動・送信・プロセス所有情報をSQLiteへ保存。
- 起動時のRUNNING/VERIFYINGをUNKNOWNへ移すだけとし、再投入しない。
- 所有プロセス、workspace manifest、JSONL、公式保存履歴を照合。照合ではturn/startを呼ばない。
- resolveによる再検証・失敗判断・一度だけの再開許可と、実際の新規投入・runを分離。
- VERIFYING、WAITING_FOR_HUMANを追加。Codex実行、成果物、許可済みテスト、個別条件、人間判断を別々に記録。
- 受入条件未指定や結果文字列だけでは自動COMPLETEDにしない。
- 改行なしの末尾JSONは、valid JSONに見えても完了イベントにしない。
- 既存DBのbackup付き移行と、データを捨てないロールバックを実装。

## ファイル

更新:

- README.md、docs/architecture.md
- src/runner.py、src/transport.py、src/isolation.py
- tests/test_runner.py、tests/fake_codex.py、tests/live_checks.py

新規:

- src/storage.py、src/recovery.py、src/verification.py
- verification-commands.json、examples/read-fixture.acceptance.json
- tests/test_phase2.py、tests/crash_driver.py、tests/live_phase2.py、tests/fixtures/phase1.sql
- docs/recovery-design.md、docs/verification-design.md、docs/phase2-ab-report.md

Phase 1の実施報告・比較資料は履歴として保持した。既存22テストの名前・主要な合格条件を残した。
正常・再開の2ケースには、独立したfixtureを使う明示受入条件を追加し、AIの自己申告だけで成功する旧前提を修正した。
Phase 1実機fixtureも、受入条件指定とexecution別ログ名に対応させた。

## テスト結果

```bash
python3 -m unittest discover -s tests -v
python3 tests/live_phase2.py --execute
```

自動テストは **57件成功（既存22件、新規35件）**。
ソースarchiveとのテスト名照合でも既存22件の欠落はなかった。
コードカバレッジの測定はしていない。要求項目ごとの確認は以下のとおり。

| 要求 | 結果・根拠 |
| --- | --- |
| 正常実行から検証成功 | 模擬・実Codexとも、成果物hash・許可済みテスト・固有条件を照合してCOMPLETED |
| Codex正常終了・成果物不備 | SUCCEEDEDを保ち、欠落成果物によりタスクFAILED |
| テスト失敗 | 許可済み実OSコマンドexit 7を保存しFAILED |
| プロセス異常終了 | 模擬serverの実プロセスexit 17でUNKNOWN。exit 0の応答切断も成功にしない |
| 親プロセス停止 | RUNNING/VERIFYINGで専用fixture親をSIGKILL。実Codexでも実行中の親をSIGKILL |
| 再起動後照合 | UNKNOWNを維持し、PID/start/boot・公式履歴・manifestを照合。自動再実行なし |
| UNKNOWNの判断 | 保存結果だけの再検証、明示失敗、単回の再開許可、古い許可の拒否を確認 |
| 二重実行防止 | task取得CAS、executions.task_id一意、request_key、再開許可の単回使用を確認 |
| 中断・期限・解放 | Codex段階は実機、検証段階は実OSプロセスで確認。稼働中残存なし |
| Phase 1回帰 | 22件を維持して成功 |

追加で、改行なしのJSON断片、valid JSONに見える未完了行、別turnの結果、symlink/path逸脱、未許可コマンド、許可定義変更、古い人間承認、PID再利用、起動checkpoint欠落、中断と完了の競合を検証した。
起動checkpoint保存失敗でも子プロセスを解放すること、古いcleanupで検証中プロセスを隠さないことも確認した。
初期試験でfixtureのSQLite接続解放警告を検出し、明示closeへ修正した。最終試験に警告はない。

## 実Codexの障害注入

新規の専用runtime `.runtime/phase2-live-4ec51b3b/`だけを使用した。
バイナリはPhase 1と同じ `0.164.0-alpha.11.1`。再生成した320 schemaでthread/read、thread/turns/list、thread/resume、turn/start等を照合した。
新しいSDKや実験APIのopt-inはない。App Serverコマンド自体は実験的である。

| 実機ケース | 結果 | 解放・照合込み秒 |
| --- | --- | --- |
| 正常実行と受入検証 | COMPLETED / SUCCEEDED / PASSED | 9.251 |
| 保存セッション再開 | 同一thread、新しいtask/execution/turn、検証成功 | 6.382 |
| 期限 | TIMED_OUT、稼働中残存なし | 12.141 |
| 中断 | CANCELLED、稼働中残存なし | 5.240 |
| 親SIGKILLと再起動照合 | UNKNOWN、資源解放済み、保存turnはinterrupted、workspace不変 | 9.376 |

親停止後は自動成功・自動再実行にせず、テストoperatorの明示判断でFAILEDとして閉じた。
これは実行失敗を推定したものではなく、結果が不確実なタスクを理由付きで終了した記録である。
会話resumeと途中処理の復元を混同していない。

モデル生成開始は5件、使用量報告は4件分を保存した。未完了ターンの消費を完全には取得できず合算しない。
実機イベント中の利用率観測は同じ7日窓で20%→20%、後続の非生成確認は21%だった。
このチャット等の同一アカウント利用と分離できず、本実験だけの消費率・料金には換算できない。
APIキー・従量課金APIは使用していない。

## 既存データの互換性

実Phase 1ランナーDBの12行をコピーし、旧17列が全て一致すること、foreign_key_check成功、旧状態保持、LEGACY_UNVERIFIEDを確認した。
ロールバック後も12行が一致し、schema version=0に戻った。元のPhase 1 DBは変更していない。
専用旧schema fixtureでは自己参照・遷移を保持し、移行途中の失敗をtransaction rollbackで戻した。
新規データがある復元、独自indexの暗黙削除、backup先symlinkによる書込逸脱は拒否した。
旧COMPLETEDを新しい検証済み成功として扱わず、completion_verifiedを明示する。

## 記録と環境保持

証拠はGit対象外の `.runtime/phase2-evidence/` に保存する。

- baseline.json、phase1-source-backup.tar.gz、phase1-baseline-tests.txt
- final-tests.txt、regression-and-usage.json
- live-4ec51b3b.json、schema-check.json、final-protocol-no-generation.json
- actual-phase1-data-migration.json、migration-copyのbackup
- preservation-check.json、artifact-check.json、source-manifest.json

native runtimeにはexecution別・照合・検証の秘密形式除去済みJSONLを保持する。DBから根拠と判断をIDで追跡できる。
既存~/.codexの設定・認証・履歴を直接変更せず、既存daemon/IPCを操作していない。
保持確認は設定hashと主要保護ファイルのmetadataを比較する。共有環境の全DB・ログの全バイト不変までは主張しない。
GitHub push、外部公開、既存データの削除・初期化は行っていない。

## 未検証事項・次の判断

- OS再起動・停電、ディスク故障・容量不足、実ネットワーク断、認証失効・更新。
- 他OS、他CLI版、keyring認証、大量出力・manifest上限の負荷試験。
- 一時的・外部・ネットワーク等の副作用の完全な検出や巻戻し。
- 任意タスクに対する受入条件自体の意味的な正しさ。
- 悪意ある同一OSユーザーを隔離する認証・サービス境界。actorはローカル申告である。

次工程ではこれらの条件を明示して検証を追加する。Phase 2-C以降の実装には着手していない。
