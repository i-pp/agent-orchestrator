# ChatGPT接続設計 — Phase 2-E1

確認日: 2026-10-28（Asia/Tokyo）。今回はローカル実装・試験と公式資料の確認まで。
ChatGPT接続、Platform API呼出し、tunnel-client取得・起動、キー発行、権限・契約・課金設定変更は実施しない。

## 2026-11-05追記

以下はPhase 2-E1時点のstdio接続設計です。その後、別の隔離ブランチにHTTP/Events通知の統合候補を作成し、オフライン試験を実施しました。本番repoやTunnel構成には未反映で、実callback・ChatGPT会話再開は未確認です。Owner限定の実効アクセス範囲を確認できるまで通知は無効のままとします。候補の現在状態と導入・切戻し案は[MCP Events導入設計](mcp-events-deployment.md)、[実行時安全性](mcp-events-runtime-safety.md)を参照してください。

## 採用する境界

```text
将来: ChatGPT web → OpenAI Tunnel → tunnel-client
                                         ↓ stdio（今回は未接続）
今回: 模擬MCP client → mcp_adapter.py → 固定Unix socket → 既存daemon → 既存runner
```

アダプターはPython 3.14標準ライブラリだけを使用し、HTTP listenerを作らない。
新たな認証サーバー、scheduler、DB schema、lease、モデル呼出し、自律的な外部指示処理ループは追加しない。
アダプターは既存daemonを起動・停止せず、既存のUnix socket clientを使う。

## 公式経路・認証・権限の確認結果

| 論点 | 公式資料で確認したこと | この利用環境での確認 |
| --- | --- | --- |
| Secure MCP Tunnel | private MCPへ外向きHTTPSで接続。stdio／HTTPのローカル接続に対応 | 未接続。tunnel未作成 |
| tunnel-client認証 | tunnel IDとruntime APIキーが必要 | キー未要求・未発行・未使用 |
| Platform組織権限 | 作成・変更はTunnels Read＋Manage、運用・選択はRead＋Use | アカウントの権限未調査 |
| ChatGPT側権限 | custom MCPの追加・利用はworkspace policyとsecurity restrictionsに従う | Business workspaceの実際の利用可否・管理者許可・Lockdown等は未確認 |
| 組織の関連付け | 所有Platform組織に加え、利用対象ChatGPT workspaceとの関連付けが必要 | 関連付け未確認・未変更 |
| Business契約 | 共通のcustom MCP資料はworkspace側制約とPlatform側権限を別に扱う | Business契約だけでTunnel利用権限があるとは断定しない。対象workspaceでの提供条件も確認が必要 |

Tunnel経路・stdio対応・認証・Tunnels権限・workspace関連付けの根拠は
[公式Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)。
アカウント／workspaceの実値を公式仕様だけで推定しない。

