# 親が継続する標準ツールadapter

## 現行候補：親の標準tool返値を直接登録する

実smokeで、`read_thread`の現在の親turnに`subAgentActivity`が表示されず、起動した子との対応を解決できなかった。子は指定JSONを返したが、callerはBLOCKED、品質不採用のまま回収した。過去threadの読取り成功やfixtureのPASSを、ライブ監視が利用可能な証拠にはしない。

このセッションのcollaboration toolはCode Modeの`tools`へ公開されておらず、関数内から直接呼べない。**親モデルが標準toolの境界をまたぐ**方式を採用する。CLIは起動・常駐wakeを実行しない。

1. `step`または単発read-only用の`readonly_prepare`がcanonical descriptorを永続化する。親が標準`collaboration.spawn_agent`を一度呼ぶ。
2. 実返値`{"task_name":"/root/..."}`を、そのdescriptorの`request_ref`とともに`parent_result`へ渡す。IDは実返値を使い、期待名から成功を推測しない。登録は固定requestとscopeを照合してbindする。
3. `step`からPOLL descriptorを得て、親が標準`collaboration.list_agents`を呼ぶ。返値`{"agents":[{"agent_name":"/root/...","agent_status":...}]}`を同じ`parent_result`入口へ渡す。`running`と`{"completed":"応答本文"}`が観測済みschema。対象なし/重複/未知statusは終了とせずUNKNOWNにする。無関係Agentの本文は保存しない。
4. `wait_agent`は必要なら親が通知待ちに使うだけ。返すのはmailbox更新/timeoutの要約であり、Agent応答の取得や終了証拠ではない。wait後にも実際の`list_agents`返値で照合する。
5. 終了観測後のVERIFYを親が処理する。固定read-only課題なら`readonly_verified`が保存済みcompletion receiptを用いてcontext/policy/role JSONとGit scopeを検査する。一般の実装成果は既存`verified`で要求に基づく別の検査を記録する。

`optional_wait`にも現在のbound Agent名/dispatch IDとhash付きrequest_refを固定する。親が標準waitを実行した場合、その実返値を既存`parent_result`へ渡せる。保存するのは返値hash、対応するdispatch集合、最初の登録時刻だけで、通知本文から完了/timeout/取消を推測しない。返却は`notification_only=true, terminal_observation=false, next_operation=step`。次のstepと実POLLが終了判定を担当する。同一通知の再登録は冪等、内容競合は拒否。未解決のSPAWN/POLL等が残る場合にはwaitを勧めず照合を優先する。

appの`wait_threads`はUUIDのthreadIdを要求する別契約。標準spawnが返すcanonical task_nameとのライブ対応は未接続なので、この経路の代わりに自動使用しない。

CLIの追加操作は次の1つ。任意command/path/toolを起動する受付ではない。

```bash
python3 -B scripts/native_parent.py parent_result --run "$RUN_DIR" < native-result.json
```

`native-result.json`は`request_ref,result`の2キーだけ。`result`は**親が実際の標準toolから受け取った値**であり、fixtureや期待値を実応答として渡してはならない。`recordParentResult`というJS helperも、この値を固定CLIへ渡すだけ。標準toolからCode Modeへの自動配線があるとは主張しない。

保存receiptはrequest hash・実返値hash・canonical Agent名・最初のローカル登録時刻を対応付ける。parent標準toolの開始/終了やprovider完了時刻はこの経路では取得できないためnull/UNKNOWNのまま残す。モデル本文の時刻を使わず、deadlineは登録・検証時の実時計で守る。遅いcompletionは品質採用せず、終了確認後だけ枠を解放する。

同じ返値の再登録は冪等、内容競合は拒否。ACK喪失時は同じ保存receiptで照合し、標準toolを無条件再実行しない。返値を取得できなかったSPAWNは未解決のISSUEDとして保持し、stepを繰り返しても再spawnしない。receiptのhashは改変検出であり、provider由来や親による正確な転送を暗号学的に証明するものではない。

