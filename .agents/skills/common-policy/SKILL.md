---
name: common-policy
description: Load and verify this orchestration repository's common policy before policy-dependent coordination, implementation, or verification; report the version and hash actually received.
---

# 共通ポリシーの読込み

対象repo rootで `python3 -B scripts/policy_context.py --format inspect` を実行する。返された正本全文・version・SHA-256を実際に読み、担当作業に適用する。Skillや正本へのリンクを見ただけで本文を読んだことにしない。

正本はi-pp/ai-orchestration。`config/upstream-policy.json`のcommit/hashを固定し、既存ローカルcheckoutのGit内容を読む。`config/policy.json`でこの参照を必須化する。相対pathはrepo root基準、~はuser homeへ展開し、local設定はGitへ収録しない。読込metadataのupstream commit・実読込checkout commit・HEAD・dirtyを区別する。旧rootの`policies.md`は履歴参照として保全し、本文をSkillやこのrepoへ複写管理しない。

固定した5文書にはEditorial Policy v2.0の正本と運用補足を含む。技術記事の作成・編集・引継ぎではこの規約を適用し、Bの受理・凍結とAへの引継ぎ、技術レビューと編集レビューを区別する。一般の実装タスクへ記事形式を強制せず、独立したFace Cへ共有指示を送らない。記事用Skillや例文の実読込み、記事の受理・公開承認は、この共通本文の注入だけでは完了しない。

上位実行環境の規則・権限・ユーザーの明示的制約が優先し、このポリシーは権限を追加しない。読込み失敗・競合・hash/version不一致なら、推測や別設定への切替をせず該当するpolicy依存作業を止め、原因を親へ返す。安全で独立した作業は続ける。trust登録、保護された状態領域、認証/provider/MCP公開を変更して回避しない。

結果には読込み経路、version/hash、実際の受取り範囲、未検証を記録する。runtimeが本文を注入した場合も、その実績をnative/Chat/Dot/local全経路へ一般化しない。
