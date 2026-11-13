# タスク完了判定設計

プロセス終了、Codexのcompleted、モデルのsuccess、受入検証、人間承認を別々に扱う。
成果物やテスト結果をモデルの説明だけで確定しない。

## 判定の分離

| 項目 | 根拠 |
| --- | --- |
| Codex実行 | thread/turn、対応する完了通知、終了コード、解放、保存結果 |
| 成果物 | workspace内通常ファイルの存在・サイズ・SHA-256・期待値 |
| テスト | 許可ID、固定argv、期限、終了コード、除去済み出力、解放 |
| 固有条件 | result_equals/result_containsの指定値、判定、結果ハッシュ |
| 人間判断 | actor、理由、decision_id、有効な根拠とworkspace fingerprint |

Codex正常終了でも、成果物欠落・テスト失敗ならFAILED。
正常結果はまずVERIFYINGへ進め、根拠と最終状態を同じDBトランザクションに保存する。
検証中の親停止はUNKNOWNであり、保存済みCodex実行SUCCEEDEDを保持する。

## 受入条件

operatorが投入時にJSONを指定する。

- artifacts: path、任意のsha256。
- tests: 許可ID配列。
- conditions: kind（result_equals/result_contains）と非空value。
- human: 人間判断の要否。

未知のキー、未許可コマンド、境界外path、無効な型・hashを拒否する。合計30項目まで。
受入条件とコマンドsnapshotをSQLiteへ保存し、呼出元から後で変更できない。
条件未指定や結果文字列だけの検査は人間判断待ち。自動検証の不合格を承認で上書きできない。

自動COMPLETEDには、正常なCodex結果、モデルの成功報告、失敗・拒否なし、独立した成果物またはテスト、全条件合格、解放を要求する。
outcomeも生成物であり独立検証の代用ではない。存在だけの条件は、新規作成・内容の正しさ・品質を保証しない。

## 境界とコマンド

成果物はruntime/workspaceの相対パスだけ。絶対path、`.`、`..`、symlink、非通常ファイルを拒否する。
dir_fdとO_NOFOLLOWで各要素を開き、文字列prefix検査だけに依存しない。ファイル上限16 MiB。内容をログに保存しない。

コマンドはプロジェクト直下verification-commands.jsonへ事前定義する。
IDから取得したargvを投入時に固定し、実行前にも許可定義と一致を確認する。
モデルが出したコマンド・引数・作業場所は採用しない。実行パスは絶対、シェルは拒否、shell=Trueは使わない。1コマンド60秒以内。
登録プログラム自体はoperatorが信頼・レビューする対象であり、registryは署名・認証の仕組みではない。

cwdはworkspace固定。別のuser/PID/mount/network namespaceでroot filesystemをread-onlyにし、認証領域を隠し、専用tmpfsを使う。
OS依存ファイル等の読取りは可能であり、任意プログラムの全読取りをworkspaceだけに限定する仕組みではない。
認証情報を読む検証は登録しない。既存環境への書込み・権限拡張へフォールバックしない。

## 期限・中断・人間判断

runの期限はCodexと検証を含む。コマンド固有の期限も適用し、どちらかに達すればTIMED_OUT。
検証中もcancelを受け付ける。解放・起動checkpointが不確実ならUNKNOWN。
復旧時に明示する再検証は新しい60秒予算で行い、Codexを生成しない。

人間承認には自動検証合格と検証後workspace不変を要求し、actorと理由を保存する。
承認はタスク固有の人間判断を満たすもので、自動検証失敗の無視ではない。
actorはローカルoperatorの申告。外部サービスの認証・認可は今回実装しない。

## 根拠と限界

verifications.evidenceをshowでも取得でき、execution/reconciliation/decisionへIDで接続する。
出力は上限付き・秘密形式除去済みで無加工原本ではない。任意の機密情報の自動検出は保証しない。
manifestは現在の通常ファイル状態だけで、一時的・外部の副作用を証明しない。
意味的正しさを推測するモデル、動的コマンド生成、検証plugin frameworkは追加していない。

## Phase 2-Cでの適用

legacyは従来のworkspace境界を維持する。isolated taskの成果物・テストは所有workspaceだけを対象にする。
検証コマンドも最小rootへpivot_rootし、他workspace・coordinator・CODEX_HOMEを隠し、別network namespaceで実行する。
workspace-writeの投入には明示fixture、成果物内容SHA-256、許可済みtestsを必須とする。存在だけでは受け付けない。
Reviewerはsnapshotの独立コピーをread-onlyで検証し、source taskを自動承認しない。
詳細は[Workspace隔離](workspace-isolation.md)を参照。

## Phase 2-D1の判定

許可コマンドも資源制限下で実行し、制限超過や監督未確認を合格にしない。scope解放が不明ならUNKNOWN。
CPU bandwidthのthrottling自体と、CPU累積予算超過による停止を区別する。検証argvは引き続き固定許可リストのみ。
