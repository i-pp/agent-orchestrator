# Phase 1-A 実行方式比較

パスは[表記規約](path-conventions.md)の疑似パスです。シェル例は同ページの変数を設定してから使用します。原本ログは変更していません。

実施日: 2026-10-28、Asia/Tokyo。実機はUbuntu 26.04.1 LTS / Python 3.14.4。
検証時のバイナリの場所は `<CODEX_BINARY>`、versionは `codex-cli 0.164.0-alpha.11.1`。

## 条件と根拠

- 既存ChatGPT認証をread-only mountし、専用CODEX_HOMEを使用した。APIキー、従量課金API、既存daemonは使っていない。
- 両方式とも同じread-only設定、同じ`fixture.txt`、同じプロンプトを使用した。
- 最初のタスクはファイルの`CERULEAN-17`とnonce `ORCH-CONTINUITY-49`を返すもの。再開時はnonceを入力に再掲せず、前ターンの値を返すものとした。
- CLIのstdinはプロンプト引数があっても追加コンテキストとして読まれるため、直ちにEOFを送った。
- App Serverは毎回新規stdio子プロセスを起動し、再開実験では前プロセス終了後に保存threadをresumeした。
- 時間はプロセス起動から解放までの単発観測。統計的ベンチマークや将来の速度保証ではない。

