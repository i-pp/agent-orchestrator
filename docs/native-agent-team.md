# Nativeチームの実行経路と境界

## 2026-10-30の再開作業

既存の検証済みPlan/artifact部品をsrc/native_team_plan.py / native_team_artifacts.pyへ採用し、src/native_team.pyで開始前の永続admission、実native ID、revision/hash、検証receipt、UNKNOWN/timeout時の未解放slotを接続する。標準toolsの呼出しは親Coordinatorが担当する。独立2 worktree→親統合→独立Verifier→永続記録のPoCと、既存FIFO daemonへの本番接続は区別する。

新しい操作手順はdocs/native-team-poc.md、残課題はdocs/issues/native-team-followups.md。以下は旧段階の接続境界を含む履歴であり、未実装との記述は新手順と実行証拠に照合する。native roleは運用上の役割で、独立sandbox権限の保証ではない。global config、service、GPU/local LLM、MCP公開範囲は変更しない。

このファイル群は既存ランナーの置換ではない。既存の専用Codex home、multi_agent=false、隔離fixture、SQLite、MCPの読み取り3ツール、Tunnelの2 user serviceを変更しない。

## 動く接続点

Dotから既存のCodex作業へ目的・制約を委譲し、Codex親が標準spawn/wait/interruptで子を扱う。この委譲と完了通知は今回の独立作業で実際に使用した。Dot側が使う委譲手段とローカルCLI/MCPは別の層であり、新しいAPI/provider設定は不要。

進捗や判断待ちはCodex親が同じ作業の返答で集約する。会話の要約/状態取得機能が使える場合はそれを読み取る。利用できない場合は完了通知・親からの報告を待ち、架空の常時監視を実装したことにしない。既存タスクを閉じたり強制的に書き込み接続を奪ったりしない。

Dotから渡す最低限の契約:

1. 目的、元の要求、完了条件、入力revision/hash。
2. repoと許可範囲、既存変更の保全対象、各実装子に専用worktreeを用意すること。
3. DAGの依存、同時子2、各子300秒/全体900秒、retry最大2。同原因・不確実な副作用の再送禁止。
4. Verifierは要求から独立検証。仕様/architecture変更・重大な不一致は人間承認待ち。
5. 結果は状態、actor、成果hash/差分、検証、判断理由、未検証、回収結果。

## MCPの境界

既存のorchestrator_status/list_tasks/get_taskが参照するのはdaemonのDBであり、native子は自動登録されない。この経路でnativeチームが監視・投入できたとは報告しない。自由文結果やソースも既存MCPから出ない。submit/cancelの公開や--allow-writes追加は今回行わない。

## 既存ランナーとの非互換

通常モードのsnapshot/read-onlyレビュー機能を、稼働中のdevelopment-pocモードで使えると推定しない。現development-pocは名前付きfixtureのimplementer専用で、resume/reviewerを拒否する。run-pairのCLIはdevelopment-pocで実行できない。これらを通すために規約・sandboxを弱めない。nativeチームへの設定追加だけではGit worktree入力を既存runnerへ追加できない。

## 適用範囲と戻し方

追加するのはrepo直下AGENTS、project用config、4役割TOML、本書のみ。すべて新規ファイルであることを適用直前に確認する。global config・global AGENTS・MCP設定・serviceを変更しない。Codexが対象projectをtrustedと認識しない場合は黙ってtrustを変更せず、既存の操作方針を確認する。

設定の新規読込みは対象repoをrootとする新しいCodex作業から確認する。既存daemonは専用の固定configを使うため、この追加で子機能が有効になるわけではない。開いている既存作業へ無理に反映・再起動しない。

巻戻しは、追加ファイルのhashが承認済みbundleと完全一致し、関係するnative子が終了した後に逆パッチを使う。変更されていれば削除せず判断待ち。ファイル設定の巻戻しは既に生成された成果・セッション・副作用を巻き戻さない。

## 保証しない範囲

自動Dot dispatchアダプター、モデル強制timeout、OS隔離、ホスト全体の並列枠、RAM/GPU hard quota、費用額の上限、localAPIの互換性はこのファイル群では提供しない。今回PoCのDAG/成果採用ポリシーは独立作業の検証済み部品であり、既存daemonへ未接続である。
