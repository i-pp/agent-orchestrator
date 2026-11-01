# 初期MCP反映案の履歴

この文書は初期承認時点の案を残したもの。現在の実証結果、追加承認済み資源枠、接続寿命修正、残る検証は [pipeline-status.md](pipeline-status.md) を参照する。以下の初期資源値を現行設定として使用しない。

利用者が通常hostで実行した単発harnessは、gpt-6.1-solの1 thread・1 task・主turn開始1回で、`PIPELINE_SMOKE_OK`を4.88秒で取得した。COMPLETED、retry 0、held task 0、回収確認済み。これはhostのrequest/task/responseの成功であり、daemon環境や外部MCPからの経路の実証ではない。

追加制限profileは別保存して後回しとする。daemon候補は同じ固定binary/argvを使い、起動cwdを許可workspaceへ固定し、通常RPC待ちをharnessと同じ10秒にそろえる。既存のqueue/ledger、shared runner lease、stop/UNKNOWN、service/login flag伝播を再利用する。新しいexecutor基盤は作らない。既存11toolのschemaは維持する。

## 承認対象

- Runtime policyはworkspace 1、model gpt-6.1-solのみ、thread 1、合計task 1、同時主turn 1、論理120秒、再試行0、入力/回答各4KB。1件はlifetime上限で、成功後も自動reset/拡張しない。
- 既存11＋pipeline 12＝23toolを、同じMCP認証へ公開する。追加toolの正確な一覧は`chat-codex-pipeline.md`。4件のreadOnlyHint＋8件のwrite hintであり、hintはexecutorの権限分離ではない。
- 同じ認証を使う呼出元が許可scopeの依頼・回答を扱える。promptはAPI上は任意の4KB以内で、検証用固定promptの強制ではない。store/queue/steerも公開され、steerで120秒期限は延長しない。任意shell/path/config/auth操作を直接受け付けるRPCは公開しない。
- thread/turnにread-onlyとon-request/userを明示するが、既存MCP/apps/plugins/skills/memoriesは継承し得る。connectorの外部書込みをfilesystem sandboxで防ぐとは保証しない。ChatGPT認証は料金・利用枠の保証ではない。
- 新credentials、新port、host security変更、linger変更、local model/GPU操作は含めない。既存daemon 512MiB、tunnel 256MiB、swap 0、TasksMax 64のまま。headroomは実環境で確認し、不足時は勝手に増枠しない。

## 反映順序と戻し方

1. 読取りhealth、active/blocked/pending recovery/UNKNOWNが0であること、最新source/state/unit hash、既存key/binary参照を再確認する。ユーザーの並行変更とindexを保全し、対象source・login入口だけをバックアップする。credential本文はコピーしない。
2. 既存CLIで専用daemon/tunnelを停止する。停止でMCPは一時切断される。idleを再確認できない、所有者や状態が不確定な場合は停止しない。
3. 承認した9 runtime sourceと0600の新policy/executor JSONだけを反映し、既存flagを全て保持したpreflightを実施する。key参照は停止前の実参照を必ず明示し、helperの汎用defaultに置き換えない。
4. 専用2サービスをpipeline flag付きで1回起動。READY、health、poll freshness、23 schema、元の11 schemaを確認する。fail時に起動を繰り返さない。
5. 既存Chat MCP接続を更新し、capabilities→thread_start→task_submit→task_get→eventsで固定prompt 1件を通す。生成再送0、status/readbackのpollは可。正規MCP経路が使えなければ未接続と報告し、ファイル直書きでMCP成功を代替しない。
6. 完了・回答・event cursor・回収・held 0を照合後、既存login入口のinvocation pinを更新し、user daemon-reloadだけを行う。enable/lingerの追加はない。再ログイン試験は今回行わない。

停止はcancelの受理とterminal確認を分離する。通常RPC最大10秒、論理期限120秒はservice全体のhard kill deadlineではない。idle後にnative childを閉じてleaseを返すため、後続操作で所有threadをresumeすることがある。このresumeとdaemonのclean environment・実resource枠は単発host成功では未検証であり、今回のMCP試験で確認する。

Rollbackは元のsource、pipeline OFFの11tool引数、元login pinへ戻す。DB/mailbox grant/task/履歴/key/binary参照は削除・変更しない。残るturnや回収がUNKNOWNなら自動で戻したと主張せず、照合待ちとして止める。既存service helperの`pipeline-rollback-plan`を利用し、繰返しrestartや別経路の実行をしない。

初回MCP確認が成功しても、store/queue/steer/archive/実cancel全ての実機確認、逆方向Chat wake、常用回数への上限拡張は未完として分離する。
