# Phase 1 実施報告

パスは[表記規約](path-conventions.md)の疑似パスです。シェル例は同ページの変数を設定してから使用します。原本ログは変更していません。

実施日: 2026-10-28 / Asia/Tokyo。作業先: `<PROJECT_ROOT>/`。
開始時にPhase 0の4資料を読み、作業先と祖先の指示・競合を確認した。対象ディレクトリは不存在だったため新規作成した。
本書はAI生成の要約であり、原本仕様、除去済み実行記録、テスト結果とは区別する。

## 1. CLIとApp Serverの比較結果

同じread-onlyファイル読取タスクで、CLIの通常6.278秒・再開3.853秒、App Serverの通常6.741秒・再開3.908秒を観測した。
両方で結果、同一セッションID、前ターンのnonce保持、正常終了を確認した。単発測定であり性能差を一般化しない。
CLIではJSONL・異常終了・タイムアウト・コマンド開始後の中断とプロセス解放を確認した。
App Serverでは専用stdio、初期化、thread/start/read/resume、turn/start/interrupt、unsubscribe、接続終了を確認した。
公式資料と実機バイナリ生成schemaを照合し、非実験APIの範囲で実装したが、App Serverコマンドとバイナリ自体は実験的・alphaである。
詳細: [runtime-comparison.md](runtime-comparison.md)。

## 2. 採用方式と理由

**専用App Server stdio子プロセス方式を採用した。**
CLIでも単発実行と保存再開は可能だった。一方、状態監視と実行中断をthread/read・turn/interruptで直接扱う方が、将来のセッション管理に必要な外側の補完を減らせる。
SDK・HTTPサーバー・共通backend抽象化は導入せず、Python 3.14標準ライブラリで実装した。
Linuxの隔離コマンドは、既存CODEX_HOMEを変更せず、既存認証をコピーせずに再開を検証するために使用している。

## 3. 実装ファイル一覧

| 対象 | 内容 |
| --- | --- |
| `README.md` | 環境要件、CLI、実行方法、DB schema、状態、検証方法 |
| `src/runner.py` | タスク投入、状態、SQLite、保存再開、イベント処理、期限・中断、CLI |
| `src/transport.py` | JSONL/RPC、ログ除去処理、両pipeの読出し、プロセス・子孫の解放 |
| `src/isolation.py` | 専用CODEX_HOME、固定read-only設定、認証・sandbox確認 |
| `src/ns_entry.py` | 専用user/mount/PID namespace内のmountと権限縮小 |
| `src/child_entry.py` | 親死亡時の子プロセス停止 |
| `tests/fake_codex.py`、`tests/test_runner.py` | オフラインのプロトコル模擬・障害・SQLite・子プロセステスト |
| `tests/compare_runtimes.py` | CLI/App Server通常実行・保存再開の実機比較 |
| `tests/compare_faults.py` | CLI異常・期限・中断の実機比較 |
| `tests/live_checks.py` | 採用ランナーの実機受入検証 |
| `docs/runtime-comparison.md` | 実測比較、失敗と修正、採用理由 |
| `docs/architecture.md` | 責務、状態遷移、実行境界、記録・解放 |
| `.gitignore` | DB、ログ、実験一時ファイル、Pythonキャッシュの除外 |

ローカルGitリポジトリを初期化し、`.runtime`のDB・JSONL・生成schemaが除外されることをgit check-ignoreで確認した。
コミット、remote登録、GitHubリポジトリ作成、push、外部公開は行っていない。

## 4. 実行方法

```bash
cd "$PROJECT_ROOT"
python3 src/runner.py check
python3 src/runner.py submit 'Read only fixture.txt and report its marker.'
python3 src/runner.py run "$TASK_ID" --timeout 60
python3 src/runner.py show "$TASK_ID"
python3 src/runner.py cancel "$TASK_ID"
python3 src/runner.py submit 'Continue the previous task.' --resume-of "$PREVIOUS_TASK_ID"
```

`run`は同期実行。状態取得・中断は別プロセスから呼べる。1つのruntimeでは排他ロックで同時実行を拒否する。
新規・再開ともread-only固定、既知のタスクのみ再開可能。UNKNOWNを自動再実行せず、終端タスクのrun再送も拒否する。
詳細な引数、DB schema、終了コード、記録先は[README](../README.md)。

## 5. テスト結果

### オフライン

`python3 -m unittest discover -s tests -v` は **22件成功**。
プロトコル部分は模擬peerを使うが、子プロセス、signal、JSON parser、SQLite、ファイルロックは実際に使用している。
モデルやCodexの動作をこの結果だけで保証していない。カバレッジ指標の測定は行っていない。
結果: `.runtime/evidence/unit-tests-final-22.txt`。

### 実機

