# Native親アクションdispatcher：実装準備

検証範囲：純粋ロジックとオフラインfixture。MCP追加・nativeモデル呼出しは行っていない。先行する8ファイルとは別差分で、コード配置と実ツールでの動作検証は区別する。

## 既存実装との分担

| 経路 | 実装と責任 | 今回の扱い |
|---|---|---|
| daemon/scheduler/runner | SQLite予約、tokenと親子PIDの照合、worker起動、cgroup解放照合、app-server実行と回収 | 変更・接続しない。別の実行経路を再実装しない |
| NativeTeam / CLI | DAG ready、admit、worktree/role/policy検査、予算・lease、bind、receipt、artifact hash | そのまま再利用。CLIは親の標準spawnを呼ばない |
| NativeDispatcher | 受領の冪等性、実行待ち操作の永続化、親からの観測照合、依存解放までの1ステップ進行 | 今回の新規ロジック |
| 親の標準ツール接続 | spawn、状態取得、interrupt、独立検証、統合 | fixture代替のみ。実ツールへの接続と継続呼出し担当は残件 |

新しいlauncher、OS隔離、socket、DB、認証、providerは追加しない。Gitの固定メタデータ検査とartifact処理は既存NativeTeamへ委譲する。taskのinstruction、検証commandは記録であり、このdispatcherは実行しない。

## 呼出し契約

```python
from native_dispatcher import NativeDispatcher

# spec/rootは、既存NativeTeamと同じ検証済みの作業定義・専用run領域。
dispatcher = NativeDispatcher.receive(root, spec, submission_key="request-v1")
progress = dispatcher.advance()
actions = dispatcher.take()  # 戻す前にISSUEDをatomic保存
# 親が各actionを適切な標準ツール・検証手順へ対応させる。
# dispatcher.observe(action["action_id"], observation)
```

`advance()`は常駐loopではない。親が期限付きで繰返し呼ぶ必要がある。戻り状態は`COMPLETE / READY / AWAITING_OBSERVATION / BLOCKED`。常に`parent_executor_required=true`を返す。親が停止している間の強制timeoutや確実なinterruptを保証しない。

| action | 親側で必要な処理 | observationの必須フィールド |
|---|---|---|
| SPAWN | 指定role・専用worktreeで標準spawn | agent_id |
| COORDINATE | 親自身が既存artifact統合を実行し検証 | agent_id, accepted, reason, verification |
| POLL | bindingと一致するnative IDの終了状態を取得 | agent_id, state |
| INTERRUPT | 同じnative IDへ中断要求 | agent_id, accepted |
| VERIFY | 親が要求・成果を検査しreceiptを確定 | agent_id, accepted, reason, verification |

stateは`RUNNING/SUCCEEDED/FAILED/CANCELLED/UNKNOWN`。verificationは`commands`と`exit_codes`の同じ長さの配列。成功採用にはexit 0が必要。想定外のフィールドとnative ID不一致は記録前に拒否する。IDはローカル対応づけであり、呼出し元identityやproviderの冪等性を証明しない。秘匿情報をinstruction、reason、commandsへ含めない。

native終了だけで依存を解放しない。VERIFYを経て既存NativeTeamが許可範囲・revision・artifact hashを確認し、成功receiptを保存してから次をadmitする。Verifier roleの独立性・検証内容の妥当性は親の割当てと検証手順に依存する。任意の`accepted=true`が独立検証の証明になるわけではない。

## 再開・不確定結果

- 同じrun/key/specの受領は冪等。異なるkey/specは競合。plain NativeTeamのrunを黙って引き継がない。
- admit後かつaction保存前に停止した場合はUNKNOWNにしてleaseを保持し、再spawnしない。NativeTeam作成とjournal初期化の間の停止も照合待ちにする。
- `take()`後、親のspawn実行前後で停止するとISSUEDが残る。標準spawnのexactly-onceは保証できない。snapshot内のactionを親が照合し、既知の実IDをobserveする。単純な再take・再起動では再spawnしない。
- 観測はOBSERVEDとして先に保存する。bind/complete直後のACK喪失は、保存済みbinding/receiptを照合しローカル処理だけ再開する。異なる観測への差替えは拒否する。
- POLL/INTERRUPTのACKが不明な場合も自動再発行しない。親による照合が必要。通常のPOLL間隔は1秒以上。
- UNKNOWNでは再spawn・即時中断をしない。既存期限到達時のみ中断を要求する。TIMED_OUT/UNKNOWNは終了観測後も品質成功へ変更しない。
- 中断受付は終了証拠ではない。終端状態のPOLL確認までleaseを保持する。実ID不明時は中断対象を推測しない。
- admitからtakeの間に期限が来たSPAWN/COORDINATEはBLOCKEDとし、返却しない。既存leaseを解放済みとは推測せず、照合対象として残す。
- 同一actionの同一観測を保持期間内なら重複ACKとして受け入れる。retired actionと軽量eventは各直近32件。古い/打切りactionへの遅延ACKは保守的に拒否し、再実行しない。これは完全な長期監査基盤ではない。
- 自動retryは追加しない。既存NativeTeamのretry/escalation上限と人の照合手順を使う。今後retry接続を行うなら、旧lease解放と新dispatch IDの対応を追加検証する。

## 永続化と制限

NativeTeamのprivate `_change()`を利用し、同じatomic snapshotの`dispatcher`名前空間（schema 1）へ記録する。別stateファイルへ真実を分散しない。専用0700ディレクトリの既存runtime lockで親アクション操作を直列化する。NativeTeam本体のlockとは分離する。

先行8ファイルを変えず準備するためのprivate API依存である。サービスへ組み込む前に明示的な拡張API・schema互換性・保存上限の契約を決める必要がある。既存snapshotの1MiB上限を継承し、超過時はエラーで止める。大きな出力本文は保存せず既存artifactを使う。

このモジュール自体は標準ツールを呼ばず、daemon/MCPへも接続しない。追加native E2Eは未実施。コードを配置しても、fixture成功を全工程の無人化完成とは扱わない。

## 検証

```bash
python3 -B -m unittest discover -s tests -p test_native_dispatcher.py -v
python3 -B -m unittest discover -s tests -p test_native_team.py -v
```

追加17件と既存19件を別集計する。fixtureの4-node DAGは独立実装a/b、親の統合、Verifierの順に進み、偽のspawnは計3回。実際のモデル呼出し・GPU・service・network・namespace操作は0回。このfixtureの時間や重なりは、既存の実Agent並列PoCの測定値ではない。

親用のdescriptor・観測登録CLIの準備は[標準ツールadapter](native-parent-adapter.md)を参照。Pythonから実ツールを直接実行する機能や常駐wakeは追加していない。
