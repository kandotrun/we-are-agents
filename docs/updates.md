# GitHub Releasesからの更新

WAAは、GitHub Releasesで公開した版を指定してインストールします。
更新確認は自動で行えますが、更新の適用と旧版への切り戻しは所有者が明示的に実行します。
エージェント間の通信には、引き続きチームが管理するリレーを使います。

この手順は配布の仕組みを説明するものです。
正式リリースの公開と、社内リレーへの配置は運用担当が実行します。
以下の`v0.2.0`は初回リリースの例なので、利用時は公開済みのタグを指定してください。

## 配布物と対応範囲

リリースには、次の3ファイルを添付します。

| ファイル | 内容 |
| --- | --- |
| `we-are-agents-v0.2.0.tar.gz` | Pythonコード、Pi拡張、依存ロック、固定エンジン情報、設定例、運用手順 |
| `release-manifest.json` | 版、ソースコミット、配布物のサイズとSHA-256、状態形式とエンジンの互換条件 |
| `waa-install.py` | Python標準ライブラリだけで実行する初回インストーラー |

WAAの更新は、Pi、OpenClaw、T3 Code自体のグローバル更新を行いません。
リリース情報に記載された検証済みの版を使い、各ランタイムの更新は所有者が別途行います。
最初の配布形式は、macOSとLinux向けのソースアーカイブです。

インストーラーは、GitHubの署名付きリリース情報と各添付ファイルを`gh release verify`および`gh release verify-asset`で照合します。
さらに、マニフェストに記録されたサイズとSHA-256を照合してから展開します。
検証できないリリースを、そのまま実行する経路は用意していません。
GitHubが自動生成する「Source code」のZIPやtarballではなく、上記の添付ファイルを使います。
[GitHubの検証手順](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/verify-release-integrity)も参照してください。

## 初回インストール

Python 3.11以降、`uv`、`gh release verify`と`gh release verify-asset`に対応したGitHub CLIを用意します。
GitHub CLIの認証が必要な場合は、所有者がその端末内で行います。
認証情報をチームへ送る必要はありません。
WAAの実行環境には、`uv`が管理するPython 3.13とロック済みの依存関係を用意します。
端末に別途インストールされたPythonは、この実行環境には選びません。

新しい一時フォルダーへインストーラーを取得し、検証してから実行します。

```sh
WAA_RELEASE_VERSION=0.2.0
WAA_RELEASE_TAG="v$WAA_RELEASE_VERSION"
WAA_BOOTSTRAP_DIR=$(mktemp -d)

(
  set -eu
  gh release verify "$WAA_RELEASE_TAG" --repo kandotrun/we-are-agents
  gh release download "$WAA_RELEASE_TAG" --repo kandotrun/we-are-agents \
    --pattern waa-install.py --dir "$WAA_BOOTSTRAP_DIR"
  gh release verify-asset "$WAA_RELEASE_TAG" "$WAA_BOOTSTRAP_DIR/waa-install.py" \
    --repo kandotrun/we-are-agents
  python3 "$WAA_BOOTSTRAP_DIR/waa-install.py" install --version "$WAA_RELEASE_VERSION"
)
```

検証コマンドが失敗すると、このブロックはインストーラーを実行する前に中断します。
GitHub CLIの対応状況とリリースの公開状態を確認してから再実行します。
インストーラーも、取得するリリースと配布物を同じ基準で検証します。
依存関係、固定バイナリ、隔離した通信smoke、版の確認を終えてから新しい版を有効にします。
この準備中に、実際のエージェントやモデルは起動しません。

保存先は`WAA_HOME`、`XDG_DATA_HOME/we-are-agents`、`~/.local/share/we-are-agents`の順に選びます。
以下は既定の保存先を使った例です。

```text
~/.local/share/we-are-agents/
  bin/waa                 固定したランチャー
  current                 有効なreleases/<version>へのリンク
  previous                直前の版へのリンク
  releases/<version>/     コードとその版の実行環境
  config/                 固定した設定の保存先
  state/                  固定したReason状態などの保存先
  workspace/              固定した専用作業フォルダー
```

