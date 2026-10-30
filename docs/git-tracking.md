# Git収録方針と初回push前の確認

この文書は収録案であり、commit/pushやindex操作の実行記録ではない。2026-10-29、接続済みGitHubで`i-pp/agent-orchestrator`のprivate属性を確認。ローカルremoteは未設定のため追加していない。

実パスの表記とshell変数は[パス表記規約](path-conventions.md)に従う。編集中のLLM実装・テストは変更せず、残る実パスは担当へ引き継ぐ。

## 収録するもの

| 対象 | 方針 |
|---|---|
| `src/`、`scripts/`、`tests/`、`tests/fixtures/phase1.sql` | 実装・検証コードと合成fixtureを収録。生成された実行データは除外 |
| `policies.md`、`policies/`、`config/policy.json` | 共通正本・manifest・project pinを収録。正本全文は変更しない |
| `AGENTS.md`、`.agents/skills/` | 作業規約と共通Skillを収録 |
| `.codex/config.toml`、`.codex/agents/*.toml` | 現在は子2枠と役割指示のみ。portableなproject設定として収録。他の`.codex`状態は除外 |
| `resource-limits.json`、`scheduler-config.json`、`verification-commands.json`、`examples/` | projectの既定値・受入仕様・例を収録。資源値は導入先で別途見直す |
| `README.md`、設計文書・整理済みの検証報告、`docs/issues/` | 開発資料として収録。生ログ・DB・個人設定調査は除外 |
| `systemd/agent-orchestrator.service`、`systemd/apparmor/` | portableな導入例・設計草案として収録。自動適用はしない |

生ログやDBは`.runtime/`等に保持する。機密を除いた検証結果を設計文書へ整理したものとは区別する。既存の文書・テスト・一部実装にはホストpathや接続先識別子への参照が残っている。これらは秘密値とは別だが、移植時にはplaceholder/環境設定への分離を検討する。今回コードや既存文書は変更していない。

## 除外・ローカル保全

- 実行runtime、Codex home、log、DB/WAL、socket/pid、cache、venv/dependencies、backups、rootの生成artifacts・一時worktreeをignoreする。
- 認証・鍵・token・secret、`.env`やlocal/private設定を除外し、sanitizedな`.env.example`等は残す。ignoreは内容検査ではないので、例に本物の値を入れない。
- `.serena/`の既存3ファイルは全てローカル保全し、今回は収録しない。作成主体が未確定であり、必要な共有設定だと確認できた場合のみ、その時点でportableな内容を別途採用する。
- `docs/config-change-investigation.md`は個人のhost/Codex設定調査でありローカル保持とする。
- 実環境用Tunnel設定2ファイルはignoreする。現在は**追跡済み**なので、ignoreだけでは収録から外れない。

## 追跡済みの要対応2ファイル

| ファイル | 検出した種類 | 対処案 |
|---|---|---|
| `systemd/aceis-mcp-tunnel.service` | 実環境固有の接続先識別子を含む稼働用設定 | 現物をローカルに残してindexから外し、portableなexampleを準備済み。index除外は保留 |
| `systemd/aceis-mcp-tunnel.yaml` | 実環境固有の接続先識別子を含む稼働用設定 | 同上 |

今回indexは変更していない。承認・内容確認後に実施する候補コマンドは次の通り。`--cached`はworking fileを削除しない。既存のindex内のユーザー差分も先に確認する。

```bash
git diff --cached --name-status
git rm --cached -- systemd/aceis-mcp-tunnel.service systemd/aceis-mcp-tunnel.yaml
```

過去commit内の内容はこの操作では変わらない。履歴の監査・書換えは今回実施しない。接続先識別子を秘密の認証値と混同せず、履歴を含めてどう移植するかは別途判断する。

## PortableなTunnel設定例

現用の稼働設定は改名・編集せず、`systemd/aceis-mcp-tunnel.example.service`と`systemd/aceis-mcp-tunnel.example.yaml`を準備した。実環境のTunnel IDを持たず、認証はrepo外のfile参照のまま。利用時は例をGit除外された現用名へコピーし、profileのplaceholderをローカルで置換する。今回は配備・停止・起動をしていない。

