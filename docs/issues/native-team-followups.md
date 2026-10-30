# Native team PoCとMCPの残課題

この一覧はオーケストレーション項目5とMCP項目4を分けて管理する。自動投稿suspend、cloud setup Close、別threadのLLM/GPU試験を再開・変更しない。共通policy本文は固定正本を参照し、ここへ複製しない。

| ID / 状態 | 問題 | 影響 | 暫定対応 | 再検討条件 |
|---|---|---|---|---|
| NATIVE-01 / ACCEPTED | native spawn/wait/interruptは親の標準toolsで呼ぶ。local CLIは自動呼出しを行わない | unattended dispatcherではない | admitを永続保存→spawn→実IDをbind→terminal照合をCoordinatorが実行。今回この接続を実測 | 正式なtool dispatch API/権限と、無人実行の必要性が具体化したとき |
| NATIVE-02 / DEFERRED | 現FIFO daemonのDBとnative runは別 | 既存MCP statusでnative taskを一元監視できない | native-team.jsonとCLI showを正として保存。daemonを再起動しない | native backendを既存daemonへ追加する明示scopeが決まったとき |
| NATIVE-03 / DEFERRED | adoptionは新規allowlist成果物のみ | 任意の既存コードpatch/merge競合を自動解消できない | 同じworktreeへの並列編集を避け、親がhash検査後に採用。既存file衝突は拒否 | 既存ファイル変更を必要とする次の実taskが決まったとき |
| BUDGET-01 / ACCEPTED | 期限監視/interruptはCoordinator、host全体のOS hard kill/quotaではない | 親停止時の実worker終了を保証しない | timeout/UNKNOWNでもnative終了確認までslot保持。reloadで再spawnしない | 確認可能なnative cancel/terminal APIが用意されたとき |
| BUDGET-02 / DEFERRED | 金額の計測API/契約額を未確認 | 厳密な費用上限は強制できない | 継承subscription経路、同時2/dispatch6/時間/回数を制限し金額unmeasured。API従量課金なし | 計測可能な請求/usage APIと予算が承認されたとき |
| LOCAL-01 / DEFERRED | local backendの接続を今回利用しない | cloud/local混在schedulerの実動は未検証 | registry localはdisabled、GPU/llama.cppを触らない | 別LLM試験と調整し、共有API/モデル/並列1を検証するとき |
| REVIEW-01 / ACCEPTED | worktree/role指示はOS security boundaryではない | 同UID任意プロセスを隔離する保証なし | 入力revisionとallowed差分/hashを検査し、Verifierを別worktree・別agentへ分離 | 別OS権限のレビュー隔離が必要になったとき。追加namespaceは禁止を継続 |
| MCP-01 / DEFERRED | 親Dotの直接Chat自動起動/応答イベント回収は別経路 | 完全自律のChat相談を開始できない | 既存11tool/mailbox/有限grantとログイン自動復帰を保全。native PoCは直接Chatを使わない | 正式な起動/応答APIと権限・対象を確認したとき |
| MCP-02 / ACCEPTED | grant期限/保持はlogical、物理削除なし | 期限後もprivate DB bytesが残る | 既存期限・総数・取消/rollbackを変更せず運用 | 物理削除が要求され、backup/復旧と併せ承認されたとき |

UNKNOWNの再送、provider/credential追加、hostsecurity緩和、service再起動、外部push/mergeはこの一覧から自動実行しない。非ブロッカーを記録することは解消済みの宣言ではない。
