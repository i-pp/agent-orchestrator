# 期限と件数を限定した開発相談grant

固定slotの追加を繰り返す代わりに、一度承認したtask UUID内で要求を初回登録時にpinする。`--allow-mailbox-grants`は既定OFF、既存の`--allow-chat-mailbox`も必要。公開操作名・schemaは既存11操作と同じ。固定slotは従来のpolicy/DBで維持し、grantだけを別ledgerで扱う。task namespaceの衝突は起動前に拒否する。

## 承認された範囲と実際の有効期間

2026-10-30 04:34 UTCに確認された承認は、7日間・合計32件・同時PENDING最大8件、依頼と回答各4096 UTF-8 bytes、要求期限24時間、回答返却7日、監査表示30日。検証用の新規要求も合計32件に含める。実際の開始・終了・policy hashは有効化記録とruntimeの`chat-mailbox-grant.json`に固定し、サービス再起動や再送では延長しない。exampleの時刻は有効化記録ではない。

| 項目 | 強制する境界 |
|---|---|
| 対象 | 1個のtask UUID、purpose=`agent-orchestrator-development` |
| 内容 | 非機密の開発相談という運用上の範囲。自動secret分類・著者本人認証ではない |
| 初回登録 | request UUIDと要求SHA-256をimmutable pin。hash/本文競合を拒否 |
| grant期間 | not_before以上、expires_at未満だけ登録・回答保存可。最大7日 |
| 要求期限 | 初回登録＋24時間とgrant終了の早い方。期限後は同内容のwrite再送も拒否 |
| 予算 | 総数32、未回答かつ期限内8。回答でpending枠のみ解放。期限切れ・取消・再接続で総数を減らさない |
| 本文 | 各4096 UTF-8 bytes以下。不正Unicode・空白のみ・hash不一致を拒否 |
| PENDING読取り | 要求期限までは要求本文、以降はEXPIRED metadataのみ |
| ANSWERED読取り | 回答保存から7日未満は要求と回答を返却。grant期限後もこの期間内は読取り可能 |
| 取消 | 永続REVOKED。登録・回答保存禁止、既存/未登録とも本文を返さずmetadataのみ |
| 監査 | ID・hash・時刻・固定event。30日分のlogical view。MCP到達監査にも本文を記録しない |
| 容量 | private SQLite ledger最大1MiB。journalやディスク全体quotaは別 |

期限切れgrantの新しいIDはNOT_FOUNDとgrant_state=EXPIREDを返す。これを新規登録可能とは扱わない。要求/回答の返却期限切れはEXPIRED、取消はREVOKED。読取りの保存有無・grant状態・hashと本文の有無を分けて扱う。clientはterminal状態になったとき、先行する読取りの本文も出力しない。

## 起動・再接続

runtimeの固定ファイル`chat-mailbox-grant.json`は所有者0600、runtimeは0700。正確な4 field（schema_version/enabled/policy/policy_sha256）だけを受理する。任意pathをMCP引数に渡す経路はない。`GrantPolicy`は未知属性・別purpose・型・UUID・上限を検査する。同じgrant_idのpolicy hash変更は既存ledgerで拒否する。

preflightは全起動引数・既存キー参照・policyを停止前に照合し、ledgerを作成しない。再開は保存済みflagとキー参照を継承する。期限切れでもretained answerを取得できるよう再接続は可能。新規writeの可否は各transaction内で再検査する。

SQLiteのBEGIN IMMEDIATE取得後に時刻とpending/totalを確認し、commit直前にも期限を確認する。競合書込みでも上限・初回hash・回答の不変性を守る。ACK喪失はUNKNOWN。既存ID/hashによるgetterだけで照合し、自動write再送や別ID発行をしない。

## オペレータCLI

シェル例はrepo rootで実行する。`RUNTIME`を対象の既存private runtimeに、`GRANT_ID`と`POLICY_SHA256`を有効化記録に合わせて設定する。秘密の値を入れない。`mailbox_ops.py`はJSONをstdinから受け取り、既定で本文をstdoutへ出さない。

```bash
python3 -B scripts/mailbox_ops.py prepare --runtime "$RUNTIME" < nonsecret-draft.json
python3 -B scripts/mailbox_ops.py register --runtime "$RUNTIME" < nonsecret-request-bundle.json
python3 -B scripts/mailbox_ops.py inspect --runtime "$RUNTIME" < request-key.json
python3 -B scripts/mailbox_grant_admin.py status --runtime "$RUNTIME" --grant-id "$GRANT_ID" --policy-sha256 "$POLICY_SHA256"
python3 -B scripts/mailbox_grant_admin.py revoke --runtime "$RUNTIME" --grant-id "$GRANT_ID" --policy-sha256 "$POLICY_SHA256" --apply
```

prepareは候補を組み立てるだけでpolicyやledgerを作成せず、取消状態を含むwrite許可の確認にはならない。status/revokeは既存ledgerのみを開き、存在しないDBを作成しない。revokeは明示`--apply`が必要。同一grant ID/hashを確認して不可逆に取消し、取消後のstatusで照合する。不確実なrevokeを自動再送しない。公開MCPに取消や管理コマンドを追加しない。

## Rollback

```bash
python3 -B scripts/poc_services.py grant-rollback-plan --runtime "$RUNTIME"
```

出力された停止前検査・stop・start・status手順でgrant flagだけを外す。既存11操作・固定slot・認証・公開範囲を保全し、grant ledger/configはprivateに残す。復帰時は同じgrant/policy/hashを使用し、総数や期限をリセットしない。`rollback-plan`はmailbox全体を外して旧7操作へ戻す別手順。どちらもplanだけではサービスを変更しない。稼働/UNKNOWNの仕事があれば停止しない。

## 保持と実証の限界

7日は本文の返却期間、30日はledger監査metadataの表示期間。物理削除・VACUUM・backup削除・自動pruneは実装しない。期限後もprivate DBにbytesが残る。MCP adapterの既存到達監査ファイルは別の容量制限を持ち、自動30日削除ではない。

同じ認証scopeのclientが対象workstreamで依頼/回答を読み書きする。ID/hashは会話本人や回答品質の証明ではない。この機能はChatを起動せず、任意ファイル/URL/shell/タスク・新キー・モデル起動を公開しない。ローカルMCP stdioでの実証、hosted schema/health確認、Dot直接呼出し、実Chat往復は別の証拠として報告する。
