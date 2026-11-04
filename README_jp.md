# Agent Orchestrator

## 概要

Agent Orchestratorは、Codexの実行と成果の検証、安全に再開できるかの判断を分けて記録するツールです。実行・受入証跡・復旧判断・人のレビューを別々に管理し、不確かな作業を無条件に再実行しないよう支援します。専用workspaceでCodex CLIタスクを扱うPython runnerと任意のuser daemon、ローカルMCP stdioアダプターを提供します。

> **状態: 実験的。** 通常のsystemd導入は、現在のホストでAppArmorがnamespace作成を拒否するため未完了です。別の開発用PoCには専用の証拠と制限があります。mailboxのend-to-endアクセス、書込み承認、parent-dotによる回答回収は、完全には検証されていません。単体テストの成功だけでは本番利用可能とは判断できません。

## インストール

Pythonパッケージとしてのインストーラーはなく、Pythonコードは標準ライブラリを使用します。文書に記載された環境にはLinux、Python 3.14、既存のCodex CLI／ファイル認証設定、`unshare`、`mount`、`setpriv`、`bwrap`、委譲されたsystemd user manager経由のcgroup v2が必要です。報告に記載された検証環境はUbuntu 26.04.1、Python 3.14.4、`codex-cli 0.164.0-alpha.11.1`です。他のOS／CLIバージョンとkeyring認証は未検証です。[systemd導入条件](docs/systemd-deployment.md)と[PoCサービス設定](docs/poc-services.md)を参照してください。

## 使い方

```bash
python3 -B src/runner.py --help
python3 -m unittest discover -s tests -v
```

基本的な流れは、新しいruntimeをcheckし、fixtureを用意してtaskをsubmitし、run後に状態をshowで確認することです。[runnerの手順](docs/how-to/run-task.md)にread-onlyの例があります。このPR headでは、GitHubのDNS解決に失敗し実行環境も利用できなくなったため、How-toのコマンドは未実行です。liveな`run`も未実行で、既存サブスクリプション枠を使う可能性があります。[検証記録](docs/testing.md#review-time-validation)と[Issue #2](https://github.com/i-pp/agent-orchestrator/issues/2)を参照してください。

## 発展的な使い方

- [アーキテクチャとタスクの流れ](docs/architecture.md)
- [workspace隔離](docs/workspace-isolation.md) · [並列実行](docs/concurrency-design.md) · [復旧](docs/recovery-design.md)
- [daemon](docs/daemon-design.md) · [scheduler](docs/scheduler-design.md) · [資源制限](docs/resource-limits.md)
- [MCP接続設計](docs/chatgpt-connection-design.md) · [mailbox運用](docs/mailbox-operations.md) · [mailbox権限](docs/mailbox-grants.md)
- [systemd導入状況](docs/systemd-deployment.md) · [Phase 2-E3の証跡](docs/phase2-e3-report.md) · [開発用PoCの証跡](docs/phase2-e3-2-report.md)

## ドキュメント

[検証設計](docs/verification-design.md)、[Git管理](docs/git-tracking.md)、[テスト手順](docs/testing.md)、[各Phaseの報告一覧](docs/phase-reports.md)を参照してください。共通のオーケストレーションポリシーは [`i-pp/ai-orchestration`](https://github.com/i-pp/ai-orchestration) で管理します。[英語README](README.md)がこのページの正本です。