| 要求項目 | 結果と検証範囲 |
| --- | --- |
| 正常実行 | 成功。ファイルマーカーを別コードで照合、COMPLETEDと結果を取得 |
| 起動失敗 | 存在しない実行パスをFAILEDとしてSQLiteに記録。CLIの不正設定終了コード1も確認 |
| 不正イベント | 模擬peerの不正JSON・不正構造をUNKNOWNとして記録、解放・再実行拒否を確認。実Codexのイベント破損を発生させる試験は未実施 |
| タイムアウト | 実Codexがsleepコマンドを開始した後、9秒期限でTIMED_OUT。interrupt・解放、稼働中残存なし |
| 中断 | 実Codexのコマンド開始後、別SQLite接続から中断要求。CANCELLED、interrupt・解放、稼働中残存なし |
| 保存再開 | 元プロセス終了後に同一threadへ再開。前ターンのnonceを新入力に再掲せずに取得 |
| DB再読込 | 別Store接続とCLI showから保存状態・結果を取得 |
| 再実行防止 | UNKNOWNと終端タスクのrunを拒否。UNKNOWNからのresumeも拒否 |
| 権限拒否 | read-onlyの直接command/execがexitCode=1、書込先未作成。モデルの失敗報告も最終schemaのfailureによりFAILED |
| 親死亡・子孫解放 | オフラインで親死亡guardと別process groupの子孫解放を検証。実機では制御された中断・期限で解放を検証 |

採用ランナーの最新実機5ケースはすべて受入条件を満たした。
通常22.499秒、再開7.912秒、期限9.095秒、中断10.922秒、権限拒否9.952秒（解放込み）。
最終コードの追加確認でも同一セッション・nonceを保持した再開がCOMPLETEDとなった。
CLIフロントエンドのsubmit/show/cancel/run拒否/recoverも、モデル生成なしで確認した。

一部の事前拒否ではcommandExecutionイベントが出なかった。モデルの「実行した」という文章を実行証拠にはせず、別の直接command/execで実際のOS拒否を確認した。
COMPLETEDの最終outcomeもAIの自己申告を含むため、任意タスクの意味的成功の保証ではない。

### 失敗を保持した修正

stdin未閉鎖、nested namespace拒否、拒否結果の誤分類、namespace解放直後の一時的残存判定を修正した。
namespace前提失敗後に誤って開始した実験は停止し、経緯を保存した。
初版の権限拒否タスクは誤分類されたCOMPLETEDとして履歴に残しており、後から履歴を成功したように書き換えていない。
修正前の記録と修正後の受入結果を分けて保存している。

## 6. 未検証事項

- Windows/macOS、他のCodex版、keyring認証、認証の更新・失効、アカウント・利用枠不足。
- 実ネットワーク切断、OS強制終了・再起動、実Codexが不正JSONを出す状況、大量出力・負荷。
- 任意タスクの成果物や意味的正しさを自動判定する仕組み。
- 複数セッション並行、daemon、ローカルLLM、ChatGPT/MCP接続。これらは未実装。

APIキーは使用していない。観測したturn開始は修正確認を含め22件、うち9件は中断・期限で停止した。
初回CLIのcleanup中に意図せずturnが開始した1件も含めて数えた。
17件分の使用量報告を保存し、利用率は同じ7日窓で16%から17%へ変化した。ただしこのチャット等の利用と分離できず、本実験の正確な消費率には換算していない。

## 7. 既存環境の保持確認

`~/.codex/config.toml`の開始・終了SHA-256が一致した。
認証、設定、グローバル規約、Phase 0報告3件、history.jsonl、session_index.jsonlについて、size・mtime・mode・inodeがすべて一致した。
認証内容と既存セッションDB・IPCはPythonから直接読書きしていない。認証はCodex標準処理にread-only mountで渡した。
ホスト側の専用auth.jsonはどちらのruntimeも0 bytesのマウントポイントであり、認証コピーではない。
既存daemonの接続・停止・更新、既存セッションの再開・削除、状態DBの直接操作、製品リポジトリ変更は行っていない。

このチャット等で共有CODEX_HOMEのDB・ログが通常更新される可能性はあるため、ディレクトリ全体の全バイト不変を主張しない。
保持の証拠は`.runtime/evidence/preservation-before.json`と`preservation-after.json`。

## 8. Phase 2への推奨事項

1. 現在のruntime・schema・受入条件を固定し、ネットワーク断、認証失効、強制終了後の照合を追加する。
2. タスク固有の完了判定と、UNKNOWNを人間が照合・解決する手順を定義する。
3. 複数実行が必要と確認できた場合に、作業ツリー分離、資源・利用枠上限、セッション所有権を設計する。
4. daemon化・永続worker・ChatGPT用の限定MCP窓口は、その後に個別の受入条件と認可境界を決めて実装する。
5. OSS公開前に、ライセンス、秘密・第三者知財、公開可能なログ、提供形態に応じた認証の利用条件を確認する。

上記は提案のみ。Phase 2の実装は行っていない。

## 完了条件

外部プログラムからのCodex実行、状態追跡、JSONイベント保存、結果永続化、保存セッション再開、タイムアウト、中断、再現可能なテスト、既存環境の保持確認を実施した。
実機未検証の障害条件は上記に残し、試験していない項目を成功扱いしていない。
