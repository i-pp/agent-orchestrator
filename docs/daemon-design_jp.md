# 常駐daemon設計 — Phase 2-D2

パスは[表記規約](path-conventions.md)の疑似パスです。シェル例は同ページの変数を設定してから使用します。原本ログは変更していません。

## 実行と導入境界

daemonは前景のイベントループとして動作し、既存runnerを独立workerとして呼ぶ。
Python 3.14標準ライブラリ、App Server stdio、所有SQLite、namespace、受入検証は維持する。
HTTP／MCP／外部ネットワークlistener、ChatGPT接続、ローカルLLM接続は実装しない。

```bash
cd "$PROJECT_ROOT"
python3 -B src/runner.py --runtime .runtime/daemon-demo daemon
```

この手動起動はsystemd user **scope**へ入れて資源を制限する。serviceのinstall／enable／startはしない。
前景helper→専用resource supervisor→daemon→workerという親子関係を保ち、親死亡guardを継承する。
別端末からshutdown、または前景helperへのSIGINT／SIGTERMで停止する。

`systemd/agent-orchestrator.service`は将来の手動導入用のuser unit定義。
既存同名unitと設定の有無を確認し、operatorが内容をレビューしてから導入する。
EnvironmentFileは`~/.config/agent-orchestrator/daemon.env`で、`CODEX_BINARY=/absolute/path/to/codex`だけを明示する想定。
リポジトリはそのファイルを作成・上書きしない。APIキーを渡す用途ではない。
WorkingDirectory／ExecStart／runtimeの位置も配置先に合わせて確認する。

Phase 2-E3でInstall sectionを取り除き、手動起動だけを対象とする構成にした。
自動起動を将来有効にする場合はWantedBy等を別途レビューし、別承認を必要とする。
Restart=on-failure、RestartSec=2、StartLimitIntervalSec=600、StartLimitBurst=3で再起動を制限する。
CPUQuota=50%、MemoryMax=512M、TasksMax=64、swapなし、core dumpなし、LimitFSIZE=64Mを定義する。
Unitの停止猶予は60秒。既存systemd設定、system service、root、lingerは変更しない。

**実運用unitはインストール・enable・startしていない。**
E3では専用transient fixtureのuser serviceでRestart/依存停止/credential/資源制限を確認した。
ただしsystemd配下ではAppArmorがuser namespaceを拒否するため、実行中タスクの停止・復旧試験は失敗した。
Stage A全成功と実運用導入は未達成。詳細と承認境界は[systemd導入設計](systemd-deployment.md)を参照する。
[systemd.service公式仕様](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)に基づく設計で、前景再起動試験と実serviceの実測を区別する。

## 制御socket

`/run/user/<uid>/agent-orchestrator/<runtime絶対パスのhash>.sock`を使用する。
長いruntime名でもUnix socketのパス上限を超えない。TCPへfallbackしない。
user runtimeと専用directoryはowner UID・0700、socketはowner UID・0600を検査する。
clientとserverの双方でLinux SO_PEERCREDを読み、同じUIDだけを許可する。
同じhost UIDを自由に操作するoperatorは信頼境界の内側であり、別利用者を認証するサービスではない。

daemon.lockをruntime単位でexclusiveに保持し、二重daemonを拒否する。
古いsocketは、lease取得後にtype・owner・mode・接続不能・inode不変を確認した場合だけ取り除く。
regular file、symlink、広すぎるmode、接続できるsocketは置換しない。
正常終了時も自分がbindしたinodeだけをunlinkする。既存Codex IPCは対象にしない。

