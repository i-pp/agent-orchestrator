# ログイン後のMCPサービス自動復帰

## 現在の適用状態（2026-10-30）

利用者の明示承認（06:59 UTC確認）により、新規4ファイルと専用login入口unitを配置し、daemon-reloadとenable（--nowなし）を実施済み。入口はloaded/enabled/inactive、LoadErrorなし、実行0回。既存daemon/TunnelのPID・InvocationID・health・DB・key参照・設定・grantは不変、linger=no。新規20＋既存33＝53検査PASS。実logout/loginでの自動復帰は未検証であり、適用成功と区別する。

最初のunitはWorkingDirectoryをshell風に引用してbad-settingとなったため、不要なWorkingDirectory指定を削除し回帰検査を追加した。入口を一度も実行せずに修正・reloadし、実user managerのLoadState=loaded/LoadError空を確認した。初回失敗を隠さず適用記録に保存する。単独systemd-analyze verifyは実行環境のsocket EPERMで未検証のまま。現用サービス停止・再起動・model/GPU操作は0。

以下は構成と今後の導入・取消手順であり、この文書を読んだだけで再実行しない。

現用の専用daemon/Tunnelを止めずに準備する。最小案はpersistentな**入口user unit 1件**を通常の`systemctl --user enable`でdefault.targetへ登録し、user manager起動時に既存`poc_services.Services`を1回呼ぶ構成。実際のdaemon/Tunnelは従来のtransient unitを維持する。直接persistentな2unitへ交換すると、既存のTransient/nonce/InvocationIDによる所有判定を変更する必要があるため、この工程では行わない。

| 構成 | 動作 | 今回 |
|---|---|---|
| persistent login入口＋enable、Linger=no | user managerのログイン時起動で既存2サービスを復帰。最後のログアウトで停止し得る | 準備対象 |
| linger有効 | user managerをboot時にも起動し、ログアウト後も維持する。ログインしていない期間もサービス・キー使用が続く | 設定しない。別途具体的承認 |

OS起動直後からの無人常駐や、既存managerを保った単なる画面ロック解除ごとの起動ではない。複数login中はmanagerが存続し、この入口も一度だけ実行される。Lingerの現在値も適用時に再確認する。

## ファイルと強制する範囲

- `scripts/poc_login.py`: plan/render/restoreのみ。install/enable/linger/credential作成機能はない。
- `systemd/agent-orchestrator-login.example.service`: renderer用テンプレート。`Type=oneshot`、`RemainAfterExit=yes`、`WantedBy=default.target`、Restart=no、開始240秒上限。`ExecStop`なし。
- `tests/test_login_recovery.py`: 実user service・socket・namespace・モデルを使わないfixture試験。

入口名は既存runtime名のhashから`agentorchestrator-poc-<runtime-hash>-login.service`を作る。現在の承認対象なら`agentorchestrator-poc-365d01e41070-login.service`。既存daemon/tunnel unit名・DB・scope・キー保存先・resource属性・認証を変えない。新しいpublic tool、credential copy、LoadCredential、shell、timer、retry loopは追加しない。

参照するruntime/codex/key path、4 flag、固定mailbox policy hash、grant policy hashをSHA-256で固定し、render時とrestore時に照合する。現在の11操作の組合せだけを受理する。VSCode更新でCodex pathが変わった場合や、正規の新grant採用でもpinが変わるため、無断で新構成へ追従せずBLOCKEDになる。運用者が差分を確認して入口を再renderする。grant expiry後も既存のretained readを維持し、期限・取消・予算をリセットしない。

## ログイン時の状態遷移

1. 保存state/policyと入口のexpected invocation hashを照合する。
2. 同じ所有者の両サービスが既にREADYならALREADY_READY、変更0で終了する。実行中仕事があっても停止しない。
3. 両unitがnot-found、socket/health locatorがなく、stateがREADY（manager消失後の古い記録）またはSTOPPEDの場合だけ復帰を検討する。
4. 既存の操作lock内で状態を再読し、同じpin・unit不在・未解決仕事0を再確認する。元のキー参照を明示して既存preflightを通す。古いREADYだけをSTOPPEDへ整合し、unitをstopしたりlocatorを削除したりしない。
5. 既存Services.startを1回呼ぶ。ここでもlock/所有者/idleを検査し、daemon起動→daemon readiness→Tunnel起動→healthz/readyz/control-plane poll→READYの順序を再利用する。既存のresource上限・clean環境・追加隔離禁止を変えない。
6. 通常の起動失敗は既存Servicesが新しい自分の世代だけcleanupし、FAILEDまたはCLEANUP_UNCONFIRMEDを保存する。入口は再試行しない。