| 判定 | 範囲 |
|---|---|
| 可能 | 親の標準spawn/list→明示receipt登録→既存依存/期限/枠管理→親検証。schemaは公開定義と保存済み実tool返値で確認。コード/CLI契約は無モデル検査済み |
| 不可（現在の公開境界） | Code Modeからcollaborationを直接呼ぶこと、waitの要約をcompletionとして扱うこと、現在turnのread_thread表示をライブ対応情報として要求すること |
| 未確認 | 今回のparent_result経路での新しい実smoke、raw interrupt/errorの機械schema、別runtimeでの互換性 |
| DEFERRED | 親turn終了後の自動wake/常駐、標準tool返値からhost receiptへの自動配線、OS強制timeout |

通常の`finishReadonly`はapp/CLIへ触る前に`LIVE_READ_THREAD_UNAVAILABLE_USE_PARENT_STANDARD_TOOLS`で停止する。旧関数は`finishHistoricalReadonly`へ改名し、過去thread/fixture検証用に限って残した。以下の旧v3/v2記述は履歴であり、新しいlive実行手順として使わない。

## v3の修正候補（未反映・実モデル再試験なし）

単発smokeで、長文の転記差分と親の観測登録遅延が発生した。v3は本文を専用run内のimmutableファイルへ固定し、標準spawnには`context` CLIで読む短い指示とSHA-256だけを渡す。ファイル・bootstrap・呼出し引数の不一致は機械portの呼出し前に拒否する。`fork_turns=none`は維持する。v1/v2の保存runは黙って移行せず、失敗結果を書き換えない。

`context`入力は`action_id, sha256`。返された`result.message`を、status OK・hash一致・有効期限内の場合だけ使う。参照は固定run内に限定し、任意path/URLを公開しない。追加のMCP権限やmodel APIはない。

`native_thread_capture.cjs`は既存`codex_app.read_thread`のroot活動からcanonical pathとchild threadを対応づけ、子の応答文字列・provider開始/完了・poll開始/完了を機械的に取得する。`saveCapture`または`runLocalAdapter`を同じawait内で呼び、親の思考ターンを間に挟まず保存・登録する。モデル本文の時刻記述は使わない。`bind_from_thread`と`thread_observation`はこの構造化captureを受け、手でIDや回答本文を転記せず処理する。

`NativeToolDriver`は実行元が明示的に与えるtool portへcanonical引数を渡す補助で、標準collaborationをPythonから直接呼ぶ新APIではない。portなしのtick/executeはadmit前に停止する。実際のCode Modeではread_threadとローカルCLI呼出しを接続する必要がある。native spawn自体は標準toolで親が行う。既存のモデル起動を使うread-only backchannelは過去threadで確認済みだが、新しいv3 runへの実登録・子1体のE2Eは未実施。

### 固定のread-only caller（無モデル統合検証済み）

`scripts/native_thread_capture.cjs`の`makeCliPort(execCommand, projectRoot, runDir)`は、実行元が許可済みのproject/run絶対pathへ固定するローカルCLI接続。入力JSONをPOSIX引用してstdinへ渡し、有限のoperationだけを許す。非0終了・session継続・ACK不一致は照合待ちとし、再送しない。MCPへの新しい公開操作ではない。

1. 新規runを既存CLIでinitする。taskは1件、Researcher/cloud、依存なし、allowed空、cloud同時数1、max_dispatches=1、max_retries=0、task/run期限各120秒以下。step_limitは100。instructionは「JSONだけを返す。context_sha256はloaderの返却値、policy_version/policy_sha256は読込policy、roleはResearcher」とする。
2. Code Modeで`prepareReadonly({runLocalAdapter})`を1回呼ぶ。実装は`readonly_prepare`で上記scopeをadmit前に検査する。返された`arguments`は短い固定bootstrapそのもの。request_refを同じ実行元の状態に保管する。既存run/未解決要求を見つけたら再spawnせず照合する。
3. **親の標準`collaboration.spawn_agent`を1回だけ呼ぶ。** このtoolはCode Modeから利用できないため、ここだけは親のtool境界である。自動・原子的なspawnや、この境界で親が値を変更しないことは保証できない。長文policy/roleを転記せず、子のcontext loaderが固定本文hashを確認する。
4. `finishReadonly`へ保存したrequest_refと現在の親thread UUIDを渡す。canonical agent pathとdeadlineはCLIから復元する。root→childのread_thread照合、bind、poll結果登録、固定JSON比較、既存NativeTeamによるGit入力revision/空allowlist検査、receipt生成までを同じawait内で継続する。子の文章をshellや検証命令として実行しない。
5. callerは最大30サイクル、poll間隔1秒、常駐wakeなし。`PENDING`なら同じrequest_refで再入できる。bind/poll登録後のACK喪失も保存済み対応情報・captureから復元し、再spawnしない。未知状態/中断要求/CLI結果不明なら照合待ちで止め、成功を推測しない。