モデルとverifierには制御socketやuser busを渡さない。
isolated workspaceの最小rootでは/run/userが存在せず、旧read-only namespaceとlegacy verifierでも/run/userを空mountで隠す。
同じUIDというだけでモデルの制御アクセスを許可しない。mount境界を実OSで検証する。
[unix(7)](https://man7.org/linux/man-pages/man7/unix.7.html)のpathname socket権限とSO_PEERCREDを参照した。

## プロトコル

1接続で1つのJSONL request／response。requestはid、method、paramsの3項目だけ。

```json
{"id":"request-id","method":"status","params":{}}
```

| method | 操作 |
| --- | --- |
| submit | taskをQUEUEDへ保存。安定したrequest_keyが必須 |
| status | daemon状態、またはtask_idで特定taskの根拠 |
| list | FIFO順の一覧。limit=1..100、offsetでページング |
| cancel | QUEUED取消／実行中の中断要求。UNKNOWNの自動再実行はしない |
| pause | 新規dispatchを停止。実行中は継続。SQLiteへ保存 |
| resume_scheduler | pause解除。安全条件を満たすQUEUEDだけを取得 |
| shutdown | 新規dispatchを止め、所有workerを安全に停止 |

submitは既存Store.submitの固定受入条件・workspace・role・resume許可を使用する。
Socketの既定workspaceは独立したread-only。任意のコマンド／実行ファイルを指定するmethodはない。
request_keyとtimeoutを含むtask定義の保存は同じSQLite transaction内で行い、応答が失われても同じkeyで重複を防ぐ。

clientは接続前のbacklog混雑だけを期限付きで待つ。送信後の切断を理由にsubmitを自動再送しない。
既存CLIでは`--via-daemon submit/show/cancel`と`status/list/pause/resume_scheduler/shutdown`を利用する。
CLIが生成したrequest_keyは失敗応答にも表示するので、再投入では同じkeyを使う。

selectorsによるnonblocking I/Oを使う。最大64接続、request 128 KiB、response 4 MiB、clientの入出力待ちを制限する。
不正JSON、途中切断、サイズ超過、未対応methodでdaemonを終了しない。
ログにrequest本文やworkerのstdoutを重複記録せず、task ID・操作・終了情報を記録する。
大きすぎるstatusはローカルshowへ誘導し、任意の内容を切り貼りして成功応答にしない。

## 資源制限と容量

scheduler-config.jsonにdaemon／workerのCPU・memory・TID・CPU累積予算と各期限を定義する。
resource-limits.jsonのタスク本体・verifier上限は変更しない。

| 役割 | CPU帯域 | memory | TID | CPU累積予算 |
| --- | --- | --- | --- | --- |
| daemon | 50% | 512 MiB | 64 | 24 CPU時間／1起動 |
| worker | 100% | 256 MiB | 32 | 60 CPU秒／task |
| Codex／verifier | 既存policy | 既存2 GiB | 既存512 | 既存60 CPU秒／scope |

taskの短いCPU予算と長寿命daemonの予算を分ける。0や無制限値で機能を外す設定はない。
user scopeに入れて実効値を確認してからdaemonを開始し、直接の--serveで制限値が合わなければ拒否する。
workerとCodexは別scopeへ移し、daemonの512 MiB枠へ2つのCodexを押し込まない。
service導入時は前景helperもunitの制限下となる。手動起動時の外側helperは小さな前景制御processである。

daemonの外側監督は全runtime容量を理由に制御socketを失わせず、scheduler側が新規dispatchを停止する。
task／workerは従来の保存予算監視を維持する。daemon自身のJSONLと1ファイルの上限も維持する。
**総容量は監視予算でhard quotaではない。** 厳密quotaを必須にする運用はfilesystem／権限の設計・承認・実測前に開始しない。
自動prune、既存ログ・UNKNOWN証拠の削除はない。[保持方針](retention-policy.md)を参照。

## 停止と障害

正常停止はdispatch停止→所有taskのcancel要求→runnerのturn/interruptと検証終了待ち→期限超過時だけ強制解放の順。
未取得QUEUEDはそのまま残す。実行中taskが中断した事実・解放が確定すればCANCELLED、不明ならUNKNOWN。
前景helperのSIGTERMも先にsocket shutdownを要求し、scope supervisorを早期に殺して完了記録を失わないようにする。

SIGKILLでは親死亡guardがworkerとその子へ伝播する。daemon再開時はlease・PID/start/boot・worker scope・Codex scopeを照合する。
停止途中のlive ownerを先走って変更しない。既存daemon、別runtimeのprocess、無関係なunitを停止しない。
SQLiteロックは期限付きで応答／dispatchを延期し、taskを重複取得しない。
daemon／workerの強制停止、socket断、SQLite競合は専用fixtureと実OSで試験する。停電・OS再起動は行わない。
