# 限定workstream grantの承認案（未接続・既定OFF）

固定slotを1件ずつ追加する代わりに、期限と件数を限定したworkstreamを一度承認し、その範囲内で要求を初回登録時にpinする案。実装は`src/mailbox_grants.py`の独立ledgerとオフライン検査まで。既存MCP adapter、service flag、現用policy、既存DBへ接続していない。現在の11操作・live固定1slotは変わらない。

## 一度に確認する最小範囲

`examples/mailbox-grant.example.json`は`enabled=false`の候補。policy属性だけがvalidatorへの入力であり、このJSONを置いても公開・有効化されない。例の開始・終了は2026-10-30 00:00 UTC〜2026-11-06 00:00 UTC。承認時に実際の期間とpolicy hashを再確認する。期限を暗黙に延長しない。

| 項目 | 制約 |
|---|---|
| 対象 | 1個の固定task UUIDで表すagent-orchestrator開発相談workstream |
| 目的 | 非機密の設計確認・差分レビュー・テスト結果の相談。ラベルは意味上の分類で、内容分類の自動強制ではない |
| 新要求 | UUIDのrequest_idとSHA-256を初回登録でimmutable pin。以後同じ要求を上書きしない |
| grant期限 | 最大7日。指定not_before以上/expires_at未満だけwrite可能 |
| 要求期限 | 初回から最大24時間、grant終了までの早い方。再送でも延長しない |
| 件数 | grantあたり合計32要求、同時PENDING最大8。期限切れも合計件数から除去しない |
| 本文・回答 | 各4096 UTF-8 bytes以下。空白だけ・不正Unicode・hash不一致を拒否 |
| 本文取得 | PENDINGの要求本文は要求期限まで。ANSWERED後は要求本文・回答とも正常回答保存から最大7日。grant終了後も残り、全体ではgrant開始から最長14日となり得る。取消時は両本文を即座に非返却 |
| 監査 | write/revokeの時刻・ID・hash・固定eventだけ。表示期間最大30日。本文・外部例外の全文は記録しない |
| 格納上限 | 別のprivate SQLite ledger最大1MiB。rollback journal等とディスク全体quotaは別 |
| 自動処理 | 自動Chat起動・モデル起動・write再送・任意ファイル/URL/shell/タスク実行なし |

同じMCP認証scopeのclientには、このworkstream内の新規要求登録・要求取得・回答保存・回答取得を許すことになる。質問ごとの事前hash指定を不要にする変更であり、現行1slotからの明確な公開対象拡大。ID/hashはChat本人や著者・品質の証明ではない。非機密制約は運用上の前提で、自動secret検出や役割別本人認証の保証ではない。

## ledgerで準備済みの強制

GrantPolicyは未知field、別purpose、UUID不正、上限超過を拒否する。同じgrant_idのpolicy hash変更も拒否し、再オープンで期限・取消をリセットしない。新grant_idは新たな承認対象であり、clientやtimeout処理が勝手に発行しない。

SQLite `BEGIN IMMEDIATE`内でpending/totalを調べて登録する。並行書込みでも上限と初回hash pinに勝者は1件だけ。同一内容再送は予算を増やさず冪等、別内容を拒否する。最初の回答だけを保存し、同一回答の再送は延長・上書きしない。

ACK喪失はUNKNOWNを返す。get_request/get_answerで既存ID/hashを照合し、再送や別ID作成を自動実行しない。commit前失敗なら保存なし、commit後の応答喪失ならgetterで保存状態を確認できる。SQLite障害や不正な本文/hash/期限metadataを成功と扱わない。

## 取消とrollback

`GrantLedger.revoke()`は準備されたoperator操作であり、MCP toolではない。取消は永続・冪等で、新規登録と回答保存を停止する。getはREVOKEDと保存有無/hash等のmetadataのみを返し、本文を返さない。取消を戻す機能はなく、再許可は別grantの具体的承認が必要。

runtimeへの将来統合は別の既定OFF切替で行い、承認前に導入しない。rollbackではgrant経路を無効にして現行の固定slotだけへ戻す。元のpolicy/回答を削除・上書きせず、grant ledgerはprivateに保全する。public側にgrant操作や任意コマンドを増やす案ではない。

## 保持の正確な限界

7日は「回答を返せる期間」、30日は「監査metadataの表示期間」をコードで制限する。今回のmoduleは物理削除・VACUUM・backup削除・自動pruneを実装していない。期限後のbytesはprivate DBに残る。保持期間の経過だけで消去されたと報告しない。

実運用前に、承認者はこの論理保持を採用するか、物理削除と保全の別手順を要求するかを決める。hard deletion期限が必須なら、削除・backup・復旧不能性を別途具体化し、検証してから有効化する。上限到達時は新規書込みを拒否し、容量を勝手に拡大したり、旧IDを削除して再利用したりしない。

read操作の全到達履歴は将来の既存MCP audit層で記録する必要がある。この独立ledgerだけでは未到達・認証失敗・不正JSON等を観測できない。未知callのabsenceをChatが使えなかった証明にしない。

## 現段階の検証と残る作業

17検査で、既定OFF、policy/型/上限、scopeと未知field、動的初回pin、冪等・競合、pending/total予算、並行登録、期限、回答返却期間、永続取消、ACK喪失/rollback、Unicode/サイズ、改ざん、metadata監査、11既存tool不変を確認した。専用一時SQLiteのみ、namespace・モデル・サービス・API通信は0。

既存adapter/serviceとの接続、取消操作のoperator経路、metadata-only状態を既存clientが正しく扱うこと、実Chat往復、物理保持の運用は未実装/未検証。承認案の対象と未接続の準備コードを区別し、現行の11操作がこのgrantで既に動くとは扱わない。
