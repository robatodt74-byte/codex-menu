# Codex Menu

macOSのメニューバーから、ローカルのCodexチャットの状態とログを確認するアプリです。SwiftUI/AppKitとPython標準ライブラリで動作します。

## 機能

- 複数の親チャットを一覧表示し、直近のログを確認
- メニューバーへ表示するチャットの選択・固定と再起動後の保持
- チャット名・フォルダー名による検索
- Zedで元のログを開く

| 状態 | 表示 |
| --- | --- |
| 稼働中 | `working` |
| 承認待ち | `awaiting approval` |
| 停止・状態不明 | `no work` |
| 取得エラー・監視切断 | `error` |

`no work`には状態不明も含みます。状態の理由と取得エラーの詳細はパネルに表示します。UIの説明・操作ボタンは日本語です。

## 必要な環境

- macOS 13以降
- Xcode Command Line Tools（Swiftコンパイラー）
- `/usr/bin/python3`
- このMacに保存されたCodexのローカル履歴
- 元ログを外部エディターで開く場合はZed

追加のPythonパッケージやサーバーは不要です。

## ビルド・インストール

```sh
git clone https://github.com/robatodt74-byte/codex-menu.git
cd codex-menu
bash build.sh
bash install.sh
open "$HOME/Applications/CodexMenu.app"
```

アプリは`~/Applications/CodexMenu.app`へ配置されます。`install.sh`は既存アプリを上書きせず停止します。更新するときは実行中のアプリを終了し、既存アプリを別の場所へ退避してからインストールしてください。

ビルドは一時ディレクトリでアプリを自己署名し、生成物を`build/`へ配置します。ファイルプロバイダーが生成物にFinder属性を追加すると署名検証に失敗する場合があるため、通常のローカルディレクトリにインストールしてください。配布用のDeveloper ID署名・公証やログイン時の自動起動は設定していません。

メニューバーをクリックすると一覧とログが開きます。一覧の行を選ぶと右側のログが切り替わり、「メニューバーに表示」で固定対象を変更できます。「再接続」で監視プロセスを再起動できます。

## データ取得

`CODEX_HOME`（未指定なら`~/.codex`）内の`state_*.sqlite`と`thread_history_*.sqlite`を読み取り専用で参照し、JSONLの開始・完了・中断イベントと照合します。`lsof`でセッションロックを開いているプロセスを確認します。ログ更新が止まっただけでは停止・承認待ちと判断しません。

更新順で最大1,000件の親チャットが対象です。アーカイブとサブエージェントは除外し、ローカルに記録のあるCLIセッションも含みます。約2秒間隔で更新し、監視の切断・15秒以上の無応答は`error`として表示します。ログは直近のメッセージとツール記録の抜粋です。

CodexのローカルDB・JSONL・セッションロックは安定した公開APIではありません。Codexの更新に応じて形式への対応が必要になる場合があります。クラウドだけにあるチャットは対象外です。

## 承認待ちを取得する

`awaiting approval`の検出には、Codexの`PermissionRequest` Hookの設定と信頼が必要です。

1. アプリ下部の「awaiting approval の取得を設定…」を押します。
2. 内容を確認して「追加する」を押します。
3. Codex CLIの`/hooks`でCodexMenuのHooksを確認・信頼します。既存チャットでは設定の再読み込みが必要になる場合があります。
4. 実際の承認要求で表示が変わり、ツール完了・処理終了で解除されることを確認します。

登録イベントは`UserPromptSubmit`、`PermissionRequest`、`PostToolUse`、`Stop`、`Interrupt`、`SessionEnd`です。既存のHooksを保持し、変更前の`hooks.json`を日時付きでバックアップします。アプリを移動した場合は実行先が変わるため再設定・信頼確認が必要です。

Hookは承認への回答やプロンプト変更を行いません。セッション/ターンID、時刻、保留件数、ツール入力のハッシュだけを保存します。アプリの「設定あり」は通知の受信を意味しません。実通知の最終受信時刻を別に表示します。

削除する場合は`hooks.json`から、コマンドが`CODEX_MENU_HOOK_V1=1 `で始まるハンドラーだけを削除してください。他ツールのHooksや信頼設定をまとめて戻さないでください。

公式仕様: [Codex Hooks](https://learn.chatgpt.com/docs/hooks)

## 保存先

`~/Library/Application Support/CodexMenu/`に表示対象ID、本文を含まない診断情報、Hook状態を保存します。固定対象はアプリのUserDefaultsに保存します。`CODEX_MENU_DATA`でアプリの保存先を変更できます。

監視はこのMac内で処理し、ネットワーク通信・AIによるログ判定・使用量APIの呼び出しは行いません。実行ログ、DB、資格情報、個人設定、生成済みアプリはこのリポジトリに含めていません。

## 検証

```sh
/usr/bin/python3 -m unittest discover -s . -p 'test_*.py' -v
bash build.sh
build/CodexMenu.app/Contents/MacOS/CodexMenu --self-test
build/CodexMenu.app/Contents/MacOS/CodexMenu --preview "$PWD/build/preview.png"
```

Pythonの16テストでは状態遷移、並列承認、JSONL追尾、読み取り専用DB、複数チャットの独立性を検証します。ネイティブチェックでは表示・選択・固定・ログ切替・切断/復帰を検証します。プレビューとネイティブチェックはサンプルデータを使い、実設定や固定対象を変更しません。

ローカルでビルド、テスト、配置版の署名、実チャットのログ選択・固定・再起動後の保持を確認済みです。実際の承認要求を使ったHooksのE2E検証は未確認です。
