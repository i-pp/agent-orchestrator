# 共通正本の固定版ロード

共通正本は[i-pp/ai-orchestration](https://github.com/i-pp/ai-orchestration)。このrepoは実行・監視・タスク管理と、正本を読む仕組みを所有する。正本repoへ編集・pull/reset・自動同期を行わず、本文をこちらで複製管理しない。

## 採用版と取得元

- GitHub採用commit: `aeef6db1cc9680833277f76962044da0dae501b9`
- version: `git:aeef6db1cc9680833277f76962044da0dae501b9`（原文に数値版がないためcommitを版とする）
- 本文集合のSHA-256: `3a6e2876cb93f60a0261bce0488bd67ac93053d52fb8fe6fd6821fe9489d4efd`
- 正本3文書: docs/ai/orchestration.md、docs/ai/policies.md、agents/README.md

`config/upstream-policy.json`が取得元・commit・文書ごとのSHA-256/Git blob SHAを固定し、`config/policy.json`が新正本の採用を必須化する。実行時にbranchの最新版を追わず、ネットワーク・fetch・clone・copyを追加しない。指定したローカルGit objectから全文を読み、各hashと本文集合hashを検証する。

共通の版を変更するときは正本側での変更・commitを確認し、pinを意図的に更新して試験する。hashエラーを消すためだけの自動再hashや、不達時の旧版fallbackはしない。下記ローカル設定を作っても、中央のhash pinは解除されない。

## ローカルcloneのpathとdirty

portableな既定は`../ai-orchestration`で、**agent-orchestratorのproject root基準**。`~`は実行ユーザーのhomeへ展開し、環境変数の暗黙展開はしない。絶対pathもoperatorのローカル設定で指定できる。実在を検査し、symlinkやcheckoutのsubdirectoryは拒否する。

通常は正本と一致するoriginと、採用commitのGit objectを持つ既存cloneを使う。ローカルの配置例は存在を仮定しない。まずremote・HEAD・dirtyを読み取って確かめる。ユーザーの未commit変更をreset/pullで上書きしたり、別cloneを自動作成したりしない。

既存の並行作業repoにoriginやGitHub commit objectがなくても、**operatorがローカルcommitを明示し、固定3文書の全bytesがGitHub pinと一致する場合だけ**、内容が等価なcarrierとして参照できる。originが確認できたと表示せず、canonical commit・実際の読込commit・HEAD・dirty・origin_verified=falseを別々に記録する。別repoの内容を信用してpinを更新する機能ではない。

`config/policy-source.local.json`はGit除外する。必要なら[設定例](../examples/policy-source-checkout.example.json)を参考に、実在するpathと40桁commitを明示する。これは配置設定でありcredentialを含めない。

```json
{
  "schema_version": 1,
  "checkout": "../ai-orchestration",
  "checkout_commit": "<40-hex-local-commit>"
}
```

読込metadataにはcheckout、loaded_checkout_commit、checkout_head_before/after、checkout_dirty、checkout_origin_verified、local_commit_overrideが入る。本文は常に選択commitのGit objectであり、dirtyなworking tree本文は読まない。HEADが進んでもpinを自動採用しない。古いcommit objectがなくなれば読込み失敗になる。

## 読込みと優先順位

```bash
python3 -B scripts/policy_context.py --format inspect
python3 -B scripts/policy_context.py --format packet --target dot --task '目的と完了条件'
```

`common_policy.load_project_policy`→固定source設定→`upstream_policy`→既存local Git object→hash検証→immutable bundleの順。本文はdeveloperInstructionsへ各文書1回だけ入れ、metadataをexecution ownerへ記録する。Runnerのthread/startとthread/resume、repo AGENTS/4role/Skillの読込み経路は同じCLI/loaderを使う。resume元のversion/hashが違うとモデル開始前に拒否する。

優先順位は、実行環境の上位規則・権限・ユーザーの明示的制約・既存安全ガードが先、その範囲で固定共通正本、project/roleの具体化、task。正本は権限や承認を追加せず、追加隔離禁止、公開scope、認証、費用制限、既存のJSON/schemaやsandbox検査を上書きしない。モデルの遵守をこの仕組みだけで保証しない。

不達、commit/版/hash不一致、壊れたsource設定、symlink等はPolicyErrorとなり、policy依存のdispatchをモデル開始前に止める。診断用の読み取りや独立した安全な編集まで止める意味ではない。直接の上位指示はこの正本より優先する。packet組立ては自動配送や、親dotからの直接呼出し成功の証明ではない。

## 旧v1.0との差分と扱い

旧`policies.md`は利用者原文v1.0（SHA-256 `8fede5f946fd944485e7095ea27cb4923fb97ebf466d1e4b355ad21a50a00286`）を変更せず、manifestとともに履歴として残す。採用設定は旧hashも検査するが、現在の共通本文として二重注入しない。二つの正本を並行改訂しない。

意味のある差は次のとおり。

| 差 | 今回の扱い |
|---|---|
| 共通統治とruntime実装の管理repoを明確に分離 | 共通本文は正本へ集約し、こちらは読込と実行検証を所有 |
| 旧S0〜S3に対し新正本はRoutine/Controlled/High impactの3区分 | publication/production/credential/privacy/費用等の重大影響は具体的なユーザー承認が必要。既に得た承認を再要求しない |
| ChatGPT/Dot/各Agentの役割は追加権限を与えないと明示 | 設計回答や委譲を承認と混同しない |
| 旧版のDAG/最大2並列/再試行上限/決議status/評価指標等の詳細は新正本に数値定義なし | 既存project設定・ガード・運用制約を維持し、上限を外さない。今回それらを新たに完成したと主張しない |
| 新版は数値versionがなく内容も要約された別文書集合 | v2等を捏造せずcommitと各hashで固定 |

## 検証範囲

実local checkoutの固定3文書を読込み、採用commit/hashとローカルcommit/dirtyを確認する。fixture Git repoでは正常・不達・dirty保全・異なるHEAD・等価ローカルcommit・改ざん・版不一致・path解決を検査する。文書別hashとsection境界はcontext組立て時にも再検査する。

Runner→模擬App Server peerでは実際のdeveloperInstructions・owner metadata・失敗前停止・resumeを検査する。実モデルへの新しい推論や、すべての環境での自動遵守を確認したとは扱わない。中央repoを編集する試験、namespace追加作成、認証変更は行わない。
