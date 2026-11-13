# Native team: DAG開始判定と永続記録

> Historical Japanese source, preserved from the pre-v4 implementation. This is not a current translation. Use the [English guide](native-team-poc.md) for current behavior.

`src/native_team.py`は既存の検証済みPlanとartifact adoptionを接続する、小さいlocal coordinator APIである。新しいモデルlauncher、常駐Scheduler、既存FIFO daemonの置換ではない。CLI `scripts/native_team.py`はJSONを読み状態を更新するだけで、モデル/API/サービス/コマンド文字列を実行しない。

## 構成と責任

| 部品 | 実装する範囲 |
| --- | --- |
| DAG / Scheduler admission | 依存成功後の開始、cycle拒否、開始前の期限・回数・同時枠確認 |
| Registry | role/native_role、cloud/localと有効化状態。localは無効、providerは継承 |
| Budget | cloud最大2/local最大1、dispatch最大6、retry最大2、各300秒/全体900秒以下 |
| Artifact | allowed pathとhashの固定、親だけが新規成果物を採用、Gitの範囲外差分拒否 |
| Escalation | WAITING_FOR_HUMANによる依存停止。判断/承認の自動取得はしない |
| 永続記録 | private run directory、排他、fsync/atomic replace、指示・actor/native ID・revision/hash・検証・理由 |

同時枠はこのrun内の予約で、ホスト全体のhard quotaではない。期限到達は標準interruptを呼ぶための判断でありOS watchdogではない。クレジット金額はunmeasured。roleのwrite指示/worktreeは運用分離で、権限を狭めるsandboxや本人認証ではない。

## 準備と開始前の固定

共通ポリシーは既存Skill/AGENTSの手順で本文を実受領し、version/hashをspecへ固定する。自動pull/reset/新credentialやtrust変更はしない。入力repoの既存差分を保全し、専用のcleanなGit worktreeを実装者ごとに用意する。各workspaceは同じgit common directoryに属し、重複/nestingを避ける。検証者も別worktreeを使う。

```bash
git worktree add -b team-task-a "$WORKTREE_A" "$INPUT_REVISION"
git worktree add -b team-task-b "$WORKTREE_B" "$INPUT_REVISION"
```

shell変数は利用者が対象の許可済みpath/revisionへ設定する。上のコマンドを既存dirty checkoutの代わりにresetする用途へ使わない。project固有のignored policy-source設定は、既存の正規参照を各worktreeへ別途用意する。秘密・実環境設定をGitへ入れない。

specはschema=1、goal、requirementsのlist、projectの絶対path、policy={version,sha256}、registry、tasksを持つ。registryは`native_team.registry()`のroles/backends/budgetsを出発点に、予算を小さくできる。tasksはid/role/backend/deps/workspace/instruction/allowedを持つ。runtime/ログはrepoの追跡対象へ置かず、専用run directoryを使う。既存runへのinitは拒否し、続きを新しいIDで再投入しない。

```bash
python3 -B scripts/native_team.py init --run "$RUN_DIR" < team-spec.json
python3 -B scripts/native_team.py show --run "$RUN_DIR"
```

## 実行前制御と結果対応

1. coordinatorがready taskを選ぶ。依存や枠の最終判定は`admit`が行う。
2. `admit`がRUNNING/ticketを永続保存した**後でだけ**、標準native spawnを1回呼ぶ。
3. 返った本物のagent/thread IDを`bind`する。spawnのACK不明なら再送せずUNKNOWNとして既存tool履歴を照合する。
4. 標準waitからnative terminalを確認し、scope/diff・検証根拠・hashを親が確認して`complete`する。

```bash
python3 -B scripts/native_team.py admit --run "$RUN_DIR" < admission.json
# ここで標準native spawnを呼ぶ。CLIはspawnしない。
python3 -B scripts/native_team.py bind --run "$RUN_DIR" < native-binding.json
# ここで標準waitのterminalを確認する。
python3 -B scripts/native_team.py complete --run "$RUN_DIR" < completion.json
```

JSONの正確なfield集合:

| 操作 | stdin object |
| --- | --- |
| show / expire | `{}`（空stdinも可） |
| admit | task_id, actor |
| bind | task_id, dispatch_id, agent_id |
| complete | task_id, dispatch_id, agent_id, success(bool), reason, verification |
| unknown | task_id, dispatch_id, reason |
| release | task_id, dispatch_id, agent_id, reason |
| retry | task_id, cause_code（安定した大文字code） |
| escalate | task_id, reason |

verificationは`commands`の文字列listと、対応する`exit_codes`の整数list。成功には全exit code=0が必要。文字列は実行されず、観測済みのコマンド結果を記録するためのもの。CLIは未知field/不正JSON/重複key/64KiB超を拒否し、未承認の任意引数で呼べる関数を増やさない。エラーは本文や外部例外を返さない固定code。変更操作の例外は保存済みの可能性があるためUNKNOWNで止まり、show/native tool履歴から照合する。CLI自身はretryしない。

## 統合と独立検証

両実装がSUCCEEDEDになってから親のintegration taskをadmitする。親だけが`native_team_artifacts.integrate`へ各worktreeのroot/allowed/expected hashを渡す。全入力を先に検査して新規ファイルだけを採用し、重複出力・既存path・symlink・hash変化を拒否する。既存コードの競合mergeを自動解決した機能とは扱わない。

親が統合したrevisionとhashを記録し、検証者用worktreeを作る。Verifierは元のrequirementsと固定入力から別の検査を行い、実装者テストの成功だけで判断しない。検証用出力以外のsource変更は拒否し、検証コマンド・exit・hash・判定理由をreceiptへ保存する。結果やworktreesを消す前に成果と証拠を回収する。dirty/UNKNOWNのworktreeは自動削除しない。

## 期限・不明・取消の扱い

```bash
python3 -B scripts/native_team.py expire --run "$RUN_DIR"
# 期限切れIDに対応する標準interruptを呼び、実際の終了を待つ。
python3 -B scripts/native_team.py release --run "$RUN_DIR" < confirmed-native-release.json
```

expire後もnativeの終了ACKまでlease/同時枠を保持する。UNKNOWNも同じで、reloadだけでは解放・再spawnしない。releaseは本物のIDとterminal観測を照合して行い、UNKNOWN/TIMED_OUTを成功に変えない。retryは既知FAILED・解放確認済みの場合のみ、同cause codeの反復と上限超過を拒否する。原因の意味を自動分類する機能ではない。今回の実用PoCはretry0で行う。

仕様/architecture変更、重大なテスト不一致、未承認の外部送信はescalateで該当依存を止める。承認を返す機能やChatへの送信はこのCLIにない。

## 接続していないもの

既存FIFO daemonのDB、MCPの直接Chat起動、hosted submit/cancel、GPU/local llama.cpp、API課金、サービス再起動は別の残課題。ここを完成させるために現用を止めたり、安全設定を弱めたりしない。標準native toolsの実呼出し、actorとIDの対応、重なり、統合、独立検証の証拠が揃って初めて実用PoC成功とし、単にCLI/単体テストが通ったことと区別する。
