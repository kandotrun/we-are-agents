# we-are-agents

チームの各メンバーが使うAIエージェントを、セルフホストする通信基盤でつなぐプロジェクトです。
仕様の選択や権限の承認は人間が担い、依頼、質問、返答の取り次ぎをエージェント同士で進めます。

通信には[Reason NetworkのOSSエンジン](https://github.com/reason-machines/reason-network)を再利用します。
ReasonアカウントやReason CLIは使わず、チームが管理するirohリレーを指定します。
このリポジトリはReason Machinesの公式製品ではありません。

## 配布版と更新

[更新の手順](docs/updates.md)では、GitHub Releasesで検証した版を取得し、コードだけを切り替えます。
設定・端末鍵・未読メール・署名台帳は、版から独立した固定パスに保存します。
更新の適用と切り戻しは所有者の明示操作で行い、エージェントのMCPツールからは実行しません。

初回は公開済みのImmutable ReleaseにあるインストーラーをGitHub CLIで検証してから実行します。
導入後は、固定ランチャーを使って版の確認と更新を行います。
ランチャーをPATHへ追加するか、手順に記載した絶対パスで起動します。

```sh
waa --version
waa update status
waa update check
# 指定する版は公開済みリリースの番号に置き換える
waa update apply --version 0.2.1
# 互換性を確認した、導入済みの旧版に切り替える
waa update rollback --version 0.2.0
```

開発用チェックアウトで既に使っている`private/`や`.local/`は、その絶対パスを維持します。
更新機能は既存データの移動や、Pi・OpenClaw・T3本体の更新を行いません。

## 最初に動かす

macOSまたはLinuxのarm64とx86_64に対応します。
Python 3.11以降と[uv](https://docs.astral.sh/uv/)が必要です。

```sh
uv sync --locked
uv run waa bootstrap
uv run waa smoke
```

`bootstrap`は固定したリリースを`.tools/`に取得し、Gitに記録したSHA-256で検証します。
上流のインストールスクリプトは実行しません。

`smoke`はループバックのリレーと、独立した状態を持つ2つのデーモンを起動します。
合成セッション間の送受信、明示的な受信確認、再送時の重複抑止、両デーモンの再起動後の配送を検証し、失敗時は非ゼロで終了します。
モデル、実際のエージェント、ユーザー設定は使いません。

## 自前リレーを起動する

TailscaleやNetBirdを使わずにチームで接続する場合は、[Cloudflare Tunnelを使う公開リレー](docs/public-relay.md)を利用します。
VPS上のリレーはループバックだけで待ち受け、公開HTTPSからWebSocketを転送します。
リレーの端末公開ID許可リストと、Reasonのチーム招待の両方で参加を管理します。
DockerfileとComposeによる非rootコンテナ起動にも対応します（同手順を参照）。

VPN内に限定する場合は、次のTLS設定例を使います。

```sh
mkdir -p private
cp examples/relay.toml private/relay.toml
# private/relay.tomlのIP、証明書、秘密鍵のパスを編集する
uv run waa relay -- --config-path private/relay.toml
```

リレーのHTTPS待受をTailscaleまたはNetBirdのIPに設定し、エンジンが信頼する証明書を用意します。
証明書の発行、VPNへの参加、ACLの変更、実環境への配置は自動化していません。
自己署名証明書をそのまま使う構成には対応していません。

## チームを作成して参加する

最初のメンバーがチームを作成します。
URLはチーム自身のリレーに置き換えます。

```sh
uv run waa network --relay https://relay.example.ts.net -- \
  init development --as alice
```

表示された招待リンクを、参加者の端末の`private/invite.txt`に保存します。
リンクには参加用の秘密情報が含まれるため、Gitや公開ログに保存しません。
参加者は同じリレーを明示して接続します。

```sh
uv run waa network --relay https://relay.example.ts.net \
  --invite-file private/invite.txt -- join --as bob

uv run waa network --relay https://relay.example.ts.net -- who --json
uv run waa network --relay https://relay.example.ts.net -- team --json
```

状態は既定で`.local/network/`に保存します。
別の状態を使う場合は、`network`の直後に`--state /absolute/path/to/state`を指定します。
招待に記録されたリレーと指定したリレーが一致しなければ参加処理を始めません。
Reasonアカウントを使わないため、表示名にはReasonによる本人確認は付きません。

初回設定では、他の作業中セッションの自動公開、Reasonクラウド連携、LAN探索、自動更新を無効にします。
チーム作成前の既定リレー一覧取得はHTTPプロキシ設定で遮断します。
VPN内だけを通る経路の保証には、別途[2台での受け入れ確認](docs/acceptance.md)が必要です。

## エージェントを接続する

複数人で使う場合は、[所有者・署名・自然言語ポリシー](docs/identity.md)を先に設定します。
署名鍵を確認済みの所有者とエージェント名へ登録し、各アダプターのバインディングに`identity`を指定します。
このモードでは、未登録・未署名・改ざんされた依頼をモデルの起動や本文読取へ渡しません。
作業範囲は各所有者のローカルポリシーに自然言語で記述し、既存のツール権限と人間の承認はそのまま使います。
自然言語ポリシー自体は、OSやコーディングツールの権限を強制する仕組みではありません。

```sh
uv run waa identity init --directory private/kan-pi \
  --owner-id kan --owner-name Kan --agent-name Pi
uv run waa identity show --profile private/kan-pi/profile.json
```

[Pi連携](docs/pi.md)は、グローバル設定を書き換えず、指定した作業ディレクトリと状態でPiを起動します。
Pi 0.87.1の実プロセスで、実エンジンへの着信、処理開始、本文の読取、明示的な受信確認、相関する返答までを、ローカルの合成モデルを使って検証します。

```sh
uv run waa pi run --binding private/pi.json
```

[T3 Code連携](docs/t3.md)は、明示した1つのスレッドに新着通知をキューへ追加し、本文を専用のMCP受信箱で読む構成です。
通知の成功だけでは、本文を既読にしません。
T3の実環境への接続は、資格情報とスレッド専用の設定を用意した後に検証します。

[OpenClaw連携](docs/openclaw.md)は、署名付きのMCP受信箱とローカルの`/hooks/wake`を使います。
既存の会話や外部チャネルへテストの通知を送らないよう、チャネルを接続しない専用プロファイルで設定します。
最初に端末の公開IDと署名公開鍵を作成し、管理者が登録してからチームへ参加します。
OpenClawの認証情報や秘密鍵は、その端末内で管理します。

他のMCPクライアントは、次の起動定義で固定エンジンの送受信ツールを利用できます。
絶対パスとリレーURLを環境に合わせて置き換えます。
これは基礎的な送受信用の定義で、上記の署名付き所有者確認はPi、T3、OpenClawの専用アダプターで利用します。

```json
{
  "mcpServers": {
    "we-are-agents": {
      "command": "uv",
      "args": [
        "run", "--project", "/absolute/path/we-are-agents",
        "waa", "network", "--relay", "https://relay.example.ts.net",
        "--", "mcp"
      ]
    }
  }
}
```

この汎用定義だけでは、待機中のネイティブ会話を着信で再開できるとは限りません。
既存の上流フックには状態ディレクトリの引き継ぎが不揃いな箇所があるため、このラッパーでは上流の`setup`を公開していません。
Claude Code、Codex、Cursorの状態を固定したネイティブな起動とフックの検証は、[受け入れ項目](docs/acceptance.md)として残しています。

## 開発時の確認

```sh
uv sync --locked
uv run python -m unittest discover -v
uv run waa smoke
```

設計は[architecture.md](docs/architecture.md)、固定バージョンとライセンスは[upstream.md](docs/upstream.md)に記載しています。
ローカル検証、実エージェントの再開、T3接続、VPN内の2台通信を区別して報告します。
