# パス表記と実行例の変数

文書中の`<PROJECT_ROOT>`等は説明用の疑似パスです。山括弧のままshellへ貼り付けず、実行例では対応する引用付き変数を使います。過去の検証報告もこの表記へ匿名化し、原本ログ・実行中の設定・認証ファイルは変更していません。

| 説明用の表記 | shell変数 | 意味 |
|---|---|---|
| `<PROJECT_ROOT>` | `$PROJECT_ROOT` | このrepositoryのcheckoutの絶対パス |
| `<STATE_DIR>` | `$STATE_DIR` | このprojectの`.runtime/`内にある専用実行状態directory |
| `<MODEL_DIR>` | `$MODEL_DIR` | 導入先で管理するモデルdirectory |
| `<INFRA_ROOT>` | `$INFRA_ROOT` | 別管理のinfrastructure repository |
| `<USER_HOME>` | `$HOME` | 操作者のhome。特定ユーザー名を含めない |
| `<CODEX_BINARY>` | `$CODEX_BINARY` | 導入済みCodex executableの絶対パス |
| `<TUNNEL_CLIENT>` | `$TUNNEL_CLIENT` | 導入済みTunnel client executableの絶対パス |
| `<CONTROL_PLANE_KEY_FILE>` | `$CONTROL_PLANE_KEY_FILE` | repo外の既存private credential fileのパス。内容は展開しない |

例として以下の配置を使います。別の配置なら、使う変数だけ導入先のパスへ設定してから、その文書の実行例を読みます。これはファイルを作成・インストールする操作ではありません。

```bash
export PROJECT_ROOT="${PROJECT_ROOT:-$HOME/src/agent-orchestrator}"
export STATE_DIR="${STATE_DIR:-$PROJECT_ROOT/.runtime/example}"
export MODEL_DIR="${MODEL_DIR:-$HOME/models}"
export INFRA_ROOT="${INFRA_ROOT:-$HOME/workspace/project-kiss-infra}"
export CONTROL_PLANE_KEY_FILE="${CONTROL_PLANE_KEY_FILE:-$HOME/.config/agent-orchestrator/credentials/control-plane-api-key}"
export CODEX_BINARY="${CODEX_BINARY:-$(command -v codex)}"
export TUNNEL_CLIENT="${TUNNEL_CLIENT:-$(command -v tunnel-client)}"
cd "$PROJECT_ROOT"
```

Codex/clientがPATHにない場合は、対応する変数へ実際の導入済みexecutableの絶対パスを指定します。`STATE_DIR`は`PROJECT_ROOT/.runtime/`配下とし、既存の仕事を所有するdirectoryへ不用意に切り替えません。過去報告中のphase/run名は証拠の識別子で、別環境にもそのdirectoryが存在するという意味ではありません。

`/usr/bin`、`/etc`、`/proc`、`/sys`、`/dev`など、仕様上必要な標準OSパスは置換しません。`%h`・`%t`・`%d`はsystemdのspecifier、`@{AO_PROJECT}`はAppArmor草案の変数であり、shell変数とは別の構文です。

稼働用Tunnel設定はローカル保持・Git除外し、例を実値へ置換してから利用します。草案や文書を直しただけで稼働設定が変更されたとは扱いません。namespaceを作る手順はCodex隔離内から実行せず、既存の追加隔離禁止を維持します。

コマンド例の`TASK_ID`・`TASK_A`・`SNAPSHOT_ID`・`DECISION_ID`等は、対応する直前のコマンドが返したIDを設定して使います。`BACKUP_FILENAME`は確認済みのbackup名です。未設定のまま実行したり、説明用の`<task_id>`をshellへ貼り付けたりしません。
