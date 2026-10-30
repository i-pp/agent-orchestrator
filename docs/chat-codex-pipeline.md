# Chat ↔ Codex pipeline: local opt-in candidate

既存 AceIS MCP Dev の `mcp_adapter.py` に追加する候補。新しいorchestrator、ネットワークlistener、認証基盤は作らない。既定OFFで、既存11操作のschemaは変更しない。現用daemon・tunnel・サービス設定への反映、実モデル起動、Chatとの実往復は未実施。

## 確認した実APIと責務

導入済み `codex-cli 0.165.0` の `app-server generate-json-schema` と `app-server --help` を確認した。対応schemaのhashは `src/codex_app_protocol.py` に固定。公式説明は [Codex app-server](https://learn.chatgpt.com/docs/app-server)。コードはstdio JSON-RPCの `initialize` → `initialized`、`account/read`、`model/list`、`thread/start`、`thread/resume`、`thread/read`、`turn/start`、`turn/steer`、`turn/interrupt`、`thread/archive` を使用する。CLIのschema生成・help確認と、実app-serverの起動成功は別の証拠である。

threadは会話と固定workspaceの単位、taskは1回のturnまたはまだ実行しない入力の単位。transportは明示起動する固定 `codex app-server --listen stdio://` の子プロセス。既存親主導adapterと同様に、実行IDの受渡し・UNKNOWNの照合を管理し、モデルやDAG実行基盤を再実装しない。既存native親adapterは標準Agentツール用、今回のprotocol adapterは公式app-server用で、両者を同じAPIとは扱わない。

modelはthread作成時に初期デフォルトを指定する。taskのmodel指定はそのtaskだけを上書きし、省略時はledgerに保存したthread初期デフォルトを使う。adapterは各turnでmodelを明示するため、app-server側で前turnの設定が継続していても、次の省略taskへ暗黙継承しない。store時は許可list、実行直前はaccount/model catalogも検証する。steerは既存turnへの入力で、model変更は受け付けず、ACKのturnId一致も確認する。

初段は**実行するCodexのfilesystem sandboxをread-onlyにするtask生成**に限定する。MCPのreadOnlyHintだけを指すものではなく、threadに `sandbox=read-only`、turnに `sandboxPolicy.type=readOnly`、両方に`on-request`・`approvalsReviewer=user`を明示し、既存global full-access/no-approval値をそのまま採用しない。MCPの投稿/queue/stop操作自体はwrite操作である。直接のshell/path指定や承認回答ツールは提供しないが、モデルによる既存tool利用までworkspaceキーで完全に隔離できるという保証はしない。read-onlyは書込み制限であり、workspace外の読取りを完全禁止するjailではない。実行前に対象native環境の既存MCP/拡張設定も確認する必要がある。現在の管理sandbox内からの追加隔離作成は禁止のまま。

## 操作対応表

| MCP候補名 | 動作・成功条件 |
|---|---|
| `codex_capabilities` | 許可workspaceキー、上限、モデル一覧/可用性。モデル実行なし。モデル一覧は課金・利用権の証明ではない |
| `codex_thread_start` | 許可modelを検証し、固定workspaceに会話を作る。まだturnを開始しない |
| `codex_task_submit` | 入力を永続FIFOへ登録。空きがあれば1turn開始、実行中ならqueue |
| `codex_chat_store` | immutable入力を保存するだけ。推論しない |
| `codex_chat_queue` | 保存入力をqueueへ移す。明示tickで実行 |
| `codex_chat_steer` | 現在turnの `expectedTurnId` に追加入力。model変更不可、暗黙steerなし |
| `codex_threads_list` | このledgerが所有するthread/taskのみ。回答・生remote IDは一覧に含めない |
| `codex_task_get` | 1taskと最大4KBの回答。既知thread/turnを読み戻して完了を照合 |
| `codex_events` | durable cursor以降のイベントを最大50件。完了/承認待ちをpollで回収 |
| `codex_task_cancel` | 未実行入力はlocal cancel。実行中はinterruptを1回要求し、実停止はterminal event/readbackで確認 |
| `codex_thread_archive` | 未解決入力のない会話をarchive。履歴・ledgerを削除しない |
| `codex_pipeline_tick` | 期限/状態照合と最大1件のFIFO dispatch。常駐wake/schedulerではない |

新12操作を有効化すると既存11＋12＝23操作。入力schemaは追加fieldを拒否し、APIへ任意path・URL・shell・image・provider・configを渡す操作はない。承認待ちイベントには自動承認を付けない。承認解決UIへの接続は未実装で、待機・取消・人間への通知に留まる。

## 重複・再接続・期限

UUIDのlocal IDとapp-serverのopaque IDをledgerで対応付ける。request_keyと内容hashをeffect前にtransactionで固定し、同一内容は既存receipt、違う内容はCONFLICT。同じkeyでもUNKNOWNは再送しない。dispatch開始・ACK不達・ローカルcommit失敗・crash後のISSUEDはUNKNOWNとして枠を保持する。thread/startのACKでremote IDを失った場合は自動で別threadを作らない。

SQLiteのprivate ledgerは、入力・状態・receipt・イベントをFULL同期transactionで保存。入力と回答各4KB、thread最大8、task最大32、同時turn最大2、論理期限最大300秒、operation最大512、event最大4096、JSON状態最大2MiB、DB最大8MiB。上限はlifetime上限で、自動削除・無期限拡張はしない。config例はさらに小さく、4thread・16task・同時1・120秒。policyまたは共通policy hashの変更を検出したら既存ledgerの流用を拒否する。

期限は常駐OS watchdogではない。明示tick時に経過を検知し、interruptは1回だけ。停止確認が取れなければSTOP_REQUESTED/UNKNOWNを保持し、枠を解放して次の推論を始めない。接続切断後は同じledgerと既存Codex保存状態から既知IDだけを照合し、thread/resumeにも固定scopeを指定する。接続自体が壊れたら新しい明示接続が必要で、書込みの自動再送はしない。

完了/承認通知はstdioをbounded pollし、受理済みイベントをcursorで再取得できる。起動ACKより早い完了は回答部分のみ保留し、返されたturn IDが一致した場合に採用する。archive中/UNKNOWNはthreadをclosingとして保持し、別入力の実行を拒否する。要求受理と実停止/実archiveの確定を混同しない。

## ローカル候補の準備と有効化境界

`config/codex-pipeline.example.json` は可搬なテンプレートで、そのままでは起動しない。`<PROJECT_ROOT>`は運用者が選んだ既存の正規絶対path、modelは導入app-serverの一覧と突き合わせた明示許可IDへ置換する。workspace値は絶対pathのみで、`~`展開・相対path・symlinkを受け付けない。runtime指定は既存adapter同様、repo内`.runtime`下の0700 ownerディレクトリ、policyは`codex-pipeline-policy.json`として0600 owner regular fileにする。共通policyは既存 `load_project_policy` でhash/commit検証して全文注入し、本文を複製管理しない。

**次のコマンドは承認後の実行案であり、今回実行していない。** `PROJECT_ROOT` と `STATE_DIR` は実際の配置へ明示設定する。

```bash
python3 -B "$PROJECT_ROOT/src/mcp_adapter.py" \
  --runtime "$STATE_DIR" --allow-codex-pipeline
```

これはローカルstdio候補を起動する入口。既存11操作の各flagは別途維持が必要。専用service CLI・login自動復帰への新flag継承、既存tunnel経由での23操作公開はまだ組み込んでいないため、このコマンドだけで現用サービスが拡張されたとは扱わない。イントラ移設はtransport/executorを分けたまま、秘密・local policy・runtimeをGit対象外に置く。今回HTTP公開、新キー、allowlist拡大はしない。

明示起動時はaccount/readを `refreshToken=false` とし、返値はaccount typeだけへ射影。ChatGPT認証と、thread実効provider=`openai`を確認してからtaskを実行する。新規login/key/provider設定はしない。料金額・プラン内の実精算はこのAPIでは検証できない。

公開承認に必要な項目は、上記12tool、実際のworkspace、実機一覧から選ぶmodel ID、例の4/16/1/120上限、同じMCP認証で全対象依頼/回答へアクセスできる範囲、native executorの既存tool設定、tick担当・頻度、停止/復旧手順。現時点ではworkspace/modelの最終選択と実機probeが未確定なので、有効化済みと報告しない。

rollbackは新flagを外して旧11操作の起動引数へ戻す。実行中taskがあれば先にstop要求とterminal照合を行い、UNKNOWNのまま二重起動しない。runtime DB・既存mailbox/grant・監査ログ・キー参照を保持し、削除しない。サービス反映とrestartには別の具体的な承認が必要。

## 検証と未接続

オフライン検証は実 `ProtocolClient` とfake app-server、private SQLite、MCP Adapterを通す。protocolのfield/schema照合、store/queue/steer、FIFO、同一key競合、並行予約、ACK不達/crash、期限、stop未確定、archive排他、早期完了、reconnect/resume、承認通知、payloadを含まない監査、既存mailbox保存を確認する。app-server subprocessのIO/回収はfake Popen/pipeで検証する。namespace・Docker・GPU・実モデルは使わない。

2026-10-30の独立レビューで、steer ACKのturnId不一致と起動ACK前の承認要求の取りこぼしを検出し、両方を修正・fixtureで検証した。初期ACK前のturn ID欠落も成功/承認待ちへ採用しない。

同日の許可済みmetadata読み取りprobeでは、通常の管理exec内で固定stdio app-server子プロセスを1回起動したが、initializeのACKを確認できずUNKNOWNで終了。account/readとmodel/listは送っておらず、認証種別・実モデル一覧・利用権・課金区分は未確認。子プロセスは回収済み、推論要求0、再試行0。stderrを破棄する現transportのため根本原因は未特定で、help/versionで見えたread-only PATH alias警告だけから認証/権限障害と断定しない。host native環境との同等性も未確認。保護領域・別credential・別実行権限へ切り替えていない。読取りprobeとturnを伴う追加sandbox作成を区別し、後者は実施しない。

| 成功条件 | 今回の範囲 |
|---|---|
| Chat→MCP→Codex→回答回収 | オフライン組立てを検証。実Chat/実Codex往復は未実施 |
| Codex→Chat起動 | 未接続。mailboxもapp-serverも休止Chatをwakeしない |
| mailboxへの回答保存 | 既存明示APIを使うオフライン連携PASS。実Chatによる保存は別確認 |
| モデルのファイル編集・実行 | 初段read-onlyでは対象外。write scopeと実行環境の追加承認が必要 |
| 常駐通知・自律継続 | 未実装。events/tickの明示pollが必要 |

逆方向の既存UI経路は、導入済みCodex app toolの `send_message_to_thread` が既存ChatGPT会話を対象にできる契約を確認した。ChatGPT宛にはmodel/thinking overrideは適用されない。送信には宛先会話へのユーザー明示承認が必要で、今回メッセージは送っていない。`read_thread`は現在の実機確認で、既存Chatの過去回答495文字を省略なしに取得できた。以前の本文参照だけという制約と区別し、この1件での本文回収成功を記録する。他会話や実行中turnの回収まで保証しない。UI toolはこのlocal stdio executorやイントラMCPから呼べるHTTP APIではなく、自動wake bridgeは未実装。実際に許可されたUI側Codexが既存Chatへ投稿し、Chatが既存mailbox APIで回答を保存する経路を再利用する案はあるが、今回の12toolだけでその両端が自動接続されたことにはならない。

秘密・顧客情報を依頼へ入れない。prompt/answerはprivate ledgerと該当get結果に含まれるが、audit/eventsへ本文・コマンド・credential・workspace実pathを記録しない。ID/hashはChatのidentity証明ではない。