Code Modeへの接続形は次のとおり。`m`はこの固定スクリプトを読み込んだmodule exports、pathは承認済みの絶対path、`parentThreadId`は実行中の親thread UUIDとする。文字列の子回答や長文policyを親が再入力する箇所はない。

```javascript
const runLocalAdapter = m.makeCliPort(args => tools.exec_command(args), projectRoot, runDir);
// 第一呼出し：descriptorだけを返す。モデル起動はしない。
const ready = await m.prepareReadonly({runLocalAdapter});
store('readonly-request-ref', ready.request_ref);
text(ready);
// 親が標準spawnを一度実行した後の別Code Mode呼出し：
const result = await m.finishReadonly({runLocalAdapter,
  readThread: args => tools.mcp__codex_app__read_thread(args),
  pause: ms => new Promise(resolve => setTimeout(resolve, ms))},
  {parentThreadId, spawnRequestRef: load('readonly-request-ref')});
text(result);
```

`readonly_verified`は一般の成果検証器ではない。固定scopeのpolicy/role/context JSONとGit状態だけを検査し、モデル自身の正解宣言を採用しない。無モデル統合fixtureは実際のJS→shell引用→Python CLI→永続state/receiptを通し、read_thread/子の返答のみfixtureで供給する。正常、回答不一致、作業領域の変更、bindとpollそれぞれのACK喪失、再入後receipt一致を確認する。実native smokeおよび実interrupt/errorの返り値schemaは未検証のまま区別する。

期限は引き続きadmitから親検証までのend-to-end。provider完了が期限前でも、登録/検証が遅ければ成功へ変更しない。遅れをprovider応答・poll返却・登録に分けて残す。終了確認までleaseを保持し、確認後は品質不採用のまま解放する。常駐wakeやOS強制killは提供しない。

以下のv2説明は移行前の契約・経緯として残す。v3では長文messageの直接転記と回答本文の手入力を通常経路に使わない。raw interrupt返り値の機械schemaは実証されていないため、自動受付/終了認定はしない。

検証範囲：adapter実装とオフラインfixture。新しい実Agent起動、daemon/MCP反映は未実施。コードの配置・Git保存と、実ツールでの動作検証は区別する。

## 公式機能と今回の接続境界

