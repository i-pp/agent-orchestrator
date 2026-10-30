# systemd user deployment — Phase 2-E3

状態: **導入禁止・Stage A未完了**。unit構文、読み取りMCP転送、credential受渡しはfixtureで確認したが、
systemd配下では既存Linux namespace隔離がAppArmorに拒否される。
以下の導入手順は、隔離の問題を解決し全試験に合格した後、利用者がStage Bを承認した場合だけ実行する。
今回は実サービスの配置、秘密値の移行、手動start、enable、linger変更を実施していない。

## 構成と境界

```text
ChatGPT → 既存OpenAI Tunnel → aceis-mcp-tunnel.service
                              tunnel-client v0.0.18
                                └─ stdio mcp_adapter.py（read-only）
                                     └─ private Unix socket
                                          └─ agent-orchestrator.service
                                               foreground helper
                                                 └─ guarded daemon scope
                                                      └─ worker / App Server scopes
```

unitはリポジトリの`systemd/`に保存する。daemon本体、FIFO、lease、SQLite、最大2件dispatch、
UNKNOWN保護、workspace隔離、受入検証は変更していない。専用runtimeは`.runtime/systemd-e3`。
既存E2の手動daemon/runtimeには接続・停止・移行しない。

`After=`に加えてdaemonの`ExecStartPost`が、private socket、status、runtime、PID/start時刻/boot ID、
unit MainPIDとの祖先関係を確認する。Tunnelの`ExecStartPre`も同じ確認を行う。
別unitや手動daemonのsocketを準備完了と誤認しない。
Tunnelの`ExecStartPost`はnative `health --require-control-plane-poll --json`でhealthz・readyz・poll成功を確認する。
health URLはowner UID、private regular file、symlinkなし、loopback HTTPのみを許す。
起動時の準備待ちは有限で、失敗すればunitを停止する。

Tunnelは`BindsTo`＋`After`＋`PartOf`でdaemonへ依存する。
手動daemon stopでTunnelも停止、手動daemon restartでTunnelも再起動することをfixtureで確認した。
Tunnelだけの停止・通信断はdaemonやtaskを停止・再投入しない。
**daemonの異常終了に伴う依存停止後、Tunnelが自動復帰することは保証しない。**
operatorがdaemon readinessとUNKNOWN照合を確認し、必要ならTunnelを明示的にstartする。

## 既知のブロッカー

systemd transient user unitの`unshare --user --map-root-user --mount /usr/bin/true`はexit 1となった。
`/proc/self/uid_map`への書込みが拒否され、同一PIDのkernel監査に以下を確認した。

- `unconfined`から`unprivileged_userns`へのuserns作成時の遷移。
- `apparmor=DENIED`、`operation=capable`、`capname=sys_admin`。
- `kernel.apparmor_restrict_unprivileged_userns=1`。

IDE配下のagentは`/proc/self/attr/current`が`snap.code.code (complain)`で、既存148試験が成功する。
その実行コンテキストをsystemdに持ち込んで回避してはならない。
本unit内でdaemonがidle起動できることは、Codexタスクの隔離起動を保証しない。
実行中タスクのsystemd SIGTERM/SIGKILL試験は準備段階でFAILEDとなり、未達成。

