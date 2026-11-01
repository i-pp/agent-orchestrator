# 単発host往復：先に1件の回答を回収する

追加のsmoke-no-tools profileは利用者の指示で後回しとした。現在のprofileなしの通常host起動では、固定VS Code Codex binaryでaccount/model読取りが成功している。`scripts/codex_pipeline_once.py`は、そのargvを維持して既存`Pipeline`のthread/task/回答回収を1回通す候補である。実モデルでの往復は未実施。常設APIや別orchestratorは追加しない。

## 実行範囲

運用者が通常hostで承認後に実行する。管理sandboxからの実行・設定緩和・認証コピーは行わない。既存CodexのHOME/loginを使い、固定native ELFとhashをpreflightで確認する。

```bash
python3 -B "$PROJECT_ROOT/scripts/codex_pipeline_once.py" --binary "$CODEX_BINARY" --sha256 "$CODEX_SHA256" --workspace "$SMOKE_WORKSPACE" --coordinator-runtime "$EXISTING_RUNTIME" --run-dir "$NEW_RUN_DIR"
```

- `PROJECT_ROOT`はレビューした候補checkout。`CODEX_BINARY`/`CODEX_SHA256`は成功済みread probeと同じ参照。
- `SMOKE_WORKSPACE`はagent-orchestrator 1件。modelはgpt-6.1-sol固定。新thread 1・task 1・主turn開始最大1回、retry 0。別run-dirを自動で作り直さない。
- `EXISTING_RUNTIME`のrunner.lockを排他的に保持し、既存task DBのidle・UNKNOWN不在を確認する。サービスの起動/停止/設定変更はしない。
- `NEW_RUN_DIR`は未使用の絶対path。親は既存の所有者directory。0700で新規作成し、request・0600 receipt・既存形式のprivate SQLite ledgerを保持する。既存directoryは拒否する。

送信する固定依頼：

> Reply with exactly PIPELINE_SMOKE_OK. Use only this message. Do not call tools, read files, access the network, change settings, or create subagents.

固定共通policyを従来の検証済みdeveloper instructionsとして渡す。通常Codexが追加するproject instructions/skills/memories等の文脈は継承し得る。thread/turnのfilesystem sandboxはread-only、approvalはon-request/userを明示する。一方、既存MCP/plugins/appsは無効化しない。read-onlyでconnectorの外部書込みまで防げるとは保証せず、no-toolsという依頼も権限の強制ではない。

## 判定・停止・回収

初期化を含む120秒の論理期限、各通常RPC最大10秒。Ctrl-C/期限/承認要求/予期しないtool itemを観測すると、既知turnへinterruptを1回だけ送り、最大2秒のACK待ちと最大2秒のterminal readbackを行う。観測後の中断はtool副作用の事前防止や取消を意味しない。承認/認証更新要求には回答しない。

実行者process groupと観測した子を回収する（直接子TERM待ち2秒＋KILL待ち2秒、group照合最大2秒）。120秒は停止/回収を含むOS全体のhard deadlineではない。観測前にgroup外へ完全離脱した子まで管理できるという保証もない。

正確な回答`PIPELINE_SMOKE_OK`、terminal COMPLETED、held task 0、回収確認を全て満たした場合だけPASS。別の回答の本文はstdoutへ出さない。要求・回答・判断は新run directoryへ保存する。

ACK不明、停止未確認、台帳/保存エラー、回収未確認はUNKNOWN。ローカル子の終了だけでremote turnの停止を成功扱いしない。元のreceiptとledgerを保全し、元thread/turnの照合を通常Codexで行うまで再実行しない。stdin/ポートで追加依頼を受け付ける機能はない。追加tool公開・常設daemon反映・新credentials・API従量課金経路は含まない。ChatGPT認証の確認は料金・利用枠の保証ではない。
