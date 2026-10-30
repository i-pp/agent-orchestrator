# Phase 2-E1 実施報告

パスは[表記規約](path-conventions.md)の疑似パスです。シェル例は同ページの変数を設定してから使用します。原本ログは変更していません。

実施日: 2026-10-28（Asia/Tokyo）。本書はAIによる実測・公式資料の要約。
対象は`<PROJECT_ROOT>/`。開始commitは指定どおり`2490a45071ef14a85c840c701380f1e683fcb622`。

## 結果と実施境界

Python 3.14標準ライブラリだけで、既存Unix socketを使うstdio MCPアダプターを追加した。
daemonのFIFO、lease、DB schema、状態遷移、検証、workspace、資源制限を再実装していない。
実際のOS processと専用daemon runtime、模擬App Serverを使い、MCPからの投入・状態取得・独立検証・中断・再投入抑止を確認した。
読み取り3ツールが既定。ローカル限定の明示起動で書込み2ツールを追加できる。

ChatGPTへの実接続、Tunnel作成、tunnel-client取得・起動、Platform API呼出し、キー発行、
組織関連付け・権限・契約・課金設定の変更、systemd serviceの導入・起動、外部公開は行っていない。
実Codexのモデル生成は**0回**。実Codexの起動は、認証もnetworkもない隔離内の版表示だけ。

## 公式接続経路の確認

Secure MCP Tunnelを将来の第一候補とする。公式資料でstdioをローカル中継できることを確認したため、
今回のMCPアダプターに外部HTTP listenerを追加する必要はない。
ただし、次の条件を満たしたとは報告しない。

| 条件 | 確認結果 |
| --- | --- |
| Tunnel runtime APIキー | 公式に必要。既存のChatGPT subscriptionログインとは別。未要求・未使用 |
| Platform権限 | 作成管理はTunnels Read＋Manage、運用・選択はRead＋Use。実アカウントの権限は未確認 |
| ChatGPT Business | 対象workspaceのcustom MCP提供条件・利用許可・管理者policy等は未確認。契約だけで権限があると推測しない |
| 組織とworkspace | 対象双方へのTunnel関連付けが必要。実環境の関連付けは未確認・未変更 |
| 料金・課金 | 参照資料からTunnel単独の料金・Businessへの包含・課金有効化要否は確定できなかった。無料とは扱わない |
| サーバー認証 | Tunnel control-planeキーと、サーバーOAuth等、Unix UIDは別層。実接続時の設定は未検証 |
| MCP revision互換 | ローカルは2025-11-25。実tunnel-client／ChatGPTとの互換は未検証 |