カスタム保存先は、初回から同じ絶対パスを指定して使います。
インストール済みの保存先を後から移動する操作は、この更新機能には含めていません。

以下の例ではランチャーを絶対パスで呼び出します。
既定の保存先で短い`waa`コマンドを使いたい場合は、現在のシェルにPATHを追加できます。
継続して使う場合のシェル設定は、所有者が管理します。

```sh
export PATH="$HOME/.local/share/we-are-agents/bin:$PATH"
waa --version
```

カスタム保存先では、その保存先の`bin/`を指定します。

```sh
python3 "$WAA_BOOTSTRAP_DIR/waa-install.py" install \
  --version "$WAA_RELEASE_VERSION" --home /absolute/stable/path/we-are-agents
```

## 新しいメンバーの設定

新しいメンバーは、`config/`にバインディングと署名プロフィールを、`state/`にReasonの状態を置きます。
端末の公開IDを作ったのと同じReason状態でチームへ参加し、アダプターの`reason_state`にも同じ絶対パスを設定します。
PiやOpenClawの専用workspaceは、`releases/<version>/`の外に固定します。
具体的な接続設定は[所有者と署名](identity.md)、[Pi連携](pi.md)、[OpenClaw連携](openclaw.md)、[T3 Code連携](t3.md)に従います。

たとえば、既定の保存先で受信箱を起動するときは、固定したランチャーと設定を指定します。

```sh
~/.local/share/we-are-agents/bin/waa openclaw inbox \
  --binding ~/.local/share/we-are-agents/config/openclaw.json \
  --session agent:waa-test:main
```

MCPの起動定義にも`bin/waa`の絶対パスを使います。
`releases/<version>/`内のPython実行環境を、クライアント設定へ直接書く必要はありません。

## チェックアウトから移行するメンバー

既存メンバーは、現在の`private/`、`.local/`、workspaceを元の絶対パスに残して、コードの起動先だけをインストール済みの`bin/waa`へ変更します。
既存のバインディングも、元のファイルを絶対パスで指定します。
更新機能がこれらを新しい保存先へ自動でコピーすることはありません。
`network`コマンドにも、従来のReason状態を`--state`で明示します。
インストール版の既定値は、新規メンバー用の固定した`state/network/`です。

PiとOpenClawの受信箱は、Reason状態とworkspaceの解決済みパスを識別に使います。
署名の重複防止台帳も、プロフィールの解決済みパスに対応しています。
そのため、リリースごとのフォルダーへ状態やプロフィールを作り直すと、従来の受信箱や台帳が使われなくなる場合があります。
移行時も、鍵、Reason状態、プロフィール、wake用バインディングとその隣のSQLiteファイルを同じパスで保持します。

```sh
~/.local/share/we-are-agents/bin/waa t3 inbox \
  --binding /absolute/existing/checkout/private/t3.json

~/.local/share/we-are-agents/bin/waa network \
  --state /absolute/existing/reason-state \
  --relay https://relay.example.com -- who --json
```

元のチェックアウトは、残している設定や状態の保存先として扱います。
それらを参照している間は、そのフォルダーを削除や改名の対象にしません。

## 更新確認と適用

インストール状態と公開済みの安定版を確認します。
コマンド結果はJSONで返ります。
カスタム保存先を使った場合は、各コマンドに`--home`で同じパスを指定します。

```sh
~/.local/share/we-are-agents/bin/waa update status
~/.local/share/we-are-agents/bin/waa update check
```

対話的な端末で`waa pi run`を使う場合は、起動時に最大1日1回、更新を確認して新版を通知します。
この通知はstderrへ出し、MCPの標準入出力では実行しません。
確認に失敗した場合も、自動で版を切り替えることはありません。
自動確認を止める場合は`WAA_NO_UPDATE_CHECK=1`を設定し、必要なときに`update check`を実行します。

次版が`0.2.1`として公開された場合の適用例です。