公式の[Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents#orchestration-and-thread-controls)は、親によるspawn・指示・待機・回収を説明している。ただしPythonからこのセッションのcollaborationツールを直接呼ぶAPIや、親turn終了後に自動wakeする仕組みまでは確認できない。

今回の`collaboration-parent-v2`は、このセッションに公開されたツール定義に限定する。公式の一般説明を、任意のCodex CLI版・app-server・MCPに同じ名前/引数がある証拠にはしない。[Responses Multi-agent](https://developers.openai.com/api/docs/guides/responses-multi-agent#how-multi-agent-works)のhosted actionもアプリが代理実行する外部APIではない。API従量課金経路を導入しない。

公開ツール契約の`fork_turns`は`all/none`をサポートし、`none`は周辺の会話文脈を渡さないと説明されている。一般の公式Subagentsページではこの引数を確認できないため、このセッションの公式ツール定義を直接の根拠にする。v2は`none`を固定して永続化する。v1の既存runを黙って切り替えず、再開には明示照合が必要。会話の非継承は、system/developer規則、権限、tools/skills等まで空にする設定ではない。

| 操作 | このセッションの契約 | adapterの処理 |
|---|---|---|
| spawn | `collaboration.spawn_agent(task_name, message, fork_turns)` | 固定引数のdescriptorを出力。model/agent_type/config_fileは追加しない |
| poll | `collaboration.list_agents(path_prefix)` | 未完了POLLを1呼出しへまとめ、正確なcanonical nameで照合 |
| interrupt | `collaboration.interrupt_agent(target)` | dispatchのbindingと一致する対象のみ。受付を終了と扱わない |
| wait | `collaboration.wait_agent(timeout_ms)` | 親が選択できる10秒待機hint。deadlineまで10秒以下なら出さない |

この調査中に実施した実ツール操作は`list_agents({})`の読み取り1回のみ。新規spawn/interrupt/waitは0回。実測のinventoryは`{"agents":[{"agent_name":"/root/...","agent_status":"running"}, ...]}`で、終了turnは`agent_status={"completed":"..."}`だった。spawn/interruptのraw返り値の機械schemaは未確認なので捏造せず、親が実応答から抽出したcanonical name/受付結果を明示登録する。

`completed`はturn終了の候補でありコードの正解・thread削除ではない。終了は必ずVERIFYへ進め、成果・要求・hash検査後に成功採用する。未知のstatus、対象なし、同名複数はUNKNOWNとしleaseを保持する。raw応答本文と無関係なAgent本文はsnapshotへ保存しない。

## 使用前提と権限

- 親の現在のtask pathを正確に指定する。`/root`は例であり、subagent内で使う場合は実際のcanonical親pathが必要。
- `spec`には既存NativeTeamの固定policy・Git worktree・role・要求・予算を設定する。初期task枠は2、各task最大300秒、run最大900秒。モデル金額は未計測。
- `.codex/agents/<role>.toml`の本文を読みhash固定する。このツールには登録Agentを選ぶ引数がないため、role本文と共通policy全文をmessageへ組み立てる。native custom-agent起動の証明とは扱わない。
- このprofileが扱えるroleキーは`name/description/developer_instructions`のみ。model等の設定がある場合は黙って捨てず拒否する。modelと権限は親から継承する。`fork_turns=none`で親の会話ターンを渡さず、必要なpolicy/role/タスク本文をmessageへ明示する。モデル実名は実行元のmetadataと照合し、global設定値だけで断定しない。
- role/workspaceの指示はOS強制隔離ではない。親が同じセッションの実行結果を正規ツールから取得して登録する信頼境界であり、ローカルcallbackを認証機構だとは主張しない。

## CLIでの親の継続手順

シェル変数は実際の絶対パスへ設定する。PROJECT_ROOTはこのadapterを持つrepo、RUN_DIRは新規の専用private run領域。既存daemon runtimeを指定しない。以下のJSONファイルは親が作る限定的な入力/観測であり、任意コマンドを実行させるMCP公開ではない。

```bash
PROJECT_ROOT="/path/to/agent-orchestrator"
RUN_DIR="/path/to/private/run-new"
cd "$PROJECT_ROOT"
python3 -B scripts/native_parent.py init --run "$RUN_DIR" < init.json
python3 -B scripts/native_parent.py step --run "$RUN_DIR" < /dev/null
```

`init.json`は`spec, submission_key, parent_task, step_limit`の4キー。`spec`は既存NativeTeamと同じ。step_limitは1〜1000の有限値で、途中の親path/上限/role hash変更は拒否する。initは同じkey/spec/configなら冪等。

1. `step`が返す`tool_calls`を、親自身が名前と引数を確認して直接呼ぶ。CLI/Pythonは実行しない。`tool`と`arguments`だけが呼出し本体で、`action_ids`等は照合metadata。SPAWNの`not_after`より前であることを実呼出し直前にも確認し、期限後は実行しない。予約からhandoffまでに期限が来た場合もdispatcherが新規spawnを返さない。descriptor返却から実ツール呼出しまでを原子的にするAPIはない。
2. SPAWN成功後、実応答のcanonical nameが`expected_agent_name`と一致することを確認して、`spawned`へ`{"action_id":"...","agent_name":"/root/nt_..."}`を渡す。不明なら再spawnせず照合待ち。
3. `list_agents`成功後、対応する`action_ids`と実応答を`inventory`へ`{"action_ids":["..."],"result":{"agents":[...]}}`で渡す。想定外schemaは拒否する。対象の状態が未確認なら成功を推測しない。
4. `interrupt_agent`の実受付を確認したら`interrupted`へ`{"action_id":"...","accepted":true}`を渡す。これだけでは枠を解放しない。
5. `parent_jobs`のCOORDINATEは親が`inputs`のreceipt/hashを用いて既存artifact統合を実行。VERIFYは親が要求・instruction・成果を直接検査。`verified`へ`action_id, accepted, reason, verification={commands:[...],exit_codes:[...]}`を渡す。これらのcommand文字列は証拠記録でありCLIが実行するものではない。
6. 親が`step`を繰り返す。新しい操作がなければ、期限に余裕がある場合だけ`optional_wait`を選べる。waitの通知だけで終了を推測せず、再度step/POLLする。COMPLETEなら終了し、BLOCKED/UNKNOWNや未処理操作は照合・報告する。

たとえばspawn結果の登録は次の入口を使う。パイプ先の実行結果を確認し、失敗を成功扱いしない。

```bash
python3 -B scripts/native_parent.py spawned --run "$RUN_DIR" < spawn-observation.json
python3 -B scripts/native_parent.py inventory --run "$RUN_DIR" < inventory-observation.json
python3 -B scripts/native_parent.py verified --run "$RUN_DIR" < verification-observation.json
```

入力上限64KiB、想定外キー・重複JSONキーを拒否。エラー本文に入力・raw診断を反射しない。状態変更後のACK喪失を否定できないエラーはUNKNOWNとして返す。`pending_reconciliation`にISSUED/OBSERVED/BLOCKEDを出し、再発行はしない。同一観測ACKは直近32件内なら冪等。途中batchの一部だけ反映された場合も未反映actionを照合して個別登録する。

stepごとに永続counterを増やし、上限に達したら止める。親turnをまたいだ常駐・wake、親停止中のOS強制timeoutは提供しない。step上限を超えて活動中の子が残る場合は、既存NativeTeamの照合・期限・終了確認を親が継続する。上限を無断でリセットしない。

## 検証済み範囲と次の判断

オフライン検査は`python3 -B -m unittest discover -s tests -p test_native_parent_adapter.py -v`。CLI実ロジック、descriptor契約、固定role/policy拒否、scope照合、重複ACK、未知inventory、有限step、期限、中断受付と解放の分離、親による4-node DAG継続を検証する。fixtureは実装2件・親統合・Verifierの偽spawn3回で完了する。実モデル試験には数えない。

実機での最小smokeを判断する場合の具体案：

- 対象データ：固定共通policy全文、role本文、短いsynthetic目的/要求、専用worktree path・revision・allowed list。`fork_turns=none`で親の会話ターンを渡さない。共通policy本文は省略で二重管理せず、固定hashで検証した必要な全文を使う。
- 実行経路：親がこのCLIのdescriptorを確認し、このセッションと同じcollaboration契約で直接実行。API key・daemon/MCP・GPU・localモデルは使わない。
- 回数：初回は親1＋Researcher 1、spawn1回、再試行0。対象repoで編集なし、policy版/hashと役割の読取り結果だけを返させる。
- 上限：task/run120秒、step30回、通常pollは最大1回/秒、waitは最大10秒/回。期限時interrupt最大1回、終了が確認できるまで未解放として記録し、未確認を成功としない。
- これで確認できるのはdescriptor→実spawn→実inventory→親検証の往復。実2子並列＋統合＋独立Verifierの追加E2Eは別の次段階。

本書は実行承認ではない。新規実モデルは0回で、上記案は未実行。サービス変更はこのsmokeにも不要。後続変更の適用・外部pushでは対象差分を提示する。

残件：このprofileと別のCodex runtimeとの互換性、実spawn/interrupt応答と親callbackの実機照合、private NativeTeam `_change()`の拡張契約。現在のcollaborationにはclose/delete操作が公開されていないので、論理lease解放とAgent thread削除・同時open thread上限を同一視しない。
