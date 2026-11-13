# Tunnel MCPの運用手順と完了条件

共通ポリシーの正本は[policies.md](../policies.md)。この文書は運用手順であり本文を複製しない。参照指定された`docs/ai/orchestration.md`は、確認したローカルrepository群になく、GitHubの対象repositoryも空だった。正本の所在確認は[課題](issues/mailbox-operations.json)に残し、確認済みpolicy v1.0の範囲で進める。

## 完了条件と現在の確認範囲

| 条件 | 証拠・判定 |
|---|---|
| 既存11操作と完全schemaを維持し、submit/cancelを増やさない | schema比較の回帰、現用adapterのdiscover/tools-list |
| key/codex/3 capability flagを保存stateから再構成 | resume-plan / rollback-plan。参照欠落は推測せず拒否 |
| 停止前検査、未解決仕事・不確定世代・別ownerの拒否 | preflight、stop直前DB検査、mock回帰 |
| writeのACK喪失で再送・別slot生成をしない | registerのwrite最大1、再接続後getterで照合。未発見でもUNKNOWN維持 |
| readの接続障害・timeoutを有限に処理 | read-only再接続最大1回、各RPC既定5秒・上限10秒、pipe書込みも同じ期限 |
| 要求・回答のID/hashを照合して回収する | inspect、server/client双方のhash検証、実stdio fixture |
| 到達・入力拒否・保存状態を秘密なしで区別 | begin/end、tool_rejected、end.mailboxのstatus/code/hash。raw本文は監査しない |
| 通常タスクを準備して承認対象を具体化する | prepareは候補JSON出力のみ。現用allowlistへ自動追加しない |
| 既存の回答と単発疎通の証拠を保全 | 既存slot/DB/policyを変更・削除しない |

確認済みの経路はローカルCodexの正規送信→起動済みChatのMCP get/save→ローカルCodexのMCP本文回収。ChatGPT UIのツール更新で11操作を確認した。親dotのcatalog反映と直接呼出し、休止Chatの起動は別の制約である。`mailbox_ops.py`は明示的なローカルstdio操作であり、hosted Chat/dot経由の呼出し成功とは表示しない。

## 再接続・復旧・rollback

パス変数は[表記規約](path-conventions.md)に従う。通常運用の前にstatusを確認する。

```bash
cd "$PROJECT_ROOT"
python3 -B scripts/poc_services.py status
python3 -B scripts/poc_services.py resume-plan
python3 -B scripts/poc_services.py rollback-plan
```

planは保存済みの同じkey/codex参照とflagsから、既存CLIのpreflight/stop/start/status引数配列を作る。キー内容を読まず、自動実行・自動再投入しない。rollback-planはmailbox flagだけを外して旧7操作に戻す。stateに参照がなければ既存の非機密起動記録を照合する。既定のcredential pathへ勝手に切り替えたり、新しいキーを作ったりしない。

計画はsnapshotであり、実行前に再検査する。仕事・UNKNOWN・WAITING_FOR_HUMANの解消、所有世代、health、変更範囲を確認してから、承認範囲で配列のコマンドを順に実行する。startには保存済みkey/codex/flagsを明示する。stop直前もtask DBを再確認するが、他経路の任意書込みまで完全に排他化するものではない。

task DBに載らないlocal LLM要求等が進行中かは別途確認する。未完了のMCP begin/end、実行主体の状態や既存の不確定記録を照合し、UNKNOWNなら再起動/再送を決め打ちしない。接続断やUIエラーだけでは失敗と断定しない。自動再接続はローカルの読み取りRPCを最大1回だけ再接続する範囲であり、サービス監視daemonや恒久的な再起動ループは追加していない。

再起動後はREADY、両unitのowned/active、daemon/Tunnel health、11schemaを確認する。connector UIのツール更新、現在の会話catalog、実際のtools/call成功は別々に記録する。新しいAPIや権限で迂回しない。rollbackでもDB/policy/logを削除しない。状態ファイルやlockを消して強制起動しない。

## 同じ要求の登録と回答回収

`bundle.json`は承認済みのtask_id/request_id/request_sha256/requestを含む。`key.json`は同じ3共通field（必要ならexpected_answer_sha256）を含む。IDをtimeoutのたびに生成し直さない。入力はUTF-8 JSONで、未知field、過大入力、path/URL/commandの追加を拒否する。

```bash
python3 -B scripts/mailbox_ops.py register < bundle.json
python3 -B scripts/mailbox_ops.py inspect < key.json
python3 -B scripts/mailbox_ops.py inspect --include-content < key.json
```

