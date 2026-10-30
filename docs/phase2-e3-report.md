# Phase 2-E3 — systemd常駐化・ChatGPT読み取り接続検証

実施日: 2026-10-28（Asia/Tokyo）。開始commit: `07b9808218c50b4ef4624f729d1c9371af075c8c`。
本書はAIによる実測の要約。原出力・抽出証拠はGit対象外の`.runtime/phase2-e3-evidence/`。

## 判定: Stage A未完了、Stage B/Cには進まず停止

| 利用者指定の完了段階 | 判定と証拠 |
| --- | --- |
| 1. unit実装・静的検証 | 2 unit、service用profile、readiness helperを作成。verify exit 0。ただしStage A統合試験2件失敗 |
| 2. 実サービス手動起動 | 未実施。実運用名のunit未配置・未起動。専用transient fixtureのidle daemon起動/停止/再起動は成功 |
| 3. 実Tunnel readiness | E3 systemd経路では未実施。模擬control plane＋実clientでは成功。E2利用者実行の既存Tunnel成功とは区別 |
| 4. Tunnel経由MCP呼出し | 実client＋loopback模擬control plane＋実adapterのinitialize/list/status成功。OpenAI hosted経路では未検証 |
| 5. ChatGPT読み取り実呼出し | 未実施・未検証。ブラウザ操作なし、接続登録なし |

実サービス導入の承認を得る以前に、systemd配下のnamespace起動がAppArmorで拒否される問題が見つかった。
権限や隔離を変更せず、実運用のunit配置・credential移行・startは行っていない。

## 事前確認と保持

AGENTS.md、README、E1/E2報告、接続設計、daemon/service/adapter/fixture実装・テストを確認した。
OS Ubuntu 26.04.1 LTS、Python 3.14、systemd 259.9。
clientは既存のv0.0.18、source `274a5588414819f063d27100a4fb1096aae3830f`を継続使用し、再導入していない。
OpenAI Docsとsystemd 259公式仕様、v0.0.18の公開source/実CLIを照合した。

開始時tracked treeはclean、remoteなし。
前工程の未追跡`docs/config-change-investigation.md`と`docs/phase2-e2-report.md`は変更せず、今回commitへ含めない。
開始時に存在した手動daemon PID 2959466/2959467は停止・再起動していない。
fixtureは独自UUIDの`agentorchestrator-e3-*.service`と新規runtimeだけを操作し、終了時に解放した。

`preservation-check.json`でconfig.tomlのhash、config/auth/AGENTS/history/session_indexのmetadataを開始時と比較した。
認証内容は読まず、config hashは一致、対象metadataも一致。旧testファイルのhashと前工程未追跡2文書も一致。
実サービス2件は引き続きLoadState=not-found、inactive。
実用daemon.env、実用credential file、`.runtime/systemd-e3`は未作成。
既存daemon/Codex IPC/内部履歴DBには接続・変更していない。

## 変更内容

- `systemd/agent-orchestrator.service`: 専用E3 runtime、有限のsocket readiness確認、KillMode=mixed、起動回数制限、Install section削除。
- `systemd/aceis-mcp-tunnel.service`: native client前景実行、daemon依存、LoadCredential＋file参照、loopback health、有限のpoll待ち、CPU/memory/process制限。
- `systemd/aceis-mcp-tunnel.yaml`: E2の既存Tunnelを参照する秘密値なしのservice用profile。元profileを変更しない。
- `scripts/service_wait.py`: socketのUID境界に加え、所有PID/start/boot/runtime/unit祖先関係を検証。healthはprivate URL file、loopback、native health/pollを確認。
- `tests/systemd_fixture.py`: loopbackだけの模擬control plane。synthetic credential、JSON-RPC command/response、通信断注入。
- `tests/test_phase2_e3.py`: 15件。実systemd user transient unitと実clientを使う。実モデル/APIキーは使わない。
- `docs/systemd-deployment.md`: credential手順案、依存関係、停止/復旧、資源、導入承認境界、ChatGPT側の利用者手順。
- README、daemon設計、本報告を更新。

core daemon/scheduler/runner/adapter、DB schema、既存148試験は変更していない。

## テスト結果

| 実行 | 結果 | 証拠 |
| --- | --- | --- |
| 変更前の既存148件 | 148成功、132.768秒 | baseline-tests.txt |
| 最終全163件（旧148＋新15） | **161成功・2失敗、187.537秒、skipなし** | full-tests.txt |
| unit静的verify | exit 0 | unit-verify-final.txt |
| namespace単独実測 | exit 1、uid_map書込み拒否 | usernamespace-diagnostic-correlated.json |

旧148件は最終全試験でも全成功。FIFO試験のEOFは今回のbaseline/最終全試験では再発していない。
E2での一時的EOFの原因を解消したとは扱わない。今回は再発時にfixture出力を保存するTestResultで実行した。

新規15件のうち成功13件:

- 有限のdaemon不在失敗、health URL境界、poll必須、キー環境変数を渡さないhealth確認。
- unitのfile credential/read-only/手動専用設定。
- systemd transient idle daemonの起動・停止・再起動と実cgroup値。
- native clientの起動・停止・再起動、daemon依存の停止/手動restart伝播。
- credential欠落/空fileでの起動拒否。unitの`%d`展開をprestart propertyでも検証。
- 実client経由initialize(2025-11-25)、tools/list読み取り3件のみ、orchestrator_status成功。
- synthetic key値がclient argv/env/journalに含まれないこと。MCP child/health file解放。
- pause済みQUEUEDとfixture UNKNOWNのunit再起動後保持、execution数0。
- 模擬control plane切断/再起動失敗後もtask数0、勝手な投入なし。

