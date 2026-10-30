# 正規host executorへの段階的配備案（未配置）

現Codex execのsandboxを変えず、ユーザー管理hostの既存agent-orchestrator daemonに実行責任を持たせる案。新しいorchestrator・ユーザー・資格情報・外部listenerは追加しない。ここで書いた配置/再起動/host probeは未実施。

## 調査結果と限界

導入npm launcherはnative binaryを選び、argvと既存環境を引き継いでspawnする。launcherソースにCodex stateのmkdir/write処理はない。package manifestは0.165.0、native Rust実装のsourceは導入物に見当たらず、binary内にPATH alias作成・private app-server socket directory作成・policy/plugin/cache作成のエラー文字列がある。文字列の存在だけで、特定の処理が今回実行されたことや起動必須であることは証明しない。

今回execのmetadata観測ではCODEX_HOME/SQLITE_HOMEは未指定。defaultの`~/.codex`・そのtmp/log/sessionsと`~/.cache`はread-only mount。書込み可否は通常のmode/ownerだけでなくmount制限により不可。authファイル本文は読んでいない。237 byteのstderrから得た固定分類はREAD_ONLY_FILESYSTEMのみで、既知の非致命的PATH alias警告と致命的失敗を分離できない。終了1・stdout 0は確認済みだが、fatalとなった具体的path/syscallは未特定。

