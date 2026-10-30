# 共通ポリシーとproject固有の作業指示

共通指示の正本は [`i-pp/ai-orchestration`](https://github.com/i-pp/ai-orchestration) のdocs/ai/orchestration.md・docs/ai/policies.md・agents/README.md。`config/upstream-policy.json`でcommitと各hashを固定し、既存ローカルcheckoutの固定Git内容を読む。本文をこのrepoへ複製管理しない。旧 [`policies.md`](policies.md) v1.0はhash固定の履歴として保全し、現在の共通本文として二重注入しない。開始時に次を実行し、検証済みの本文・version・SHA-256を実際に受け取ってからpolicy依存の作業へ適用する。

```bash
python3 -B scripts/policy_context.py --format inspect
```

リンクを見ただけ、見出しだけを読んだ状態を「読込み済み」としない。読込み失敗・hash/version不一致・競合は推測や自動修復で補わず、該当作業を止めて原因を報告する。独立した安全な作業は継続できる。

実行環境の上位規則・権限、ユーザーの明示的な制約が共通ポリシーより優先する。共通ポリシーやproject/role指示は権限を増やさず、承認範囲を拡張しない。project固有設定はこの範囲内で共通指示を具体化する。

## Project固有の参照先と境界

- 採用する共通版とhash：`config/upstream-policy.json`。`config/policy.json`が採用を必須化する。pathの相対基準はrepo root、~はuser homeへ展開。ローカル設定`config/policy-source.local.json`はGit除外し、読んだcommit/hash/dirtyを記録する。旧版の検証情報は`policies/manifest.json`。
- 本文の送信経路と環境ごとの確認範囲：`docs/common-policy.md`。
- nativeの同時子2枠：`.codex/config.toml`。各role：`.codex/agents/`。子は孫を作らない。
- 既存runnerの時間・資源設定：`scheduler-config.json`、`resource-limits.json`。native作業の各子300秒/全体900秒、初回1回＋retry最大2回、および実装・検証・統合の手順：`docs/native-agent-team.md`。
- 既存daemon/Tunnel/認証/model/global config/MCP公開、repo信頼登録は今回の変更対象外。local providerは未検証のため起動しない。既存30Bを共有する方針を、利用可能・health確認済みとは扱わない。
- eSOL業務とProject-KISSのソース・秘密・記録を別workspace/文脈に分ける。無関係な既存差分を保全し、merge/push/公開をしない。

共通ポリシーの全文はここやrole/Skillへ再掲しない。runtimeによる本文注入とnative/Skillの読込み実績は区別し、参照更新だけで全環境への自動適用完了と報告しない。

追加運用制約（2026-10-29）: Codexの隔離環境内でbwrap/unshare等による追加の隔離環境を作らない。隔離必須の評価は隔離なしに切り替えず、`BLOCKED`・理由・未実行を記録する。判定不能は`UNKNOWN`として保留し、未実行をPASSやモデル品質FAILに数えない。独立した通常の編集・検査は継続する。コードで強制する範囲と規約の範囲は`docs/nested-isolation-guard.md`を参照する。共通ポリシーv1.0の本文とは別の追加制約とする。