途中のSTARTING/FAILED/CLEANUP_UNCONFIRMED、片方だけ残るunit、foreign/別世代、socket/health locator残存、QUEUED/RUNNING/VERIFYING/UNKNOWN/WAITING_FOR_HUMANはBLOCKED/UNKNOWNで手動照合を求める。先行起動の状態不明を再送や「別IDで再実行」に置き換えない。lock解放後に他の正規CLIが先に起動しても、startはそれを停止・採用・再試行しない。

この入口は監視daemonではない。RemainAfterExitのactive表示だけでMCP正常とは判定せず、既存status/healthを使う。通常の例外cleanupは試験するが、入口自身のSIGKILL/開始上限到達/manager停止中に全cleanupを保証しない。その場合は既存の不完全stateを保持して次回も自動startを拒否する。永続unitによる追加の隔離環境は作らない。

## 適用前に揃えるもの

1. この4ファイルをレビュー済みの同一内容でrepoへ追加する。現用service再起動は不要。
2. `RUNTIME`を既存runtime、`PREPARED_UNIT`を許可された作業領域内の対象unit名のファイル、`UNIT_NAME`を上記の計算済み名に設定する。新runtimeやkeyを作らない。
3. 以下はrenderと静的検査だけで、現用を停止・起動しない。

```bash
python3 -B scripts/poc_login.py plan --runtime "$RUNTIME"
python3 -B scripts/poc_login.py render --runtime "$RUNTIME" > "$PREPARED_UNIT"
systemd-analyze --user --generators=no --man=no verify "$PREPARED_UNIT"
```

renderは実pathを含むlocalファイルを生成するためGitへ収録しない。既存unitテンプレートは疑似pathのまま保持する。`plan`のpinとrender結果のpin、既存key参照・4 flags・2 policy hashを確認する。実keyの内容を表示・copy・環境へimportしない。

## Action-time承認後のmigration

承認対象は、対象repoへの4ファイル追加、`~/.config/systemd/user/agentorchestrator-poc-365d01e41070-login.service`の**新規配置**、daemon-reload、同unitのenable（--nowなし）。次回user manager起動で同じキー参照を使い既存11操作を自動復帰する影響を明示する。ファイルの上書きや新認証・lingerは含めない。既存の同名unitがあれば勝手に上書きしない。

適用直前に現用status/active jobsと親のGPU試験予定を照合する。配置とenableのみなら既存2サービスを止める必要はない。サービスを実際に停止・再起動して試す段階は親と時刻を調整し、仕事があれば延期する。

```bash
# 対象directoryの所有者・非symlink、同名unitの不在を先に確認する。
install -m 0600 "$PREPARED_UNIT" "$HOME/.config/systemd/user/$UNIT_NAME"
systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"
systemctl --user is-enabled "$UNIT_NAME"
```

ここで`enable --now`、restart、logout、`loginctl enable-linger`は実行しない。既存2サービスのInvocationID・health・DB/grantを前後比較する。

単発no-op smokeを承認した場合のみ、両サービスREADYを再確認して`systemctl --user start "$UNIT_NAME"`を1回実行する。ALREADY_READY・既存2unitのInvocationID不変・モデルturn0を確認する。本当に停止状態から起動できることと実logout/loginは別の検証で、利用者と時刻を調整する。model/GPU評価を兼ねない。

## Rollback

```bash
systemctl --user disable "$UNIT_NAME"
systemctl --user is-enabled "$UNIT_NAME"
```

--nowを付けずに次回起動を解除する。入口にExecStopはないため、現在のdaemon/Tunnel・GPU仕事を止めない。配置ファイルはdisabledのまま保全できる。除去が必要なら、導入時hashと一致する当該ファイルだけを許可されたbackup先へ移し、daemon-reloadする。キー・DB・grant・2transient unit・認証は変更しない。従来のresume-planによる手動復旧に戻せる。

意図的にサービスを停止したまま次回loginも維持したい場合は、入口のenableを先に解除する。期限後のgrant更新や新しい公開範囲への変更を、このrollback/再renderの副作用として行わない。

## 検証の限界と後続条件

mock/unit testsは53件PASS。user unitの配置・enable・実managerによる読み込みは確認済み。単独systemd-analyze verifyは環境のsocket EPERMで未検証、実際の自動ログイン復帰・ログアウト中の存続も未検証。現用2サービスをテストのために止めない。networkが既存75秒のTunnel readiness期限に間に合わない場合は有限に失敗し、自動retryしない。日常運用でこの失敗が観測された場合に、回数/時間上限付き再試行を別課題として検討する。

公式仕様（導入済み259系列と照合）:

- [systemd.service: oneshot/RemainAfterExit/timeout](https://www.freedesktop.org/software/systemd/man/259/systemd.service.html)
- [systemctl: enableはstartとは別](https://www.freedesktop.org/software/systemd/man/259/systemctl.html)
- [loginctl: lingerはboot時とlogout後にもmanagerを維持](https://www.freedesktop.org/software/systemd/man/259/loginctl.html#enable-linger%20USER%E2%80%A6)