[公式state説明](https://learn.chatgpt.com/docs/config-file/config-advanced#config-and-state-locations)と[環境変数説明](https://learn.chatgpt.com/docs/config-file/environment-variables)では、CODEX_HOMEはconfig/auth/log/session/cache等の既存state root、SQLITE_HOMEはSQLite stateの配置先。通常hostでは既存stateへの正規アクセスが必要になる。log/session/SQLite/socketの全てがmetadata probe起動時に必須であるとは断定しない。CODEX_HOMEを一時領域へ移しauthをコピーする方法は今回採用しない。

もう一つの実装不足は実行ファイルの選択。既存serviceは`env -i`でPATHを`/usr/bin:/bin`へ固定し、daemonには承認済み絶対binary pathを渡している。一方、現在のpipeline候補はtunnel配下のadapterでPATH上の`codex`を起動する。npm配下のCLIがそのPATHにあるとは限らず、tunnelの既存256MiB枠にexecutorも入る。現候補のflagをそのまま現用へ付ける手順は不適切。

## 配備先と境界

予定経路は次のとおり。

```
Chat -> 既存認証Tunnel -> MCP Adapter（12操作を検証）
  -> 既存Unix control socket（owner UID / 0600 / peer UID）
  -> 既存daemon内のpipeline gateway
  -> 固定native ELF app-server --listen stdio://
```

同じhost UID・既存Codex login/state・既存Tunnel key参照を使う。keyの値やauthを引数/環境変数へコピーしない。daemonは既存の512MiB/TasksMax枠、tunnelは256MiBのまま。必要なheadroomが未確認ならBLOCKEDにし、無断で増枠しない。新規port、外部WebSocket、systemd/host security変更、linger追加は不要。既存の外向きTunnel接続とCodex認証/metadata/推論の既存通信は残る。

既存daemonのcontrol APIは現在status/list/submit/cancel等で、新pipelineの12操作を直接転送する機能はまだない。この図のdaemon gateway接続は**実装待ち**。既存runnerがapp-serverを起動する構造や、control_socket/transport/resources/cleanupを再利用するが、既存submitが新thread/task APIを提供するとは扱わない。

実装差分を最小にするための残り3点：

1. 運用者が承認したnative binaryの絶対path/hashと既存CODEX_HOMEをhost preflightで確認。read-only/UNKNOWNな実行環境は拒否し、別homeや別credentialへfallbackしない。
2. daemonがgatewayと既知thread/turn、queue/tick、interrupt、子/子孫回収を所有。Adapterは既存UID socketに固定12操作だけを渡す。shell/path/provider等の自由なRPC proxyは追加しない。native実行と既存jobは共有budgetで排他（初期はpipeline同時1＋既存job稼働中は新dispatch待機）。現prototypeのchildだけcloseする実装で、任意子孫の回収まで実証したとは扱わない。
3. 新capability flagのdaemon/adapter双方への伝播、poc_servicesのplan/preflight/recovery、poc_loginのpinとtool数検証を更新。現在のloginは11操作を固定確認するため、23操作を既に復元できるとは扱わない。起動引数だけ手編集してこれを飛ばさない。

## 第1段階：通常hostの固定read probe

`scripts/codex_host_read_probe.py` は渡せる候補。現在の管理execからは起動しない。正常hostと確認できなければ子を作らずBLOCKED。root実行、read-only state、binary hash不一致、wrapper/symlink、他人のreceipt directoryも拒否する。context判定は既存isolation_guardを再利用し、判定を強制HOSTにするCLI flagはない。これはcontextの証拠であり新しい権限付与ではない。

運用者が選ぶ値：`PROJECT_ROOT`＝レビューしたcheckout、`CODEX_BINARY`＝既存native ELF、`CODEX_SHA256`＝承認対象hash、`PROBE_DIR`＝既存owner 0700 directory。~を含む文字列や相対binary pathは渡さない。

```bash
python3 -B "$PROJECT_ROOT/scripts/codex_host_read_probe.py" \
  --binary "$CODEX_BINARY" --sha256 "$CODEX_SHA256" \
  --receipt "$PROBE_DIR/host-read-probe.json"
```

子1回、initialize/initialized/account-read(refreshToken=false)/model-listの1pageのみ。処理45秒alarm、各RPC10秒、回収2秒＋2秒、再試行0、thread/turn作成0。receiptは起動前に0600・排他的に作り、STARTING/UNKNOWNでも同じpathで再実行しない。サーバーから認証更新/承認等のrequestを受けたら回答せず停止。出力はaccount type、model ID、固定診断metadataだけ。通常CLIが既存state/cacheを作成/更新する可能性はあるが、設定/認証の追加変更やtokenの出力はしない。

account=chatgptとmodel一覧が取れても課金額・model利用権は未検証。初期候補modelはgpt-6.1-solで、実一覧との照合後に固定する。host側でも失敗したらそのreceiptで止め、別のbinary・state領域・権限で繰り返さない。

## 第2段階：条件を固定して一括承認

第1段階と残り3点のoffline検証が完了してから、次だけを一度に提示する。

- 既存専用daemon/tunnelの同一UIDへコード/引数を反映。workspaceはレビュー対象project 1件、model 1件、4thread・合計16task・同時1・論理120秒・自動retry0、入力/回答各4KB。filesystem sandbox=read-only、approval=on-request/user。workspaceキーは読取りjailの証明ではなく、既存native MCP/toolsも別途確認する。
- 既存11操作を保持し、文書化した12操作だけを同じMCP認証へ追加。同じcredentialを使うcallerが全対象依頼/回答を扱える範囲を承認する。承認待ちは通知のみ、承認回答APIは含めない。
- active/UNKNOWN jobがないことと旧healthを確認して、既存2サービスだけを一度再起動。DB/mailbox grant/キー参照/公開範囲を保全し、readiness・23 schema・poll freshnessを確認。失敗時は既存起動記録の11操作へ戻し、何度も再起動しない。login pin更新も対象に明示する。
- 非機密synthetic task 1件のread-only推論を別条件として含めるか明記。実施後は回答回収・停止/terminal・残存枠を確認。Chat wakeは含まず、逆方向は既存UIとmailboxの成功条件を分ける。

停止ではinterrupt受理とterminal確認を分け、UNKNOWNを自動再送しない。rollback前に既存turnが残っていれば回収/照合する。state/履歴を削除せず、サービス設定の旧hashと引数へ戻す。新credential・新port・host sandbox緩和は承認項目へ混ぜない。

## Offline確認

host probeはfake transportのみで、HOST不明/管理sandbox/readonly state/hash不一致/wrapperを起動前拒否し、4つの固定protocol message、認証更新なし、重複receipt拒否を検証する。実host probe・daemon gateway転送・23操作の現用起動・実推論は未検証。この手順書とunit fixtureのPASSは配備完了を意味しない。
