# 共通ポリシーの読込みと適用範囲

パスは[表記規約](path-conventions.md)の疑似パスです。シェル例は同ページの変数を設定してから使用します。原本ログは変更していません。

## 正本と共通/project分離

本文の唯一の正本はrepo rootの[`policies.md`](../policies.md)。ユーザーが提示した「AI開発オーケストレーション統合指示 v1.0」の最初のMarkdown code blockを、fenceを除きUTF-8/LF・末尾改行付きでそのまま保存した。

v1.0のSHA-256は`8fede5f946fd944485e7095ea27cb4923fb97ebf466d1e4b355ad21a50a00286`。`policies/manifest.json`は共通bundleのversion/hash、`config/policy.json`はprojectが採用するbundleと期待version/hashを指定する。後者はoperator管理の設定であり、task本文やMCPから変更する入口はない。pinの不一致を自動修復しない。

`policies/orchestration.md`、`decision-making.md`、`escalation.md`は章への索引のみ。AGENTS・role・Skillへ全文を複製しない。将来は共通bundleを`i-pp/ai-orchestration`等へ移し、project側の相対`bundle_root`とpinで採用できる。今回GitHub repo作成・公開・pushは行っていない。

project固有のnative枠は`.codex/config.toml`、既存daemon/workerの資源・期限は`scheduler-config.json`と`resource-limits.json`が引き続き管理する。共通policy loaderはこれらを置換せず、DAGや費用制御が完成したとは扱わない。

## 本文を渡すコード経路

1. `src/common_policy.py`がproject設定→manifest→正本の順に読み、schema、UTF-8、先頭version、SHA-256、期待pinを検証する。symlink・別ファイルへのmanifest誘導・壊れた入力・不一致を拒否する。
2. 既存Runnerはserver/model dispatch前に読込み、`developerInstructions`へ正本全文を1回と既存の出力schema/role指示を組み立てる。thread/startとthread/resumeの両方を通す。childの隔離filesystemにrepoをmountする必要はない。
3. 使用したsource/version/hashをexecutionの既存owner JSONとJSONLへ記録する。DB schema変更はない。実行が使うのは検証時のimmutable snapshotであり、後から編集されたファイルを同じ実行へ混ぜない。
4. 読込み失敗はモデル開始前にFAILEDとする。再開元にpolicy記録があってversion/hashが違えば再開を拒否する。古い記録にpolicy情報がなければ`legacy-unpinned`を記録し、現在の検証済み本文を渡す。

優先順位は、実行環境の上位規則・権限・ユーザーの明示的制約が先、その範囲内で共通policy、project/roleの具体化、taskという説明を組立て側に置く。本文自体は変更しない。semanticな矛盾を自動判定・解決する機構ではなく、既存のsandbox/approval/schema検証も変更しない。

## Native AgentとSkill

repo AGENTSと4 role定義は、開始時に次を実行して返る**本文・version・hashを実際に読む**手順を指定する。

```bash
python3 -B scripts/policy_context.py --format inspect
```

`.agents/skills/common-policy/SKILL.md`も同じloaderへ接続する。リンクやSkillのdescriptionだけを見て本文読込み済みとしない。参照更新・Skill形式検証だけでは、native実行でSkillが選択・実行された証拠にならない。2026-10-29の具体的承認後、通常ユーザー状態のprompt-input検査は成功し、repo規約・禁止指示・Skillの記載を確認した。4 roleと子2枠は未読込みで、config/readは対象repo自身のtrust登録不足によるproject layer無効を返した。祖先<USER_HOME>の既存trustedは変更せず、新しいrepo trustを追加していない。

## ChatGPT / Dot / local向けの明示的な受渡し

CLIは通信せず、本文を含むpayloadを組み立てる。

```bash
python3 -B scripts/policy_context.py --format packet --target dot --task '目的と完了条件'
python3 -B scripts/policy_context.py --format packet --target chatgpt --task '設計上の判断対象'
python3 -B scripts/policy_context.py --format chat --task '補助解析の要求'
```

packetには本文入りdeveloper_instructionsとsource/version/hashが入る。chat形式はsystemに本文、userにtaskを持つOpenAI Chat互換messages。`--expect-sha256`で呼出し側の期待値も照合でき、不一致時は本文をstdoutへ出さず失敗する。これらは接続・送信・受領確認を意味しない。

| 経路 | 現在の確認範囲 |
|---|---|
| 既存Runner→App Server→Codex | 実Runnerと模擬peerで本文/既存role/metadataの送信・失敗前停止・resumeを検証。実モデル/稼働daemonの新turnは未実施 |
| nativeのOrchestrator/4 roles | Agent定義からの本文組立てを検証。通常prompt-input/config/readは実行済み。対象repoのproject layerがtrust不足で無効となり、4 role/子2枠は未読込み。新規trust追加・実動は保留 |
| repo Skill | 形式・参照・loader実行と通常native初期prompt内のSkill記載を確認。モデルによる自動選択・実行は未確認 |
| ChatGPT / Dot | 本文入りpacketの組立てとpin検査を検証。新packetの自動配送・自動読込みは未接続。既存の委譲通知と混同しない |
| local LLM | Chat互換messagesの組立てを検証。30Bへの送信・モデルの遵守は未確認、現用server不変更 |

共通本文の受渡しを実装したことと、受取側モデルの遵守や全経路の自動連携は別の検証対象である。既知のChat返答取得不能に対する問い合わせループは追加していない。MCP read-only公開範囲、global設定、認証/providerは不変更。2026-10-29 08:23 UTC、承認に基づき専用daemon/Tunnelの2サービスのみ再起動し、READY・health・接続済みreadonly MCP status/list/getを確認した。実行中/待機中は0件、既存DBのtask/execution/verification/transition/decisionは前後一致。

## 更新と課題記録

本文を更新するときは採用された新原文に従い、manifestとproject pinを意図的に更新し、正本一致とpayload/失敗試験を確認する。hashエラーを消すためだけに再hashしない。既存実行の記録はその実行のversion/hashを保持する。

未解決事項は[`issues/common-policy.json`](issues/common-policy.json)へ、状態・問題・影響・暫定対応・再検討条件を保存する。正本のS0/S1等の意味をこの補足で書き換えない。