根拠: [公式Tunnelガイド](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)、
[custom MCP接続](https://developers.openai.com/api/docs/guides/custom-mcp-server)、
[API料金ページ](https://developers.openai.com/api/docs/pricing)。
認証・料金・権限・workspace条件を確認するまで接続しないという利用者指示を維持する。

## 変更ファイル

| ファイル | 変更 |
| --- | --- |
| src/mcp_adapter.py | MCP stdio、5ツールの上限、schema／値検証、応答の許可項目、監査 |
| src/control_socket.py | 既存socket clientへ全体期限を適用。細切れ受信で期限が延長されるのを防止 |
| src/isolation.py | no-auth／network遮断のbinary_version。非/usrのinstalled Codex例外は--versionだけ |
| src/runner.py | 既存の隔離外版確認をhelperに置換 |
| tests/test_phase2_e1.py | MCP protocol／実OSローカル統合の21件 |
| tests/test_version_isolation.py | 補助版確認の隔離に関する4件 |
| docs/chatgpt-connection-design.md | 境界、公式経路、権限・料金の区別、未実施の導入手順 |
| README.md | ローカルMCPの起動・試験・制約を追加 |
| docs/phase2-e1-report.md | 本報告 |

開始時から未追跡だった`docs/config-change-investigation.md`は前工程の成果物として保全し、
このPhaseの変更・commitに混入させない。その他の既存testの本文やassertは変更していない。

## 実装上の安全性

- MCP inputはUTF-8 JSONL、64 KiB以内。重複キー、非有限値、孤立surrogate、batch、不正構造を拒否する。
- 初期化とtools capability、tools/list／tools/call、pingを実装。2025-11-25 revisionを明示し、2026-07-28対応とは主張しない。
- endpointは起動時の専用runtimeから固定する。clientがsocketやruntimeを選択するフィールドはない。
- operator固定のmcp-task-policy.jsonでworkspace／acceptance／timeoutを宣言する。clientによる弱化・任意パス・resumeを拒否する。
- 既存daemonが受入条件とworkspaceを再検証する。アダプターはモデル文をshellとして実行しない。
- 読み取り既定では書込みtoolを広告せず、直接呼出しも拒否する。shutdown・設定・削除・UNKNOWN解決を公開しない。
- 結果は状態・ID・検証metadataだけ。タスク本文、生成結果、例外本文、設定、認証、環境、ローカルパスを転載しない。
- 成功したsubmitは受付にすぎず、task成功を意味しない。通信断後は同じrequest_keyで確認し、盲目的な新規投入・自動再試行をしない。
- MCP call_id→task ID→execution IDを監査ログから辿れる。audit永続化不能時はdaemon操作を開始しない。
- 監査は4 MiB／process、daemon I/Oとstdoutは全体2秒、部分入力は3秒。自動pruneは追加しない。
- --versionの2箇所は専用HOME／CODEX_HOME、認証なし、network遮断の隔離へ移した。失敗時のホスト実行fallbackはない。
  D2の設定更新原因は利用者のアプリ操作と確認されており、本変更の原因と混同しない。

詳細な仕様・制約は[接続設計](chatgpt-connection-design.md)を参照する。

## テストと実測

```bash
python3 -B -m unittest discover -s tests -v
PYTHONPATH=tests python3 -B -m unittest test_phase2_e1 -v
PYTHONPATH=tests python3 -B -m unittest test_version_isolation test_runner -v
```

- 開始時の既存回帰: 123件成功、120.400秒。
- 隔離変更の対象試験: 26件成功、3.578秒（新規4＋既存runner）。
- MCP最終対象試験: 21件成功、9.734秒。
- 全体1回目: 148件成功、133.027秒。入力検査修正後の全体最終結果も**148件成功、133.754秒**（既存123＋新規25）。
- 元の123件は、開始commitと現在のtestメソッドASTを照合してすべて同一。削除・skip・assert緩和はない。
- 実Codexの版表示: `codex-cli 0.164.0-alpha.11.1`。実パスは
  `<CODEX_BINARY>`。
  no-auth／network隔離で成功し、このprobeの前後でグローバルconfig hashは一致した。

| 試験 | 結果 |
| --- | --- |
| initialize／tools/list／tools/call／ping／エラー | 模擬MCP clientと実stdio processで成功 |
| read／write境界 | 既定3件、明示時5件。隠したtoolの直接呼出しも拒否 |
| 入力schema・値 | boolを整数として渡す入力、範囲外、追加path、受入条件上書き等を拒否 |
| 不正JSON・過大入力・途中切断 | 有限エラー／終了。正常な後続frameを処理可能 |
| 期限 | 無応答daemonと細切れsocket応答で期限を延長せず失敗 |
| 応答長 | 小さくした試験上限でtools/listを拒否し、有限RPCエラーを返す |
| 秘密情報境界 | task／daemon応答に混ぜたprivate文字列を応答・auditへ転載しない |
| 正常ローカル統合 | submit→専用daemon→模擬App Server→hash＋許可済みtest→completion_verifiedを確認 |
| 重複・消失ack | 同じrequest_keyで同じtask。adapter stdout断後も重複投入なし |
| 個別cancel | pauseした専用キューの対象taskだけCANCELLED |
| UNKNOWN残存 | UNKNOWNを表示し自動再実行なし。unsafe UNKNOWNがあると新規QUEUEDはdispatchされない |
| daemon停止 | 固定のtool error。勝手なdaemon起動や別endpointへのfallbackなし |
| audit不能 | 操作前に拒否し、daemonへ要求しない |
| version隔離 | 元config・制御socketが見えない。専用HOME、no-auth、no-network。失敗時fallbackなし |

### 検出した失敗と修正

UTF-8等の検査を追加した後の対象試験で1件失敗した。
stdio frameはbytearrayだったがdecoderの明示UTF-8変換がbytesだけを対象とし、Pythonの自動文字コード判定が残っていた。
bytearrayにも明示変換を適用し、UTF-16 fixtureはASCII改行で区切った1frameとして構成した。
同じ試験の拒否条件を保ったまま、21件の再実行が成功した。
失敗原出力`mcp-final-tests.txt`と修正後`mcp-final-tests-2.txt`を保持し、失敗を削除して成功扱いしていない。

## Git・証跡・既存環境

開始時のtracked作業ツリーはclean、未追跡の前工程報告1件、remoteなし。
--versionの隔離はMCPと独立した小変更としてcommitを分ける。
隔離変更のcommitは`735d842972aaa66fb1df4ff84d75b244521ecc08`。
テスト後に登録予定blobのpath・秘密情報パターン・差分を監査し、既存SSH署名設定でローカルcommitを作成する。
秘密鍵を読む・署名を無効にする・履歴を改変する操作は行わない。
commitと署名確認結果はGit履歴とGit対象外の`.runtime/phase2-e1-evidence/commit-result.json`を参照する。

証跡は`.runtime/phase2-e1-evidence/`へ保存する。
baseline.json、baseline-tests.txt、version-tests.txt、native-version-probe.json、regression-check.json、
mcp-tests-1.txt／2.txt、mcp-final-tests.txt／2.txt、final-tests-1.txt／2.txt、Git監査・保全結果を区別する。
原本試験出力とAI要約の本書を混同しない。秘密情報を含む設定本文・認証本文・環境全体は記録しない。

保護対象の最終照合は終了時のpreservation-check.jsonへ記録する。
実測終了時点ではglobal configのhashと、config・auth・AGENTS・history・session_indexのsize／mtime／mode／inodeが開始時と一致した。
systemdの既存service状態はLoadState=not-found、ActiveState=inactiveであり、導入・起動していない。
既存daemon／IPC、Codex内部DB、認証、global configを意図的に変更していない。
GitHub作成・remote登録・push・外部公開、systemd service導入は行わない。

## 未検証事項と次工程の判断

未検証: ChatGPT上のtool discovery／呼出し、tunnel-clientの互換と実動、Business workspaceの提供条件・管理者policy、
Platform組織の権限と関連付け、runtime APIキー、Tunnelの契約・料金・課金設定、サーバー認証の実接続、遠隔writerの利用者認可。
ローカル試験からこれらを成功と推定しない。

次工程では[具体的な導入手順案](chatgpt-connection-design.md)の条件確認から始める。
APIキー・Tunnel・workspace接続の操作は、権限・料金・関連付けを確認したうえで別途承認対象にする。
初回は読み取り3ツールだけ。ChatGPTからの投入・中断は別承認後に行う。