```yaml
# Example only: replace the placeholder locally before use.
config_version: 1
control_plane:
  base_url: "https://api.openai.com"
  tunnel_id: "REPLACE_WITH_YOUR_TUNNEL_ID"
health:
  listen_addr: "127.0.0.1:0"
admin_ui:
  open_browser: false
log:
  level: warn
  format: json
mcp:
  stdio_send_initialized_notification: true
```

service例は依存・readiness・資源制限を保持し、Tunnel IDはprofileだけから受け取る。credentialは外部private fileを参照し、実キーをrepoへ置かない。例の準備と稼働環境への配備は別の操作である。

## 監査・収録時の確認

`scripts/git_audit.py`は次の必要12ファイルだけを新たに明示許可し、assistant状態directory全体は許可していない。これらにもsecret/binary/size/index conflict検査を行う。sanitizedな環境例は許可するが、本物の値やlocal/privateの親directoryは拒否する。

| 許可した正確なファイル | 理由 |
|---|---|
| `.agents/skills/common-policy/SKILL.md` | 共通policy読込みSkill |
| `.codex/config.toml` | projectの子同時枠 |
| `.codex/agents/implementer.toml` | 実装role |
| `.codex/agents/verifier.toml` | 検証role |
| `.codex/agents/researcher.toml` | 調査role |
| `.codex/agents/independent_reviewer.toml` | 独立レビューrole |
| `config/policy.json` | projectのpolicy pin |
| `policies.md` | 共通指示の唯一の正本 |
| `policies/manifest.json` | version/hash manifest |
| `policies/orchestration.md` | 正本の参照索引 |
| `policies/decision-making.md` | 正本の参照索引 |
| `policies/escalation.md` | 正本の参照索引 |

必要なsourceを新しく増やす場合は、この厳密なallowlistもレビューして更新する。追跡済みのローカルTunnel設定2件は新監査でFAILする。indexから外すまでは全体監査PASSにはならず、ignoreだけで回避しない。

```bash
git status --short
git ls-files --cached --ignored --exclude-standard
git ls-files --others --exclude-standard
git check-ignore --no-index -v .serena/project.yml .runtime/tasks.sqlite
git diff --cached --name-status
git diff --cached --check
python3 -B scripts/git_audit.py
```

追加前の監査対象はworking file、最終監査対象は実際にstageしたblob。heuristicなsecret検出が0件でも全秘密の不存在は保証しない。値を出さず、path・種類だけで要確認箇所を扱う。`git add .`や`git add -f`を先に行わず、追跡除外と監査の整合を確認してから収録する。

今回commit/push、remote設定、index変更、実ファイル削除、履歴書換えはしていない。

## 実パスの残件と編集担当への引継ぎ

README・docsと未配備AppArmor草案の個人実パスは疑似パス/変数へ変更した。shell例は構文確認し、nested MCP commandでは空白を含むproject pathも単一引数として渡ることを確認した。原本ログや稼働設定は変更していない。

| 対象ファイル | 現在の状態と次の調整 |
|---|---|
| `src/mcp_adapter.py` | LLM担当の並行変更後、個人名を含む絶対パスは最終検査で解消。こちらからは編集せず、helper実行先の設定方針は担当側の検証結果を参照 |
| `tests/test_local_llm_mcp.py` | LLM担当の関連テスト。最終検査で残るのはrepo root定数（9行目）。期待helper pathの個人実パスは既に解消。残りを__file__からの導出値へ変更する調整 |
| `tests/test_phase2_e3_2.py` | 修正済み。親側の実際のhomeをPath.home()から導出し、元のhost非露出assertionを保持。namespace実機試験は今回実行しない |

LLM担当の前2件は今回編集していない。残存実パス1件の解消と追跡済みTunnel設定2件のindex除外は、commit前に担当と確定する。監査規則と非競合の境界テストは修正済み。個別ファイルの内容を未確認のまま一括stageしない。
