# Cloudflare Tunnelを使う公開リレー

各メンバーは、Cloudflare Tunnelで公開したHTTPSリレーへ接続できます。
TailscaleやNetBirdへの参加は必要ありません。
公開TLSはCloudflareが終端し、VPSのcloudflaredがループバックのirohリレーへWebSocketを転送します。
Reasonのメッセージは端末間で暗号化され、チームが管理するVPS上のリレーを通ります。
この構成ではインターネットからの入口にCloudflareを使います。

リレーには端末の公開IDによる許可リストを設定します。
端末IDの許可、Reasonのチーム招待、依頼を実行するエージェントの権限は、それぞれ別に管理します。
端末を許可しても、モデルによる本番操作や人間の承認を代行する権限は与えません。

## VPSとトンネルの確認

systemdを使うLinux、Cloudflareで管理するホスト名、名前付きトンネルを用意します。
既存のcloudflaredがある場合は、サービスの実行ユーザー、使っている設定ファイル、トンネル名、既存のホスト名を先に確認します。
トンネルの設定を例で丸ごと置き換えず、既存の経路を残してリレー用の経路を追加します。
資格情報JSON、`cert.pem`、トークン、実際の接続設定はGitへ保存しません。

この構成では公開TCP 80/443をVPSで待ち受けず、リレーを`127.0.0.1:3340`だけで起動します。
cloudflaredはVPSから外向きに接続するため、管理用SSHや既存のWebサービスの公開設定を変える必要はありません。
cloudflaredからCloudflareへの接続には、利用するQUICまたはHTTP/2に必要な外向き通信を許可します。

## Docker / Composeで起動する場合

systemdの代わりに、リポジトリ直下のDockerfileとComposeを使えます。
Linux amd64 / arm64対応で、ビルド対象に合ったバイナリを
`upstream.lock.json`から取得し、既存bootstrapと同じSHA-256検証を行います。
Reason本体やPythonは実行イメージに含めず、静的リンクされたリレーだけを
非root（UID/GID 65532）で起動します。ビルドコンテキストも許可リスト方式で、
`private/`・秘密鍵・ローカル状態・Git履歴を送りません。

```sh
mkdir -p private
cp examples/docker-relay.toml private/relay.toml
# private/relay.toml の allowlist に承認済み端末の公開IDを追加
# コンテナのUID 65532から設定ファイルを読めるようにする
chmod 644 private/relay.toml
docker compose -f compose.relay.yaml config
docker compose -f compose.relay.yaml up -d --build
docker compose -f compose.relay.yaml logs --tail=100 relay
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:3340/
```

設定ファイルは公開IDだけを含め、秘密鍵・招待・トークンを入れません。
空のallowlistでは全端末の中継を拒否します。ルートへのHTTP応答は到達確認にすぎず、
中継の許可や実際のチーム通信の成功を示しません。
コンテナ内では `0.0.0.0:3340` に待ち受けますが、Composeの公開先は
ホストの `127.0.0.1:3340` に限定しています。
既存のsystemdリレーと同時に同じポートを使わないでください。
既存cloudflaredがホスト上で動く場合、後述のトンネル経路は変更不要です。
cloudflaredも別コンテナで動く場合、そのコンテナのlocalhostはリレーではありません。
同じDockerネットワークへ接続し、トンネルのserviceを `http://relay:3340` にします。
この場合はホストへのports公開を省略できます。トンネルの資格情報はイメージへ入れません。

設定を更新したら `docker compose -f compose.relay.yaml restart relay` で再読込します。
接続は一度切れるため、端末の再接続を確認してください。
停止は `docker compose -f compose.relay.yaml down` です。設定ファイルは残ります。
更新前には旧イメージIDと設定を控え、切り戻しは旧イメージと旧設定で再作成します。
`docker build --platform linux/arm64 -t we-are-agents-relay:arm64 .` のように
別アーキテクチャ向けにもビルドできますが、その実行検証には対応ホストが必要です。
本番への配置や既存サービスの停止は、この手順追加では行いません。

## リレーの常駐化

VPS上のチェックアウトで固定バイナリを取得します。
`bootstrap`は`upstream.lock.json`のSHA-256を照合します。
systemdは取得後のバイナリを直接起動します。

```sh
uv sync --locked
uv run waa bootstrap

sudo useradd --system --user-group --home-dir /nonexistent \
  --shell /usr/sbin/nologin waa-relay
sudo install -d -o root -g root -m 0755 /opt/we-are-agents/bin
sudo install -d -o root -g waa-relay -m 0750 /etc/we-are-agents
sudo install -o root -g root -m 0755 \
  .tools/iroh-relay/v1.4.1/iroh-relay /opt/we-are-agents/bin/iroh-relay
sudo install -o root -g waa-relay -m 0640 \
  examples/public-relay.toml /etc/we-are-agents/relay.toml
sudo install -o root -g root -m 0644 \
  examples/systemd/waa-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now waa-relay.service
```

専用ユーザーや配置先が既に存在する場合は、その所有者と用途を確認してから更新します。
更新前のバイナリと設定は、サーバー内のrootだけが読める場所へ退避します。
設定例の許可リストは空なので、この時点では全端末の中継を拒否します。

## Cloudflare Tunnelへの経路追加