ChatGPT webのcustom MCP画面はURLまたはTunnelを選び、MCPサーバー側の認証方法を設定する。
直接のURL接続で扱うHTTP系transportと、Tunnelがローカルstdioを中継する構成を区別する。
[公式custom MCP接続](https://developers.openai.com/api/docs/guides/custom-mcp-server)

### キーと料金を混同しない

- Codexタスク本体は、従来どおり既存ChatGPTサブスクリプション認証を使う。今回のモデル生成は0回。
- Tunnelのruntime APIキーは接続control planeの認証に必要であり、CodexのChatGPTログインで代用できるとの記載は確認できなかった。
- TunnelのAPIキーが必要なことと、Responses API等によるモデル従量課金を使うことは別。今回の実装はResponses APIを呼ばない。
- 参照したTunnelガイドと[公式API料金ページ](https://developers.openai.com/api/docs/pricing)では、Tunnel単独の料金・Business契約への包含・課金有効化要否を確定できなかった。
  無料、サブスクリプション内、課金不要のいずれとも断定しない。組織契約・管理者・必要ならOpenAI窓口で確認してから次工程を判断する。
- サーバー側OAuth等と、Tunnel runtimeキーと、ローカルUnix socketのUID認証は別の層。キーをMCP応答やChatGPT接続フォームへ転載しない。

## MCP実装範囲

プロトコルは明示的に**2025-11-25 revision**へ固定する。
initialize→notifications/initialized→tools/list／tools/call、ping、JSON-RPC 2.0、UTF-8 JSONL stdioを実装する。
広告するcapabilityはtoolsのみ。resources、prompts、sampling、elicitation、MCP Tasks、動的ツール変更は提供しない。
[MCP lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)、
[stdio仕様](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)

2026-07-28 revisionにはstateless request等の変更があることも確認した。
本実装を「最新revision対応」とは扱わない。要求された初期化方式で、小さな既知のrevisionを選んだ。
異なる版を要求するclientには対応版を返し、client側の互換判定を必要とする。
**実tunnel-client／ChatGPTがこのrevisionと広告内容を受け入れるかは未検証で、実接続前の確認項目とする。**
[新しいrevisionの公式仕様](https://modelcontextprotocol.io/specification/2026-07-28)

### 公開ツール

| tool | daemon method | 入力 | 既定 |
| --- | --- | --- | --- |
| orchestrator_status | status | なし | 有効 |
| orchestrator_list_tasks | list | limit 1..50、offset 0..1000000 | 有効 |
| orchestrator_get_task | status | task_id（UUID） | 有効 |
| orchestrator_submit_task | submit | prompt、request_key | 無効 |
| orchestrator_cancel_task | cancel | task_id（UUID） | 無効 |

ツールのinputSchemaと実行時の型・値チェックを併用する。追加フィールド、任意socket／runtime／path、任意shell、
shutdown／pause／設定変更／削除／UNKNOWN解決／resumeは公開しない。
readOnlyHint等を正しく広告するが、annotationsは権限制御の代用にしない。
無効なwrite toolはtools/listから除外し、直接tools/callしても拒否する。
[MCP Tools仕様](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

### 書込みはローカル試験限定

operatorが起動時に`--allow-writes`を指定し、既存runtime直下の`mcp-task-policy.json`を用意した場合だけ有効にする。
policyはowner UID・regular file・mode 0600相当・16 KiB以内を要求する。
固定する値はworkspace、acceptance、timeout。clientからこれらを上書きできない。

```json
{
  "workspace": {"fixture": "seed", "access": "read-only", "role": "implementer"},
  "acceptance": {"human": true},
  "timeout": 120
}
```

これは手動確認待ちにする例で、自動的なタスク成功を意味しない。
通常の自動検証にはoperatorが成果物hash、既存registryのtest ID等を指定する。
daemonは既存Store.submitで受入条件とworkspaceを再検証し、workspace-writeには従来のhash＋許可済みtestを要求する。
このPhaseのMCP試験ではread-only fixtureのタスク投入・中断だけを実行する。
**ChatGPT試験では--allow-writesを付けない。ChatGPTからの書込み実行は別承認まで行わない。**

### 応答・ログ・期限

- 応答は状態、task／execution／thread／turn／workspace ID、検証状態、schedulerの件数等の許可したmetadataに限定。
  task本文、モデルの自由文result、error、acceptance内容、config、認証、環境、owner、ローカルパスは返さない。
  未知の状態文字列・不正なIDをそのまま転載しない。詳細結果は既存のローカルCLIで確認する。
- tools/call成功はMCP操作の成功。submitは受付の成功であり、task COMPLETEDでも検証・worker解放の確認とは別。
  completion_verifiedの既存判定を保持する。失敗時のdaemon例外本文を応答せず、isErrorと固定codeを返す。
- request_keyは必須。同じ値・同じタスク定義の再送は既存daemonが同じtaskを返す。入力が異なれば拒否する。
  切断・timeout後に新しいkeyで盲目的再投入しない。中断や通信断が副作用を巻き戻すとは扱わない。
- MCP frame／応答は各64 KiB、promptは4,096文字かつ16 KiB以内。重複JSONキー、非有限値、不正UTF-8、batch、不正構造を拒否する。
- daemon要求は全体2秒。既存clientのreceiveが小分けの受信で期限を延長しないよう、残り時間を各I/Oへ設定する。
  stdout書込みは2秒、途中で切れた入力は3秒。入力待ちだけのidleは終了させない。
- runtimeはoperatorが起動時に固定する。PROJECT/.runtime配下、symlinkなし、owner UID・0700のroot／logsを検査する。
  endpointとSO_PEERCREDは既存clientが検証。遠隔の利用者にローカルoperatorを超える権限を与えない。
- logs/mcp-SESSION_UUID.jsonlへcall_id、RPC IDのhash、tool名、request_keyのhash、task ID、取得できたexecution ID、開始／終了を記録する。
  submit時にexecution未作成でもtask IDから後続のget_taskとdaemon DBへ辿れる。入力本文や生のrequest_keyは保存しない。
  1ログ4 MiB、監査の永続化不能・残量不足では新規操作を拒否。自動削除なし。総容量は既存の監視予算でありhard quotaではない。
- アダプターを終了・切断してもdaemonタスクを一括停止しない。個別中断にはcancel toolを使う。
  ツール説明・返された文字列を新しい実行指示として処理するループはない。

## --version補助経路の独立変更

D2.1で指摘したidentityと実行前の2箇所を、binary_version helperへまとめた。
既存isolated filesystemを用い、専用HOME／CODEX_HOME、認証mountなし、network namespaceありで--versionのみを実行する。
非/usrのインストール済みCodexをno-authで許す例外は引数が正確に`["--version"]`の場合だけ。
App Serverや任意の引数へこの例外を使えず、失敗時のホスト実行fallbackもない。
実Codexの版表示と隔離境界をモデル生成なしで検証した。D2の設定更新原因は利用者のアプリ操作であり、この補助経路と混同しない。

## 次工程の導入手順案（未実施・承認後のみ）

1. 対象ChatGPT Business workspaceとPlatform組織を特定し、custom MCPの利用権限、Tunnels Read／Manage／Use、関連付け可否を管理者が確認する。
2. Tunnelの提供条件・単独料金・課金設定要否・契約を確認し、runtimeキーの発行範囲・管理方法を承認する。既存subscription認証を代用しない。
3. ローカルの専用runtimeを準備し、実データを含まないmetadataだけが読み取り3ツールから出ることを確認する。
4. 公式配布tunnel-clientの対象版・署名等を確認する。起動前に当該版のhelpと2025-11-25互換性を確認する。
5. operatorがTunnelを作成または既存の適切なものを選び、workspace／組織へ関連付ける。管理者がrestricted runtimeキーへRead＋Useを付与する。
6. 秘密値はローカルの安全な方法で`CONTROL_PLANE_API_KEY`へ渡す。chat、CLI引数、設定ファイル、Gitやログへ値を記載しない。
7. 下記の公式ガイドのコマンド形を、実機helpと承認内容に照合して使う。**今回はどれも実行していない。**

```bash
tunnel-client help quickstart
tunnel-client init --sample sample_mcp_stdio_local --profile orchestrator-readonly \
  --tunnel-id <approved-tunnel-id> \
  --mcp-command "python3 -B /absolute/repo/src/mcp_adapter.py --runtime /absolute/repo/.runtime/approved-readonly"
tunnel-client doctor --profile orchestrator-readonly --explain
tunnel-client run --profile orchestrator-readonly
```

8. readiness／pollingを確認する。tunnel-client自身には既定loopbackの管理UI等があるため、そのbind／権限も確認する。外部公開やsystemd導入を自動で追加しない。
9. ChatGPT webのcustom MCP接続でTunnelを選択し、承認済みID、サーバー認証を設定する。TunnelキーとサーバーOAuthを混同しない。
10. tool discoveryで読み取り3件だけを確認し、status／list／getを手動試験する。MCP call_id→task／executionの証跡を照合する。
11. 初期化・revision・料金・関連付け・権限のいずれかが不明なら接続完了扱いにしない。書込み機能のChatGPT公開は次の別判断とする。

上記のtunnel-client引数の根拠は[公式ガイドのstdio例](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels#set-up-tunnel-client)。
このrunbookは将来の案で、ローカル成功を実ChatGPT接続成功として報告しない。
