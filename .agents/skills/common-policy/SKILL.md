---
name: common-policy
description: Load and verify this orchestration repository's common policy before policy-dependent coordination, implementation, or verification; report the version and hash actually received.
---

# 共通ポリシーの読込み

対象repo rootで `python3 -B scripts/policy_context.py --format inspect` を実行する。返された正本全文・version・SHA-256を実際に読み、担当作業に適用する。Skillや正本へのリンクを見ただけで本文を読んだことにしない。

正本はrootの`policies.md`。`config/policy.json`はproject固有の参照/pin、`policies/manifest.json`は共通bundleの検証情報。分割文書は索引のみ。本文はSkillへ複写しない。

上位実行環境の規則・権限・ユーザーの明示的制約が優先し、このポリシーは権限を追加しない。読込み失敗・競合・hash/version不一致なら、推測や別設定への切替をせず該当するpolicy依存作業を止め、原因を親へ返す。安全で独立した作業は続ける。trust登録、保護された状態領域、認証/provider/MCP公開を変更して回避しない。

結果には読込み経路、version/hash、実際の受取り範囲、未検証を記録する。runtimeが本文を注入した場合も、その実績をnative/Chat/Dot/local全経路へ一般化しない。
