# Tunnel poll時刻の鮮度を含むreadiness

検証範囲：コードとオフラインfixture。コード配置、Git保存、実サービス検証は別に記録する。この修正のためのサービス操作は行わない。

## 修正する問題

2026-10-30の切断では、外部MCPが`tunnel_client_not_seen`を返す一方、ローカルhealthは`result=ok`、各okフラグもtrueを返した。`control_plane_poll.value`は13:29:02 UTCの成功時刻のままで、後続ログはtransport timeoutだった。

`tunnel-client v0.0.18`のこのhealth返却で観測したvalueはUnix秒。従来helperはokフラグだけを確認していたため、古い成功値が残っている間もREADYになった。これはreadiness判定の問題で、transport timeoutの根本原因を特定したものではない。

## 変更後の条件

既存のprivate URL file・loopback・キーを渡さない固定healthコマンド・timeoutと、すべてのokフラグ検査を維持する。その上で、次の条件を追加する。

- `control_plane_poll.value`と確認時のローカルwall clockが、正の有限な数値であること。bool・文字列・欠落値・非有限値・overflowは拒否する。
- 最終成功時刻が未来でなく、確認時点から**90秒以内（境界を含む）**であること。
- malformedなJSON object構造は正常と推測しない。解釈不能なJSON/health timeoutは既存呼出し側でNOT_READYとして扱う。

90秒は今回観測したpoll deadline 35秒＋最大backoff 10秒の約2周期分。外部側の300秒失踪エラーより早く劣化を示す初期値であり、SLAやtransport正常性の保証ではない。時刻同期の逆行時も保守的にNOT_READYになる。client更新でmetricの型・単位が変わる場合はschemaを再検証する。値なしをokフラグへfallbackしない。

## 影響と非影響

- `poc_services status`は古いpollをNOT_READYにする。保存済み所有世代やphaseは書き換えない。
- login復帰処理は、稼働済みだが不健全なunitをALREADY_READYとは扱わなくなる。既存unitの再起動はせず照合待ちにする。
- 将来のstart/ExecStartPost待機は鮮度を要求する。既存の起動deadline・失敗回収の規則はそのまま。
- この変更自体は自動再起動、常駐監視、新MCP操作、再認証、権限拡大を追加しない。scheduler metadataの`orchestrator_status`はこのhelperを呼ばないため、その応答schemaは変わらない。
- helperは新しいCLI/ログイン処理でimportされる。適用のためだけに稼働中daemon/tunnelを再起動する必要はない。native tunnel-client自身のhealth返却は変更しない。

## 試験

追加`tests/test_poll_freshness.py`の16件で、今回のstale値、90秒境界、未来/不正/欠落値、既存status/login経路、CLIのbounded failure、キーを渡さない固定呼出しを確認する。変更前helperで今回のincident assertionが1件失敗することも対照確認した。

関連のlogin20件・grant14件・Readiness4件を含め、**54検査PASS**。全てオフラインfixtureで、現用service、実モデル、GPU/NPU、namespace操作は行っていない。systemdを実際に操作するfixture classは対象外であり、実service統合成功とは主張しない。

Gitからの新規checkoutではignoredのローカルTunnel設定がないため、既存preflight検査にfixture準備が必要だった。テスト用worktreeだけで公開exampleをコピーし、値を有効な実環境設定へ置換せず使用した。検査をskip・弱化せず、次の2件はGit除外のままとする。

```bash
cp systemd/aceis-mcp-tunnel.example.yaml systemd/aceis-mcp-tunnel.yaml
cp systemd/aceis-mcp-tunnel.example.service systemd/aceis-mcp-tunnel.service
```

このコピーはmock検査用で、実tunnel-clientへ渡さない。現用設定をコピーしたり上書きしたりしない。

## 適用・戻し方

候補は`service_wait.py`・既存readinessテスト1箇所・新規テスト・本書の4ファイル。現用へ適用する前に最新hashと差分を再確認する。サービス/unit/key/runtime設定/DBは変更しない。適用後は新しいCLIで鮮度を含むreadinessを確認し、再起動はしない。外部MCP疎通の成否も別に確認する。

rollbackは今回4ファイルの差分だけを戻す。対象に後続変更があれば機械的に上書きせず再統合する。戻すと古いpoll成功をREADYとする従来の制限も戻る。

transport障害の再発原因調査、外部疎通監視、dispatcher/adapterの実ツールsmokeは独立の残件。今回のreadiness修正で解消済みとは扱わない。