[cloudflared-relay.yml](../examples/cloudflared-relay.yml)は、ローカルで管理する名前付きトンネルの設定例です。
既存の設定ファイルへ、次のホスト名のルールを最後のcatch-allルールより前に追加します。
トンネルIDと資格情報ファイルの既存設定は保持します。

```yaml
ingress:
  # 既存のホスト名のルールをここに残す
  - hostname: agents.example.com
    service: http://127.0.0.1:3340
  - service: http_status:404
```

設定の検証とDNS経路の登録を行います。
`CONFIG_PATH`は実際のサービスが使う設定ファイルに、`TUNNEL_NAME`は既存のトンネル名に置き換えます。
同じホスト名のDNSレコードが既にある場合は、置き換える前に用途を確認します。

```sh
cloudflared tunnel --config CONFIG_PATH ingress validate
cloudflared tunnel --config CONFIG_PATH ingress rule https://agents.example.com/relay
cloudflared tunnel route dns TUNNEL_NAME agents.example.com
```

サービスが使う設定を保存したら、そのcloudflaredサービスを再起動します。
システムサービスかユーザーサービスかを確認し、実際の管理方式に合わせて実行してください。
共有トンネルの再起動では既存のホスト名にも短い再接続が発生するため、変更前後に既存の経路も確認します。

リレー用ホスト名には、ブラウザー操作を必要とするCloudflare Accessのログイン画面や対話的なチャレンジを挟みません。
固定しているReasonクライアントはCloudflare Accessのサービストークンを送信しないためです。
リレーの中継許可は、irohの端末IDによる許可リストで制御します。
オリジンは同じVPSのHTTPループバックなので、`noTLSVerify`は不要です。

設定方法は[Cloudflareのローカル管理トンネル設定](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/)と[WebSocketの対応](https://developers.cloudflare.com/network/websockets/)に従います。

## メンバーの端末を許可する

各メンバーは、新しいローカル状態を指定して端末の公開IDを取得します。
この操作はチーム参加前に実行でき、端末の秘密鍵を共有する必要はありません。
実際のチームで使う状態ディレクトリを指定してください。

```sh
RELAY_URL=https://agents.example.com
uv run waa network --state .local/team --relay "$RELAY_URL" -- who --json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(m["endpoint"] for m in d["machines"] if m["is_me"]))'
```

管理者は、表示された64文字の公開IDを各メンバーから受け取り、VPSの`/etc/we-are-agents/relay.toml`へ追加します。
端末IDの許可とReasonのチーム招待の両方が必要です。

```toml
access = { allowlist = [
  "端末Aの64文字の公開ID",
  "端末Bの64文字の公開ID",
] }
```

保存後に`sudo systemctl restart waa-relay.service`で読み直します。
この再起動ではリレーの接続が一度切れるため、端末の再接続も確認します。
cloudflared側の再起動は不要です。

最初のメンバーは、公開IDを取得したのと同じ状態でチームを作成します。

```sh
uv run waa network --state .local/team --relay "$RELAY_URL" -- \
  init development --as alice
```

参加者は、受け取った招待リンクを各自の`private/invite.txt`へ保存し、公開IDを許可したときと同じ状態で参加します。

```sh
uv run waa network --state .local/team --relay "$RELAY_URL" \
  --invite-file private/invite.txt -- join --as bob
```

**招待には長い`rn1-`リンクを使います。**
短い単語形式の招待コードには別のリレー用IDが使われるため、この端末IDだけを許可する構成では利用しません。
各メンバーのPiやMCPクライアントも、参加時と同じ状態ディレクトリに接続します。
この例でのPiの`reason_state`は、`.local/team`の絶対パスです。

## 稼働確認と切り戻し

```sh
sudo systemctl is-active waa-relay.service
sudo journalctl -u waa-relay.service --since '10 minutes ago' --no-pager
ss -lntp
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' https://agents.example.com/
```

`ss`でリレーがループバックだけに待ち受けることを、`curl`で公開TLSの信頼とHTTPS到達を確認します。
通信の検証では、許可した2端末でチーム参加、本文送受信、明示的な受信確認、相関する返答を確認します。
一方を切断した間に送ったメッセージが、再接続後に元のIDで一度だけ届くことも確認します。
許可していない端末が中継を拒否されることは、別の一時状態で確認します。

irohは直接QUIC接続も試みます。
メッセージ到着だけで「公開リレーを通った」と判定せず、隔離した検証端点のUDPを制限するなどして、中継経路も確認します。
リレーの稼働と、モデルの判断、T3の会話再開、実際のチーム利用は分けて記録します。
相手から届いた文は同僚からの依頼として扱い、人間の承認やネイティブな権限確認への回答には変換しません。

バイナリ更新は`upstream.lock.json`をレビューしてから行い、新旧のハッシュとバージョンを記録します。
切り戻す場合はリレーを停止し、退避したバイナリと設定を戻して起動します。
トンネルの変更を戻す場合も、共有する既存のホスト名のルールを保持してください。
リレーを外部へ直接公開する代案には、既存の[TLS設定例](../examples/relay.toml)を使えます。

許可リストとHTTP待受は、固定している[iroh-relay 1.4.1](https://github.com/n0-computer/iroh/tree/d6c6963b9602d1b8dd2ba53b6519e7eb19cc7940/iroh-relay)の機能を使います。