公式仕様は[非対話実行](https://developers.openai.com/codex/noninteractive)、[App Server](https://developers.openai.com/codex/app-server)、[設定参照](https://learn.chatgpt.com/docs/config-file/config-reference)を参照した。
インストール済みバイナリから`app-server generate-json-schema`を実行し、320件のschemaを取得した。
thread/start、thread/resume、thread/read、turn/start、turn/interrupt、thread/unsubscribeの必須・任意フィールドを実装と照合した。
`--experimental`と`experimentalApi=true`は使用していない。App Serverコマンド自体およびalpha版である点の不安定性は残る。
スキーマと照合結果は `.runtime/schema/`、`.runtime/evidence/schema-check.json`。

## 同じタスクでの観測結果

| 項目 | CLI exec | App Server stdio |
| --- | --- | --- |
| 通常タスク時間 | 6.278秒 | 6.741秒 |
| 再開タスク時間 | 3.853秒 | 3.908秒 |
| 結果 | マーカーとnonceが一致 | マーカーとnonceが一致 |
| セッションID | thread.startedから取得 | thread/start応答から取得 |
| 再開 | exec resume、同じIDとnonceを確認 | 新規プロセスのthread/resume、同じIDとnonceを確認 |
| プロセス終了コード | 通常・再開とも0 | 通常・再開とも0 |
| 残存プロセス | 観測した子孫に稼働中残存なし | 同左 |

CLIのセッションは`01a180aa-537f-70c3-97f1-b061ea2ebd46`。
App Serverのthreadは`01a180aa-7d3f-7351-b450-4fc90ac5997f`。
これらは今回の専用領域に作成したIDであり、既存ユーザーセッションではない。
証拠: `.runtime/evidence/comparison.json`、`.runtime/logs/{cli-normal,cli-resume,app-normal,app-resume}.jsonl`。

## 制御・エラー・将来拡張の比較

| 比較項目 | CLI | App Server |
| --- | --- | --- |
| イベント | JSONLのthread.started、turn.started/completed、item.started/completed等を確認 | thread/turn/item、メッセージdelta、tokenUsage、status通知を確認 |
| 状態監視 | イベントを自分で集計。プロセス存否だけでは成功を判定できない | thread/readでidle/active等を取得でき、ターン状態と分けて監視可能 |
| 保存セッション再開 | 実測成功。再開にもプロセス起動が必要 | 実測成功。新規接続で同じthreadへturnを追加できる |
| 中断 | 今回は所有プロセス群の停止。ターン中断のRPCはない | turn/interruptで中断を要求し、interrupted通知を確認できる |
| タイムアウト | 親側の期限とプロセス解放が必要 | 同じく親側の期限が必要。まず公式interruptを使える |
| エラー | 異常終了コード・JSONL・stderrを統合する必要 | RPC error、turn failure、ツール失敗を区別できる |
| 実装複雑度 | 単発処理は小さい。stdin EOF、イベントと終了コードの照合が必要 | 双方向RPC、相関ID、通知保留、未対応要求の拒否が加わる |
| 複数セッションへの将来性 | 複数プロセスで設計可能だが、状態・制御を外側で補う必要 | thread識別子と明示的状態・中断APIを利用できる。実負荷並列は今回未検証 |

## タイムアウト・中断・異常終了

CLIでは単一の`sleep 60`を指示し、command_execution開始後の停止を検証した。
正常完了とは区別し、親が停止した理由と実際の終了コードを記録した。

| CLI実験 | 結果 | 解放込み時間 | 終了コード | 最終残存 |
| --- | --- | --- | --- | --- |
| 不正sandbox設定 | モデル生成前に起動拒否 | 0.092秒 | 1 | なし |
| 9秒の実行期限 | TIMED_OUT相当の停止理由 | 10.255秒 | -9 | なし |
| コマンド開始後の中断 | CANCELLED相当の停止理由 | 11.545秒 | -9 | なし |

`-9`は所有したnamespace/process群の強制終了であり、タスクの成功を意味しない。
App Serverについては採用ランナーの実機検証で、timeout=9秒のTIMED_OUT（9.095秒）、コマンド開始後のCANCELLED（10.922秒）を確認した。
両方ともturn/interruptを送信し、interrupted通知、unsubscribe、プロセス解放、稼働中残存なしを確認した。
不正RPCの実機検証では、未知のsandbox値にJSON-RPC error `-32600`が返り、モデル生成は始まらなかった。
証拠: `cli-faults.json`、`live-checks.json`、`final-protocol-check.json`と対応JSONL。

## 採用方式

**専用App Server stdioを採用した。**
CLIの正常実行・保存・再開は機能したが、今回求める状態取得とターン中断をCLIプロセス制御で補うと、管理ロジックが増える。
thread/readとturn/interruptを公式に利用でき、将来のセッション管理にも直結することから、ユーザーの優先条件に従ってApp Serverを選んだ。
比較用CLIコードは`tests/`に限定し、本体に複数backendの抽象化は導入しなかった。
公式SDKの導入も今回は不要とした。これはローカル最小実装の判断であり、productionサポートを保証するものではない。

## 失敗と修正を含む記録

1. 最初のCLIはstdinを閉じておらず、90秒の期限までイベントを取得できなかった。cleanupがstdinを閉じた後の猶予中にturn開始が発生し、92.220秒で停止した。ログ再点検でこの意図しない開始も利用件数へ加えた。現在は実行直後にEOFを送る。
2. 最初の外側bwrap構成は内側のCodex sandboxのUID namespace生成を妨げ、App Serverのreadタスクが拒否された。会話resumeだけは成功したが、readタスク成功とは記録していない。
3. 名前空間の前提検証が失敗した後に比較を開始した誤りがあり、当該検証プロセスを停止した。現在は事前確認が失敗した場合、生成へ進まない。
4. 外側をunshareと専用mount設定へ変更した。SYS_ADMINを削除しつつ必要なUIDマッピングを許可し、nested sandboxの非生成事前検証が通った。
5. 権限拒否を報告したターンがcompletedになり、初版ランナーがタスク成功と誤分類した。最終結果のoutcome schemaと独立した失敗判定を追加し、再検証でFAILEDを確認した。
6. CLI強制終了直後の検査では、namespace解放中のプロセスを残存と判定した。後続検査ではすでに終了していたため、上限付きの終了待機を追加し、再検証で残存なしを確認した。

初回失敗の証拠は `comparison-first-attempt.json`、`live-checks-before-outcome-fix.json`、`cli-faults-before-cleanup-wait.json`、namespaceの各probe記録に残している。
失敗した検証項目を削除して合格にしていない。モデルの「実行した」という文章だけを実行証拠にしていない。

## 利用枠と未検証範囲

修正確認を含め、ログで確認したturn開始は22件。うち9件は中断・タイムアウトで停止し、初回cleanup中の意図しない開始1件も含む。
stdio初期化、schema生成、認証状態確認、直接のsandbox拒否検査はモデル生成を使っていない。
観測できた17件分のtoken使用報告を`usage-observed.json`に記録した。未完了CLIの使用量がなく、CLIとApp Serverで集計の範囲も異なるため、単純合計や料金換算はしていない。
同じ7日窓の利用率は観測開始時16%、終了時17%。このチャット等の同一アカウント利用を分離できず、差分を本実験だけに帰属できない。
APIキーの作成・取得・利用は行っていない。

大量出力、ネットワーク断、サービス制限、認証更新・失効、別バージョン、他OS、複数セッションの並行負荷、任意タスクの意味的検証は未実施。
