# 固定する上流ソフトウェア

`upstream.lock.json`にリリース、コミット、配布元URL、配布アーカイブのSHA-256を記録します。
更新時はこのファイルと`uv.lock`をレビューし、既存の通信検証を再実行します。
自動で最新版へ切り替えません。

| ソフトウェア | バージョン | ライセンス | 再利用する部分 |
| --- | --- | --- | --- |
| [Reason Network](https://github.com/reason-machines/reason-network/tree/5872711927788b0d43a8a940ea45f993bc2926bc) | 0.12.17 | MIT OR Apache-2.0 | チーム、セッション、暗号化通信、受信箱 |
| [iroh-relay](https://github.com/n0-computer/iroh/tree/v1.4.1/iroh-relay) | 1.4.1 | MIT OR Apache-2.0 | 自前の暗号化通信リレー |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 2.3.0 | MIT | T3向けのHTTPクライアントとstdioサーバー |

上流のバイナリはローカルに取得し、このリポジトリには含めません。
Reason CLIとReasonのホスト型MCPサーバーは使いません。
固定エンジンの内部プロトコルを使うため、エンジンの更新時には招待形式、メッセージ形式、受信状態の意味も再確認します。

bootstrapとPython依存関係の取得には、GitHubとPyPIへの接続が必要です。
実際のエージェントがクラウドモデルを使う場合、そのモデルへの接続は別途発生します。
