# Agent Orchestrator

## 概要

Agent Orchestratorは、Codexの実行と成果の検証、安全に再開できるかの判断を分けて記録するツールです。実行・受入証跡・復旧判断・人のレビューを別々に管理し、不確かな作業を無条件に再実行しないよう支援します。専用workspaceでCodex CLIタスクを扱うPython runnerと任意のuser daemon、ローカルMCP stdioアダプターを提供します。

> **状態: 実験的。** 通常のsystemd導入は、現在のホストでAppArmorがnamespace作成を拒否するため未完了です。別の開発用PoCには専用の証拠と制限があります。mailboxのend-to-endアクセス、書込み承認、parent-dotによる回答回収は、完全には検証されていません。単体テストの成功だけでは本番利用可能とは判断できません。

## インストール

Pythonパッケージとしてのインストーラーはなく、Pythonコードは標準ライブラリを使用します。文書に記載された環境にはLinux、Python 3.14、既存のCodex CLI／ファイル認証設定、`unshare`、`mount`、`setpriv`、`bwrap`、委譲されたsystemd user manager経由のcgroup v2が必要です。報告に記載された検証環境はUbuntu 26.04.1、Python 3.14.4、`codex-cli 0.164.0-alpha.11.1`です。他のOS／CLIバージョンとkeyring認証は未検証です。[systemd導入条件](docs/systemd-deployment.md)と[PoCサービス設定](docs/poc-services.md)を参照してください。

`run`の前に、operator管理の共通policy checkoutを設定してください。`config/upstream-policy.json`はcommit `aeef6db1cc9680833277f76962044da0dae501b9`を固定しており、既定の配置先はrepository root基準の`../ai-orchestration`です。選択したcheckoutとpinは `python3 -B scripts/policy_context.py --format inspect` で確認します。path overrideと整合性条件は[共通policyの設定](docs/common-policy.md)を参照してください。

## 使い方

```bash
python3 -B src/runner.py --help
python3 -m unittest discover -s tests -v
```

[runnerの手順](docs/how-to/run-task.md)は、read-onlyの流れとしてruntime確認、fixture作成、taskのqueue登録、状態確認を案内します。レビュー時のhelpと`policy_context inspect`は、検証対象のPRコードheadと固定policy commitをGitHub APIのhash検証済みobjectから作った作業ツリーで成功しました。その後のcommitは文書だけを変更しています。fixture作成、queue専用の`submit`、`show`は、現在のPR headとruntime blobが一致するソースsnapshotで成功しました。検証対象コードheadの`check`はこのCodex sandbox内で`BLOCKED`（`executed=false`）となり、liveな`run`は実行していません。正規originを確認できなかった点を含む出典と制限は[テスト手順](docs/testing.md#review-time-validation)に記録し、正規origin確認とlive実行は[Issue #2](https://github.com/i-pp/agent-orchestrator/issues/2)で追跡します。

## 発展的な使い方

- [アーキテクチャとタスクの流れ](docs/architecture.md)
- [workspace隔離](docs/workspace-isolation.md) · [並列実行](docs/concurrency-design.md) · [復旧](docs/recovery-design.md)
- [daemon](docs/daemon-design.md) · [scheduler](docs/scheduler-design.md) · [資源制限](docs/resource-limits.md)
- [MCP接続設計](docs/chatgpt-connection-design.md) · [mailbox運用](docs/mailbox-operations.md) · [mailbox権限](docs/mailbox-grants.md)
- [systemd導入状況](docs/systemd-deployment.md) · [Phase 2-E3の証跡](docs/phase2-e3-report.md) · [開発用PoCの証跡](docs/phase2-e3-2-report.md)

## ドキュメント

[検証設計](docs/verification-design.md)、[Git管理](docs/git-tracking.md)、[テスト手順](docs/testing.md)、[各Phaseの報告一覧](docs/phase-reports.md)を参照してください。共通のオーケストレーションポリシーは [`i-pp/ai-orchestration`](https://github.com/i-pp/ai-orchestration) で管理します。[英語README](README.md)がこのページの正本です。