Ubuntuはunprivileged user namespaceをAppArmor profileで制御する。
[Ubuntu公式仕様](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/#apparmor-unprivileged-user-namespace-restrictions)

提案は、管理者レビューを受ける専用entrypoint/profileの設計である。
対象をこのrunnerに限定し、userns作成と既存mount/pid/ipc/network隔離、workerへのprofile継承を検証する必要がある。
Python全体・unshare全体への許可、sysctl無効化、complain化、setuid/root起動、namespaceを省くfallbackは採用しない。
**profile案の具体化・適用は未実施。ホストセキュリティ変更は現在の承認範囲外。**

## credentialとprofile

元E2 profileは変更しない。元の`api_key: env:CONTROL_PLANE_API_KEY`はCLI上書きより先に解決されるため、
file参照のCLI引数だけを追加してもキー環境変数不在で失敗した。
`systemd/aceis-mcp-tunnel.yaml`は同じ既存Tunnel・legacy通知shimを使う秘密値なしのservice用profile。
唯一のstdio command、credential参照、loopback healthをunitの固定引数で指定する。
`--allow-writes`、追加channel、任意shellツールは公開しない。

`LoadCredential=control-plane-api-key:...`でsystemdのprivate credential fileへ渡し、
`--control-plane.api-key=file:%d/control-plane-api-key`でnative clientが読む。
credential内容を環境変数やargvへ展開しない。`%d`はunit内でsystemdが展開するdirectory指定である。
`LoadCredential`だけでは環境変数のキー値は設定されない。
元ファイル→systemd管理のruntime credentialへの複製はこの方式の動作であり、手動の認証ファイルcopyはしない。
[systemd 259 credentials](https://www.freedesktop.org/software/systemd/man/259/systemd.exec.html#Credentials)

キー値のないenv、dummy credentialだけでnative CLI doctor成功と実転送を確認した。
実キーの移行は利用者の操作を待つ。managerへのimport-environment/set-environmentは行わない。
`UnsetEnvironment`でキーの別経路やMCP target環境変数による意図しない上書きを抑止する。
clientはType=execとし、credentials＋ExecStartPost＋Type=simpleのsystemd警告を避ける。

## 資源・停止・復旧

| 対象 | CPU quota | MemoryMax | swap | TasksMax |
| --- | --- | --- | --- | --- |
| daemon foreground unit | 50% / 100ms | 512 MiB | 0 | 64 |
| daemon自身の既存scope | scheduler-config.jsonの50% | 512 MiB | 0 | 64 |
| Tunnel＋stdio MCP | 25% / 100ms | 256 MiB | 0 | 64 |
| worker/App Server | 既存scheduler-config.json / resource-limits.json | 既存値 | 既存値 | 既存値 |

外側unitと内側scopeは同一cgroupではない。workerは既存private sliceの独立scopeで制限される。
unitのMemoryMaxだけで全worker合計を制限したとは扱わない。親死亡guard、既存scope cleanup、資源receiptを維持する。
fixtureでunitのcpu.max、memory.max、pids.max、memory.swap.maxを実測した。OOM等の全異常系はE3で新規実測していない。
資源超過をtask成功とせず、不明な結果はUNKNOWN。証拠の自動削除なし。総disk容量は監視予算のままでhard quotaではない。

`KillMode=mixed`はまずMainPIDだけへSIGTERMを送り、前景helperから既存shutdownを行わせる。
最初からcontrol-group全体へSIGTERMを送ってguard/workerを同時に失わせることを避ける。
停止猶予はdaemon 60秒、Tunnel 20秒で、その後systemdが残存unit processを強制終了する。
独立scopeの解放は既存guardとreceiptで確認する必要がある。
Restart=on-failure、10分に3回までの起動制限を設ける。
両unitともInstall sectionを持たず、今回の自動起動を有効化できない構成とする。

## 承認後の導入手順案（現在は実行しない）

最初にAppArmorブロッカーを解決し、失敗した2試験を含む全試験に合格すること。
その後、配置先・設定・起動対象を明示してStage Bの承認を別途得る。

1. `systemctl --user show agent-orchestrator.service aceis-mcp-tunnel.service`と`systemctl --user cat`で、既存unit/drop-inがないことを再確認する。
   同名unit、設定ファイル、専用runtimeが既存なら上書きせず停止する。既存手動daemonの停止は不要。
2. 配置予定先は`~/.config/systemd/user/agent-orchestrator.service`と`~/.config/systemd/user/aceis-mcp-tunnel.service`。
   非秘密設定は`~/.config/agent-orchestrator/daemon.env`に`CODEX_BINARY=/実在する絶対パス/codex`の1行。
   `~/.config/agent-orchestrator/`は0700、daemon.envは0600。既存Codex設定や認証を変更しない。
3. 利用者が既存キーを自身の端末でcredentialへ渡す。予定先は
   `~/.config/agent-orchestrator/credentials/control-plane-api-key`（directory0700、file0600）。
   以下は既存のexport済み`CONTROL_PLANE_API_KEY`を使い、値を表示せず、既存fileやsymlinkを上書きしない例。
   **現段階では実行しない。Codexにキーを入力しない。**

```bash
# 利用者端末だけで、承認後に実行する
python3 - <<'PY'
import os
from pathlib import Path
key = os.environ.get('CONTROL_PLANE_API_KEY')
if not key or '\n' in key or '\r' in key:
    raise SystemExit('A valid existing key is required; no value printed')
root = Path.home() / '.config/agent-orchestrator/credentials'
# 親directoryは所有者・symlink・modeを別途確認済みであること
root.mkdir(mode=0o700, parents=True, exist_ok=False)
fd = os.open(root / 'control-plane-api-key', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    with os.fdopen(fd, 'wb') as stream:
        stream.write(key.encode())
finally:
    key = None
PY
unset CONTROL_PLANE_API_KEY
```

4. unitを排他的に新規配置して`systemd-analyze --user verify`、`systemctl --user daemon-reload`を行う。
   `enable`、`enable --now`、`loginctl enable-linger`は禁止。
5. 承認された実サービスだけを順次手動起動する。

```bash
systemctl --user start agent-orchestrator.service
python3 -B scripts/service_wait.py daemon --runtime .runtime/systemd-e3 --unit agent-orchestrator.service --timeout 20
systemctl --user start aceis-mcp-tunnel.service
~/.local/opt/tunnel-client/v0.0.18/tunnel-client health \
  --url-file "$XDG_RUNTIME_DIR/aceis-mcp-tunnel/health-url" --require-control-plane-poll --json
```

6. unit状態、health/poll、stdio child、readonly tool discoveryを別々に記録する。
   journalはraw HTTPを有効化せず、秘密形式の検出は一致値を出力しない方法で行う。
   native clientの停止時WARNだけを接続失敗と断定しない。

停止はTunnel→daemonの順で、**導入したこの2サービスだけ**を対象にする。

```bash
systemctl --user stop aceis-mcp-tunnel.service
systemctl --user stop agent-orchestrator.service
```

再起動はdaemon readiness、UNKNOWN/QUEUED、scope解放を確認してからTunnelをstartする。
UNKNOWNをQUEUEDへ書き換えたり、自動resumeしたりしない。
撤回時も今回新設したunitだけを停止・取り外してdaemon-reloadし、runtime/DB/証拠/credentialを削除しない。
今回それらの操作は行っていない。

## ChatGPT側の利用者手順（Stage B成功後）

認証済みブラウザの操作は利用者へ引き継ぐ。
対象workspaceのPluginsで「+」→「Add custom MCP server」→Connection「Tunnel」を選ぶ。
既存`aceis-mcp-dev`（ID `tunnel_6ae1d745be7c8191b6d502b613317b4c`）を選び、新しいTunnelは作らない。
MCP側認証の選択肢は実画面で確認する。本adapterは独自OAuthを実装していないため、対応する設定がなければ停止して報告する。
Tunnel runtimeキーをChatGPTのフォームへ入力しない。
workspaceのcustom MCP許可、対象Tunnelの関連付け、Tunnels Read＋Useが必要。
一覧に出なければ管理者へ確認し、今回の範囲で関連付けや権限を変更しない。
[OpenAI公式Tunnel接続手順](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels#connect-from-chatgpt)

Create前に読み取り3ツールだけであることを確認する。submit/cancelが見えたら接続を中止する。
ChatGPTへ`orchestrator_status`だけを1回呼び出すよう指示し、時刻・返却JSON・MCP call_idを保存する。
直近のlocal socket statusのtask_counts、maximum_sessions、paused等と照合する。
ChatGPTの文章だけでなく、runtimeの`logs/mcp-*.jsonl`のtool開始/終了と対応することを確認する。
この証拠がない限りChatGPT実呼出し成功と報告しない。
