# Workspace隔離設計 — Phase 2-C

## 対象と信頼境界

新しいisolatedモードは、同じruntimeのCodexタスクが相互のworkspace・状態・ログを参照／変更できない構成とする。
ローカルoperator、ランナーのソース・許可コマンド、Codexバイナリ、OS/kernelは信頼する。
同じホストUIDを自由に操作できる悪意あるoperator、kernel脆弱性、CPU／メモリ枯渇を防ぐVM相当の境界ではない。
旧モードは共有workspaceと既存read-only動作を保持し、同時実行しない。強い分離には`--isolated`を使う。

## 所有と配置

```text
<runtime>/
  tasks.sqlite                 # coordinatorのみが操作
  runner.lock / leases/        # 実行権限のlease
  fixtures/<name>/             # operatorが用意する専用入力
  workspaces/<workspace_id>/
    workspace/                 # タスクの成果物・検証対象
    codex-home/                # そのworkspaceだけのCodex状態
    tmp/jail/                  # 空mountpoint。jail本体はnamespace内tmpfs
  snapshots/<snapshot_id>/     # 終了後の独立コピー
  logs/ / db-backups/
```

workspaces表はID・作成owner task・access・状態・元snapshot・初期fingerprint・作成／解放時刻を保存する。
tasksとexecutionsにもworkspace_id・role・access・snapshot_idを記録する。
新規タスクは新規workspace、明示resumeだけは元のworkspaceを継承する。ownerからのresume祖先と権限の一致を検証する。
同じworkspaceの同時利用はleaseで拒否する。

入力はruntime/fixtures内の単一名のみ。製品checkoutや任意の絶対パスを入力にする機能はない。
祖先symlink、内部symlink、hardlink、非通常ファイル、path traversal、`/proc/self/mountinfo`上の内部mountを拒否する。
プロジェクト側の`.codex`設定も拒否し、固定した起動方針への上書きを許可しない。
通常ファイル1,000件・各16 MiB・合計約64 MiB・directory 1,000件の上限を適用する。
コピーは新規ファイルへ行い、hardlinkで共有しない。空directoryと所有者の実行bitを保持する。
copy前後とコピー先の通常ファイルmanifestを照合する。

## 実際のLinux境界

1. 専用子プロセスにuser/mount/PID/IPC namespaceを作る。親死亡guard、kill-child、専用procを使う。
2. mount伝播をprivateにし、tmpfsの新しいrootを構成する。
3. `/usr`・`/etc`・標準library、必要なCodex実行ディレクトリをread-onlyでbindする。
4. 自分のsession rootだけをbindする。rootはread-only、専用CODEX_HOMEはCodex用に書込み可、workspaceはタスクのaccessに従う。
5. 認証は既存auth.jsonをread-only bindする。Pythonで内容を開く・複製する処理はない。元HOMEはmountしない。
6. 最小dev、専用proc/tmp/var-tmp、必要なDNS resolverファイルのみを作る。
7. `pivot_root`し、旧rootをdetachする。host cwd／source FDを残さない。
8. CAP_SYS_ADMIN/SYS_CHROOT等を落とす。入れ子のuser namespaceに必要なSETUID/SETGID/SETFCAPだけをbounding setへ残す。
9. この外側境界の中でCodex自身のread-only／workspace-write sandboxも使う。

他workspace・coordinator DB・既存HOME・既存daemon socketは新しいrootに存在しない。
`..`、絶対path、symlink先、`/proc/1/root`でも旧rootへ戻れないことを実OSテストで確認する。
別のmount namespaceで作るため、元ホストのmount構成は変更しない。

`pivot_root`によるroot変更と旧rootの取り外しは[Linux man-pages](https://man7.org/linux/man-pages/man2/pivot_root.2.html)を参照。
chroot内からのuser namespace作成制約は[unshare(2)](https://man7.org/linux/man-pages/man2/unshare.2.html)に記載される。
初期案のchrootはこの制約で失敗した。権限を緩める代替は使わず、pivot_rootと入れ子sandboxの実測で確認した。

## read-onlyとworkspace-write

既定はread-only。workspace自体をread-only mountし、モデルの指示に依存しない。
workspace-writeは明示タスク＋専用fixtureだけ。成果物SHA-256と許可済みテストを必須にする。
threadの実効sandbox／approvalを確認し、turn/startにも次を明示する。

```json
{"type":"workspaceWrite","writableRoots":["<own-session>/workspace"],
 "networkAccess":false,"excludeSlashTmp":true,"excludeTmpdirEnvVar":true}
```

外側のprivate tmpfsは実行基盤用。モデルの書込み許可は自身のworkspaceだけ。
既存authのmountはread-onlyで、認証更新が書込みを必要とすれば失敗する。
隔離コマンドの実行ファイルは/usr配下のsystem programまたは現在インストール済みのCodexだけを許可する。別workspaceを実行資源としてmountする経路を拒否する。
trusted App Serverは認証を使用するが、独立検証コマンドのrootではCODEX_HOME全体を空mountで隠し、network namespaceも分離する。
namespace／nested sandboxのpreflightに失敗した場合はdispatchしない。danger-full-accessや通常起動へのfallbackはない。

[公式App Server資料](https://learn.chatgpt.com/docs/app-server)とCLI生成schemaを照合した。
現バイナリは上記の5項目を持つが、最新資料のreadOnlyAccess制限は生成schemaに存在しない。
未実在の項目は送らず、外側mountで読取りを制御する。experimentalApi=falseのままとする。

## 独立レビューと解放

Implementerの既知の終了状態・資源解放・workspace leaseを確認してsnapshotを作る。
snapshotはsource task/execution/workspace IDとfingerprintを持ち、ファイルをread-onlyにする。
Reviewerにはsnapshotから別workspaceへのコピーを渡す。元の実装途中workspaceはmountしない。
role=reviewerはread-only＋snapshot必須で、fixture入力・書込み権限を拒否する。
内容の改変は投入時／起動時に検出し、拒否する。

Reviewerは独立したthread／turn／executionと受入検証・result・ログを持つ。
元のImplementerの状態や人間承認は変更しない。指摘の判断・根拠はReviewerの記録から追跡する。
roleはタスクの属性であり、特定モデル／人格への固定ではない。

実行中のleaseとmountは終了・中断・親死亡で解放する。`release`は終了済みworkspaceをRELEASEDへ移し、再利用を拒否する。
QUEUED/RUNNING/VERIFYING/UNKNOWNが同じworkspaceに残る場合は拒否する。成果物・会話・ログは削除しない。
作成途中でDB commitに至らなかった未参照directoryも自動削除・実行せず、人間の調査対象として保持する。

## 限界と将来の選択肢

今回のfixtureは小規模な通常ファイルが対象。Git metadata、特殊ファイル、ACL/xattrの再現は対象外。
fingerprintは通常ファイル名・サイズ・内容hashで、実行bit・空directory・一時的な副作用の証明ではない。
空directoryと実行bitはコピーするが、これらも受入条件に必要なら別途テストへ含める。
同一UIDのoperatorによる同時改変を攻撃者モデルに含めない。
大規模／敵対的入力、cgroupによる資源上限、OS再起動、他kernel・他OSは未検証。
将来のGit worktreeは「専用checkoutを作る入力方式」の候補であり、今回の境界やleaseの代用にはしない。今回は実装しない。

## Phase 2-D1の資源境界

namespaceの外側に専用cgroup scopeを加え、CPU／memory／TIDを制限する。namespaceのmount・capability制限は緩和しない。
詳細とディスク総量保証の限界は[資源制限](resource-limits.md)を参照。
