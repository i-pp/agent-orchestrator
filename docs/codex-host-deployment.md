# 正規host executorへの段階的配備案（未配置）

現Codex execのsandboxを変えず、ユーザー管理hostの既存agent-orchestrator daemonに実行責任を持たせる案。新しいorchestrator・ユーザー・資格情報・外部listenerは追加しない。ここで書いた配置/再起動/host probeは未実施。

## 調査結果と限界

導入npm launcherはnative binaryを選び、argvと既存環境を引き継いでspawnする。launcherソースにCodex stateのmkdir/write処理はない。package manifestは0.165.0、native Rust実装のsourceは導入物に見当たらず、binary内にPATH alias作成・private app-server socket directory作成・policy/plugin/cache作成のエラー文字列がある。文字列の存在だけで、特定の処理が今回実行されたことや起動必須であることは証明しない。

今回execのmetadata観測ではCODEX_HOME/SQLITE_HOMEは未指定。defaultの`~/.codex`・そのtmp/log/sessionsと`~/.cache`はread-only mount。書込み可否は通常のmode/ownerだけでなくmount制限により不可。authファイル本文は読んでいない。237 byteのstderrから得た固定分類はREAD_ONLY_FILESYSTEMのみで、既知の非致命的PATH alias警告と致命的失敗を分離できない。終了1・stdout 0は確認済みだが、fatalとなった具体的path/syscallは未特定。

[公式state説明](https://learn.chatgpt.com/docs/config-file/config-advanced#config-and-state-locations)と[環境変数説明](https://learn.chatgpt.com/docs/config-file/environment-variables)では、CODEX_HOMEはconfig/auth/log/session/cache等の既存state root、SQLITE_HOMEはSQLite stateの配置先。通常hostでは既存stateへの正規アクセスが必要になる。log/session/SQLite/socketの全てがmetadata probe起動時に必須であるとは断定しない。CODEX_HOMEを一時領域へ移しauthをコピーする方法は今回採用しない。

初期候補には実行ファイルの選択にも不足があった。既存serviceは`env -i`でPATHを`/usr/bin:/bin`へ固定するため、PATH上のnpm CLIをtunnel配下で起動する方式は適切ではない。改修後はdaemonへ既存の絶対binary参照を渡し、`codex-pipeline-executor.json`のhashと照合してnative ELFを起動する。tunnelの256MiB枠へexecutorを入れない。

既存service記録のbinaryはVS Code同梱0.164.0-alpha.11.1、初期schema確認はnpm 0.165.0だった。既存binaryでもgenerate-json-schemaを実行し、関連26schema中20がbyte一致、残りは未解釈CodexErrorInfoのvariant差分で、使用要求と応答必須fieldsは一致と確認。server起動はしていない。両確認版以外はversion gateで拒否する。host probeと配備では既存serviceのbinary参照を維持し、npm版へ黙って切り替えない。

## 配備先と境界

予定経路は次のとおり。

```
Chat -> 既存認証Tunnel -> MCP Adapter（12操作を検証）
  -> 既存Unix control socket（owner UID / 0600 / peer UID）
  -> 既存daemon内のpipeline gateway
  -> 固定native ELF app-server --listen stdio://
```

同じhost UID・既存Codex login/state・既存Tunnel key参照を使う。keyの値やauthを引数/環境変数へコピーしない。daemonは既存の512MiB/TasksMax枠、tunnelは256MiBのまま。必要なheadroomが未確認ならBLOCKEDにし、無断で増枠しない。新規port、外部WebSocket、systemd/host security変更、linger追加は不要。既存の外向きTunnel接続とCodex認証/metadata/推論の既存通信は残る。

この図の転送はfeature候補へ実装済み。`DaemonPipelineProxy`が既存UID socketへ固定`codex_pipeline` envelopeを送り、daemonが12tool名/argument schemaを再検証して`HostGateway`へ渡す。自由なapp-server RPC proxyではない。既存submitの意味は変更せず、default OFFで従来のscopeを保つ。実host socketによる疎通はまだ未検証。

実装した3点と検証範囲：

1. 運用者の既存native binary path/hashとCODEX_HOMEをhost preflightで確認。read-only/UNKNOWNは起動前拒否、別home/credentialへfallbackなし。新executor設定は0600 owner fileでhashのみ、pathは既存daemon参照を再利用する。
2. daemonがgateway/queue/tick/interruptを所有し、通常runnerと同じrunner.lockで排他。初期pipeline同時1、通常job稼働中は入力をqueueへ保持。paused/UNKNOWN/cleanup未確認時は新turnを開始しない。nativeは専用process groupで、PID/start-tokenを確認してreparentされた同group子も採録し、TERM/KILLと不在確認を行う。markerはfork前にfsyncし、crash/回収未確認はUNKNOWN保持。legacy runnerの両経路も未解放marker/ledgerを拒否する。OS全体の任意processや、観測前にgroup外へ完全離脱した子まで制御できると保証しない。実hostでのresource headroom/回収は未検証。
3. default OFF flagをdaemon/adapter双方へ伝播し、poc_servicesのplan/preflight/recovery/startとpipeline-only rollback、poc_loginの4hash pinと11/23操作照合を実装。OFFの旧login pin bytesは維持する。実unitの配置・pin書換え・再起動は行っていない。

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

host probeはfake transportのみで、HOST不明/管理sandbox/readonly state/hash不一致/wrapperを起動前拒否し、4つの固定protocol message、認証更新なし、重複receipt拒否を検証する。gateway/共有lease/UNKNOWN/paused/reparent cleanup/legacy admission/service flags/login pin/rollbackもofflineで確認。実host probe・実socket転送・23操作の現用起動・実推論は未検証。この手順書とunit fixtureのPASSは配備完了を意味しない。

既存test_runnerの実行系13件は、この管理execで元のversion確認用isolation guardに止まり期待stateと一致しない。変更前のrunner対照でも同じNESTED_ISOLATION_FORBIDDEN・fake executor起動0を確認した。テストをskip/弱体化したり、追加isolationを実行して通したりしていない。新gatewayのfake executor検証と、この旧runnerの環境依存失敗は分けて記録する。