registerは最初にget_requestで既存状態を確認する。同一内容があれば再利用し、未登録のときだけregister_requestを最大1回呼ぶ。ACK喪失後は再接続してgetterだけで照合する。保存が確認できなければUNKNOWNを保持する。NOT_FOUNDは、timeoutした先行writeが後からcommitされないことの証明ではない。自動再送・別request_idへの置換・回答再生成はしない。

inspectはget_request/get_answerを照合する。接続障害のread-only再接続は最大1回。RPCは既定5秒、`--timeout`は0.05〜10秒。タイムアウト後のstdio子は回収し、既存サービスやChatを停止しない。正常なPENDINGは「回答待ち」、NOT_FOUNDは「保存未確認」、ANSWEREDは本文/hash一致の保存状態。UNKNOWN/BLOCKEDはexit2、正常状態はexit0。

通常のreceiptはID/hash/call_id/状態/回数/時間だけで本文を出さない。本文取得は`--include-content`を明示する。非機密の質問・回答だけを使う。元のMCP get操作自体は本文を返すため、host/client履歴に本文が残らない保証ではない。

## 通常タスクの準備と承認境界

固定1slotの回答はimmutableなので、別の仕事へ使い回さない。draftはtask_id/request_id/requestを含む。IDはoperatorが先に固定し、同じ仕事の照合中は変えない。

```bash
python3 -B scripts/mailbox_ops.py prepare < draft.json > candidate.json
```

prepareはhash、request_bundle、既存slotを保持したcandidate_policy、approval_requiredを返す。新slotならPENDING_SCOPE_APPROVALであり、policy更新・サービス変更・Chat送信は行わない。registerは現在のpin外を拒否し、prepareの結果を勝手に有効化しない。最大32slot・要求/回答各4096 UTF-8 bytes・DB上限1MiBを維持する。容量を増やす/古いslotを削除して再利用する自動処理はない。

追加承認では、正確なID・要求hash・本文・対象runtime・追加slot数、同じ認証のclientが新本文/回答を読書きできること、一時切断と反映/rollback手順を提示する。既存11操作を増やす必要はない。承認後にだけoperatorがprivate policyを変更し、preflight→仕事/UNKNOWN照合→既存手順で反映する。承認前にallowlistを拡大しない。

slotはMCP runnerのtask生成・実行ではなく、限定された相談本文の受け渡しである。実装を自動起動する機能や任意コマンドAPIは追加していない。

## 監査と障害の切り分け

private runtime/logsのmcp-*.jsonlを非機密metadataだけで照合する。task_id/request_id/request_sha256とcall_idでbegin/endを対応付ける。end.mailbox.status/code/answer_sha256/duplicateは保存状態の判断材料となる。tool_rejectedは入力/公開対象の拒否を示すが、未検証の入力値は記録しない。slotやChat本人への帰属を推測しない。

- begin/end returnedとANSWERED/hash一致があれば、取得/保存/読戻しを確認できる。
- error_or_uncertainやUNKNOWNならgetterで照合し、再送しない。
- beginのみなら処理継続・応答喪失・監査失敗を区別できないためUNKNOWNとして調べる。
- 記録がなければ「未観測」。transport未到達、読込前の失敗、不正JSON、client側tool未提供等を一括して「Chatが使えなかった」と断定しない。

raw本文・未知tool名・入力値・キー内容・外部例外の全文をログへ足さない。監査ファイルは1session最大4MiB、serviceログはLimitFSIZE64MiBで、ディスク全体quotaや無期限運用は保証しない。空き容量・未終了call・ログの保全/保存期間をoperatorが確認する。自動prune/削除はしない。

ID/hashはChat会話identityや回答者の認証ではない。同scope client間の役割別認証はなく、単発の成功を常時自律連携と同一視しない。新しいモデル推論・Chat送信・権限拡大を障害調査の副作用として追加しない。

## 必要な検証

```bash
python3 -B -m unittest discover -s tests -p test_mailbox_operations.py
python3 -B -m unittest discover -s tests -p test_service_recovery.py
python3 -B -m unittest discover -s tests -p test_service_preflight.py
python3 -B -m unittest discover -s tests -p test_chat_mailbox.py
```

mockと専用stdio fixtureで検証し、新namespace/実モデル/実サービスは起動しない。既存socket bindがEPERMとなる2件は未検証のまま。実Chatへの追加送信はこの回帰に含めない。