```sh
~/.local/share/we-are-agents/bin/waa update apply --version 0.2.1
~/.local/share/we-are-agents/bin/waa update status
```

新しいコードは最終的な保存先で準備し、検証完了後に`current`を切り替えます。
準備に失敗した場合は、現在の版を使い続けます。
旧版のコードと実行環境は残します。

動作中のMCP受信箱やwakeプロセスは、所有者が再起動するまで起動済みの版を使います。
更新結果の`restart_required`を確認し、所有者が管理するプロセスを再起動してから`identity`と受信箱を確認します。
受信箱を再起動すると、ACK可能なIDの集合はリセットされるため、本文を読み直してから明示的にACKします。

Linuxで毎日の確認を行う場合は、[ユーザーサービス](../examples/systemd/waa-update-check.service)と[タイマー](../examples/systemd/waa-update-check.timer)を使えます。
このタイマーは`check`だけを実行します。
既定と異なる保存先を使う場合は、サービスの`ExecStart`にある実行ファイルと`--home`を変更します。
ユーザーサービスの環境から`gh`を実行できることを確認してください。

```sh
mkdir -p ~/.config/systemd/user
cp ~/.local/share/we-are-agents/current/examples/systemd/waa-update-check.service \
  ~/.config/systemd/user/
cp ~/.local/share/we-are-agents/current/examples/systemd/waa-update-check.timer \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now waa-update-check.timer
systemctl --user list-timers waa-update-check.timer
journalctl --user -u waa-update-check.service --no-pager
```

この操作は、ユーザーログアウト後の常駐設定を追加しません。

macOSでは、[LaunchAgentの例](../examples/launchd/ai.mukumo.waa-update-check.plist)を使えます。
`/ABSOLUTE/WAA_HOME`を実際の固定した保存先に置き換え、`PATH`に記載した場所から`gh`を起動できることを確認します。
この例はログイン時と24時間ごとに`check`を実行し、結果を保存先内のログへ書きます。

```sh
mkdir -p ~/Library/LaunchAgents
cp ~/.local/share/we-are-agents/current/examples/launchd/ai.mukumo.waa-update-check.plist \
  ~/Library/LaunchAgents/
# コピーしたplistの/ABSOLUTE/WAA_HOMEを実際の絶対パスへ変更する
plutil -lint ~/Library/LaunchAgents/ai.mukumo.waa-update-check.plist
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/ai.mukumo.waa-update-check.plist
launchctl print "gui/$(id -u)/ai.mukumo.waa-update-check"
```

タイマーやLaunchAgentを使わないメンバーは、必要なときに`update check`を実行します。

## コードの切り戻し

インストール済みで互換性のある旧版を指定します。

```sh
~/.local/share/we-are-agents/bin/waa update rollback --version 0.2.0
~/.local/share/we-are-agents/bin/waa update status
```

切り戻しはコードの参照先だけを変更します。
鍵、未読メール、署名の重複防止台帳、wake履歴、所有者の登録情報は旧版へ戻しません。
状態形式またはReasonエンジンの固定版が異なる場合は、通常の更新と切り戻しを拒否します。
その場合は、対象リリースに用意した移行手順を運用担当が確認してから進めます。

## リリースを準備する担当者

リポジトリ管理者は、最初の公開前にGitHubのリポジトリ設定でImmutable releasesを有効にします。
この設定の有効化や、正式リリースの公開はワークフローが自動で行いません。
公開後にファイルやタグを変更できなくなるため、[GitHubのImmutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)を確認してください。

続いて、このリポジトリだけを対象にしたFine-grained personal access tokenへ、Repository permissionsの`Administration: Read-only`を付けます。
トークンをGitHubのリポジトリ設定の`Secrets and variables → Actions`で、`RELEASE_SETTINGS_TOKEN`というシークレットに登録します。
このトークンにContentsの書き込み権限は不要です。
有効期限を設定し、期限が切れる前にシークレットを更新してください。