失敗2件は削除・skip・assert緩和していない:

1. `test_systemd_sigterm_cancels_running_tasks_and_releases_scopes`
2. `test_systemd_sigkill_restarts_and_reconciles_without_replay`

両方とも**障害注入前の準備で失敗**。systemd配下のworkerが隔離`--version`でFAILEDになり、turn開始へ到達しない。
従って実行中タスクのSIGTERM解放とSIGKILL後UNKNOWN復旧をE3で確認したとは報告しない。
元の前景runnerでの同機能の既存回帰試験は成功したが、systemdでの検証を代替しない。
fixture DB診断とunit journalも保存した。

再現コマンド:

```bash
systemd-analyze --user verify systemd/agent-orchestrator.service systemd/aceis-mcp-tunnel.service
python3 -B -m unittest discover -s tests -v
```

このホストでは後者はexit 1が現時点の実測結果。
既存のunrelated spice-vdagent user unitにStandardErrorのsection警告があるが、今回のunitの構文エラーはない。
既存systemd設定は修正していない。

## ブロッカーの根拠と判断

専用transient unitの最小`unshare --user --map-root-user --mount /usr/bin/true`でexit 1。
同じunshare PID 3038865についてkernel監査ログを照合した。

```text
profile=unconfined → userns_create → unprivileged_userns
apparmor=DENIED operation=capable capability=21 capname=sys_admin
/proc/self/uid_map: Operation not permitted
kernel.apparmor_restrict_unprivileged_userns=1
```

agentのIDE実行コンテキストは`snap.code.code (complain)`、systemdの最小probeは`unconfined`だった。
前景テスト成功をユーザーサービスにも一般化できない。
このnamespace拒否は実測で確認済み。ホスト全体のAppArmor policyの完全性や、適切な専用profileの実装方法は未検証。
Ubuntu公式資料も、unprivileged user namespaceがprofileによる明示的許可を必要とすることを説明している。
[公式AppArmor仕様](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/#apparmor-unprivileged-user-namespace-restrictions)

解消には管理者レビュー付きの専用entrypoint/AppArmor policyの設計・検証が必要と判断する。
現段階でsysctl無効化、profile complain化、汎用Python/unshareへの許可、root化、隔離fallbackは実施しない。
その具体設計・適用の承認は未取得であり、ここを停止点とする。

## 試験中に修正した事項

1. E2 profileのenvキー参照はCLI overrideより先に解決され、キーenv不在ではfile引数があっても起動不能。
   失敗doctor原出力を保持し、秘密参照をunitだけへ置くservice profileを追加。synthetic fileによるdoctorはexit 0。
2. installed systemdがType=simple＋credentials＋ExecStartPostに警告。TunnelをType=execとしてverify成功。
3. `systemd-run`はargv内`%d`をliteralへescapeするため、fixture初回4件失敗。
   unit propertyの`%d`と、transient argvの`${CREDENTIALS_DIRECTORY}`（directoryパスだけ）の違いを反映した。
   credential値をenvへ渡す修正ではない。修正後native client/MCP転送成功。
4. 新たな実行中タスク試験でAppArmor拒否を発見。失敗を成功したretryで隠さず、原因を独立probeと監査ログで確認した。

これらの旧失敗出力もe3-tests-1/2/3.txt、doctor原出力、unit verify原出力として保持する。

## 認証・接続・未検証事項

キーの実値は未読・未使用・未移行。fixtureの値は公開のsynthetic markerだけ。
LoadCredentialのfile参照は実CLIと実systemdで確認したが、実キーによるE3 unitの認証は未検証。
journal/argv/envへのsynthetic marker非混入は確認。実サービスjournalの秘密非混入は未検証。
raw HTTPログを無効化し、journal出力をwarnへ抑える。従来のE2 profileと証跡は変更していない。
MCPは2025-11-25＋initialized通知shimで実clientの模擬転送に成功した。
OpenAI hosted経由のツール発見・呼出し、実サービスreadiness、ChatGPT UI/認証設定/実呼出しは未検証。
新規Tunnel、組織/workspace関連付け、課金・権限変更、systemd enable/lingerは実施していない。
モデル生成0回、Platformモデル推論API呼出し0回。

[OpenAI公式接続ガイド](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)では、
ChatGPT Pluginsのcustom MCPでTunnelを選択する。workspace許可とTunnels Read＋Use/関連付けは別に確認する必要がある。
具体的な利用者手順とstatus照合方法は[systemd導入設計](systemd-deployment.md)へ記載した。
認証済みブラウザの操作は利用者へ引き継ぐ。

## 次の判断とGit

まず専用AppArmor policyを検討するか、ここで導入を保留するかを利用者が判断する。
ホスト変更を承認なしで実行しない。Stage A全試験成功後、実サービス2件の導入先・非秘密設定・credential手順を再提示し、
Stage Bの明示的承認を別途得る。自動起動は引き続き別承認で、今回有効化しない。

今回変更10ファイルだけをindexへ登録した。index全72ファイルの秘密形式・パス・blob監査はpassed、findingsなし。
差分とhook不在も確認した。未解消の2失敗を明記した作業記録としてSSH署名付きローカルcommitへ保存する。
既存未追跡報告2件は含めず、remote登録・push・公開は行わない。