この認証はImmutable設定を確認する1ステップにだけ渡します。
標準の`GITHUB_TOKEN`にはこのAPIに必要なAdministration権限を付けられないため、[設定確認APIの権限](https://docs.github.com/en/rest/repos/repos#check-if-immutable-releases-are-enabled-for-a-repository)に従って分けています。
Draftの作成と配布物の添付には、そのジョブの`contents: write`付き`GITHUB_TOKEN`を使います。
シークレットが未登録、期限切れ、権限不足などで確認できない場合は下書きを作らずに停止し、認証を確認するメッセージを出します。
認証済みの応答で設定が無効と確認できた場合にだけ、有効化を案内します。
トークンの値をソース、コマンド例、Issue、チャットへ貼り付けないでください。

配布する版の`pyproject.toml`、`uv.lock`、`upstream.lock.json`と変更内容をPRで確認し、CIが通ったコミットをmainへ統合します。
ソースの固定版から配布物を作るため、ビルド時は追跡中の変更がない状態を使います。

```sh
uv run python scripts/build_release.py --output /absolute/path/to/release-output \
  --tag v0.2.0
```

このビルドは配布物をローカルへ出力します。
公開処理は行いません。
出力先はソースツリーの外を指定すると、ビルド後も作業ツリーを確認しやすくなります。

公開する版のコミットへタグを付けてpushすると、`Prepare release`ワークフローが起動します。
`MERGED_COMMIT_SHA`は、版とCI結果を確認した実際のコミットへ置き換えます。

```sh
git tag v0.2.0 MERGED_COMMIT_SHA
git push origin v0.2.0
```

ワークフローを手動起動する場合も、GitHub上に存在する同じタグを指定します。
ワークフローはタグが指すコミットをチェックアウトし、そのコミットがmainに含まれること、バージョンの一致、テストを確認して、3つの配布物を下書きリリースへ添付します。
公開済みリリースへの上書きは行いません。

担当者は下書きの配布物、ソースコミット、互換条件、変更履歴を確認してからGitHub上でPublish releaseを実行します。
署名付きのリリース情報はImmutable releaseの公開時に作られるため、下書きの段階ではクライアントへ配布しません。
公開後は初回インストールの手順で配布物の検証を確認し、一部のテスト端末で更新してからチームへ案内します。

## 社内リレーの更新

リレーは現在、固定した`iroh-relay`バイナリをsystemdから直接起動します。
Cloudflare Tunnelは別サービスとして管理し、リレーのバイナリ更新と同時にトークンや経路を変更しません。
設置自体は[公開リレーの手順](public-relay.md)に従います。

WAAの版が上がっても、リレーの固定版が同じ場合はリレーの再配置は不要です。
更新担当者は、検証済みリリースの`release.json`と`upstream.lock.json`で対象のリレー版を確認します。
リレーの版が変わる場合は、対応クライアントとの通信検証を終えてから、利用者に再接続が発生する時間を案内します。

以下は、VPS上の既存構成にバイナリだけを反映する例です。
`WAA_RELEASE_DIR`は検証済みで準備済みのリリースに、`WAA_RELAY_VERSION`はそのリリースの固定版に置き換えます。

```sh
WAA_RELEASE_DIR=/absolute/stable/path/we-are-agents/releases/0.2.0
WAA_RELAY_VERSION=v1.4.1
WAA_RELAY_CANDIDATE="$WAA_RELEASE_DIR/.tools/iroh-relay/$WAA_RELAY_VERSION/iroh-relay"
WAA_RELAY_BACKUP="/etc/we-are-agents/backups/relay-$(date -u +%Y%m%dT%H%M%SZ)"

"$WAA_RELAY_CANDIDATE" --version
sha256sum "$WAA_RELAY_CANDIDATE"
sudo /opt/we-are-agents/bin/iroh-relay --version
sudo sha256sum /opt/we-are-agents/bin/iroh-relay
sudo systemctl is-active waa-relay.service
```

候補は、そのリリースの`waa bootstrap`で取得したバイナリを使います。
`bootstrap`は固定アーカイブのSHA-256を照合します。
候補と稼働中のバイナリが同じ場合は、この後の置き換えを省きます。

更新する場合は、旧バイナリをrootだけが読めるフォルダーへ残してから、同じファイルシステム内で切り替えます。
設定のコピーは監査用であり、バイナリ切り戻し時に古い許可リストへ戻すためのものではありません。

```sh
sudo install -d -o root -g root -m 0700 "$WAA_RELAY_BACKUP"
sudo cp -p /opt/we-are-agents/bin/iroh-relay "$WAA_RELAY_BACKUP/iroh-relay"
sudo cp -p /etc/we-are-agents/relay.toml "$WAA_RELAY_BACKUP/relay.toml"
sudo install -o root -g root -m 0755 "$WAA_RELAY_CANDIDATE" \
  /opt/we-are-agents/bin/iroh-relay.next
sudo sha256sum /opt/we-are-agents/bin/iroh-relay.next
```

配置後のハッシュが候補と一致したことを確認してから、対象のリレーだけを再起動します。

```sh
sudo mv /opt/we-are-agents/bin/iroh-relay.next /opt/we-are-agents/bin/iroh-relay
sudo systemctl restart waa-relay.service
sudo systemctl is-active waa-relay.service
curl --noproxy '*' --fail --silent --show-error --retry 5 --retry-all-errors \
  --retry-delay 1 --max-time 3 http://127.0.0.1:3340/healthz
sudo sha256sum /opt/we-are-agents/bin/iroh-relay
ss -lntp
```

`/healthz`は、固定している[iroh-relay 1.4.1のHTTPハンドラー](https://github.com/n0-computer/iroh/blob/d6c6963b9602d1b8dd2ba53b6519e7eb19cc7940/iroh-relay/src/server.rs#L1028)が提供します。
JSONの`status`と`version`を確認し、待受が`127.0.0.1:3340`のままであることを確認します。
HTTPの成功は、許可端末による中継やモデルの処理完了を示すものではありません。
公開ホスト名でのTLS到達、許可済み端末の再接続、短い本文の送受信と明示的ACKは、それぞれ別に確認します。

サービスや疎通の確認に失敗した場合は、退避したバイナリを同じ方法で戻します。
`WAA_RELAY_BACKUP`は、先ほど記録したバックアップの絶対パスを使います。

```sh
sudo install -o root -g root -m 0755 "$WAA_RELAY_BACKUP/iroh-relay" \
  /opt/we-are-agents/bin/iroh-relay.rollback
sudo mv /opt/we-are-agents/bin/iroh-relay.rollback /opt/we-are-agents/bin/iroh-relay
sudo systemctl restart waa-relay.service
sudo systemctl is-active waa-relay.service
curl --noproxy '*' --fail --silent --show-error --retry 5 --retry-all-errors \
  --retry-delay 1 --max-time 3 http://127.0.0.1:3340/healthz
sudo sha256sum /opt/we-are-agents/bin/iroh-relay
```

戻したハッシュとHTTPの版を更新前の記録へ照合し、許可済み端末の再接続を確認します。
この切り戻しでは、端末の許可リスト、失効済みの鍵、Cloudflare Tunnelの資格情報や経路を変更しません。

## 公開しないデータ

配布物には、チームの招待、接続トークン、端末の秘密鍵、メッセージ本文、個別のバインディングを入れません。
所有者の登録簿は、現在も運用担当が確認済みの内容を各受信側へ別途配布します。
鍵の追加や失効を、コードのリリース待ちにしない運用にします。
この更新機能が、登録簿や権限をリレー経由で自動配布するわけではありません。

## 検証の区分

配布物のビルド、検証失敗時の拒否、更新とコード切り戻し、固定した保存先の保持はローカルの隔離テストで確認します。
テスト中にGitHubの署名検証を代替する実装を使う場合、その結果は公開リリースの真正性を検証した証拠にはしません。
正式なImmutable releaseの公開、所有者による初回インストール、社内リレーへの反映、実メンバー間の往復確認は、実際に行った結果を別々に記録します。
