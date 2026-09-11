# Zenn Book 2 PDF / EPUB

[Zenn](https://zenn.dev/) で公開されている本（Book）を取得し、章ごとの Markdown、印刷用 HTML、A4 の PDF、Kindle 向け EPUB を作るツールです。特定の本専用ではなく、Zenn の本の URL を渡せば使えます。

起動は Python を直接叩かず、同じフォルダのラッパー（`zenn-book2pdf`）から行う想定です。ラッパーはこのフォルダへ移動してから本体を呼ぶので、`output/` と `cache/` は常にこのプロジェクト配下にできます。

## できること

- Zenn の本の URL から、章一覧・タイトル・著者・表紙を取得する
- 各章を Markdown（`output/chapters/`）として保存する
- 表紙・目次・章ごとの改ページ付きの結合 HTML を作る（ファイル名は本のタイトル）
- 巻末に奥付を付ける（出典の Zenn URL と、著作権が本の作者にある旨）
- 同じ名前の A4 PDF を Playwright（Chromium）で自動出力する
- `--epub` で同じ名前の EPUB を作る（**Calibre のインストールが必須**）
- コードブロックは Pygments でシンタックスハイライトする
- `--bw` で EPUB の Mermaid 図・写真・コードハイライトを白黒の高コントラストにする（表紙はカラーのまま。Kindle 電子ペーパー向け）
- 本文中の Mermaid 図はローカルの Chromium で描画する（失敗時のみ `mermaid.ink`）
  - PDF / HTML では SVG（文字サイズが図ごとにばらつかないよう mm で実寸を焼く）
  - EPUB / Kindle では文字が消えないよう透明 PNG にし、小さい図はページ幅いっぱいに伸ばさない
- 処理の段階（`[1/5]` …）と、PDF / EPUB 変換中の進捗バーを日本語で出す

Kindle へ送る場合は、できた EPUB を Send to Kindle（Web / アプリ / メール）で送ってください。

## 前提条件

次の 3 つが必要です。EPUB を作るなら Calibre も必須です。

| 用途 | 必要なもの |
|---|---|
| 本の取得・HTML / Markdown | Python 3、`requirements.txt` のパッケージ |
| PDF、Mermaid のローカル描画、EPUB 用の図の PNG 化 | Playwright の Chromium |
| EPUB（Kindle 向け） | **Calibre**（コマンド `ebook-convert`） |

### 1. Python パッケージと Chromium

```powershell
pip install -r requirements.txt
playwright install chromium
```

`playwright install chromium` が無いと PDF は作れません。章の取得と HTML までは動きますが、Mermaid は外部サービス `mermaid.ink` に依存します。

### 2. Calibre（EPUB を作る場合の必須条件）

EPUB 変換は Calibre 付属の `ebook-convert` を呼び出します。入っていないと `--epub` / `--epub-only` は失敗します。

Windows（推奨）:

```powershell
winget install --id calibre.calibre -e
```

または公式サイトからインストールします。  
https://calibre-ebook.com/download

入れたあとは **ターミナルを開き直して**、次で確認してください。

```powershell
ebook-convert --version
```

`ebook-convert` が認識されないときは、次のいずれかです。

- インストール後にターミナルを開き直していない
- Calibre のフォルダが PATH に入っていない

PATH に載せたくない場合は、実行のたびに環境変数でフルパスを指定できます。

```powershell
$env:EBOOK_CONVERT = "C:\Program Files\Calibre2\ebook-convert.exe"
```

よくある実体の場所:

- `C:\Program Files\Calibre2\ebook-convert.exe`
- `C:\Program Files\Calibre\ebook-convert.exe`

## 起動用ラッパーと PATH

| ファイル | 使う環境 |
|---|---|
| `zenn-book2pdf.cmd` | cmd.exe、PowerShell（PATH に入れたあとの標準の呼び方） |
| `zenn-book2pdf.ps1` | PowerShell 5.1 / 7 |
| `zenn-book2pdf.sh` | Git Bash / WSL bash |

ラッパーは引数をそのまま `fetch_zenn_book.py` に渡します。

### このフォルダから直接起動する

PowerShell:

```powershell
cd C:\Users\kabuk\Documents\20260910_Zenn_Book2pdf
.\zenn-book2pdf.cmd --help
.\zenn-book2pdf.ps1 https://zenn.dev/USER/books/BOOK-SLUG
```

`.ps1` の実行を拒否されたとき:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

または `zenn-book2pdf.cmd` を使います（内部で ExecutionPolicy Bypass して `.ps1` を呼びます）。

Git Bash / WSL:

```bash
cd /c/Users/kabuk/Documents/20260910_Zenn_Book2pdf
chmod +x zenn-book2pdf.sh   # 初回のみ
./zenn-book2pdf.sh https://zenn.dev/USER/books/BOOK-SLUG
```

### PATH に追加してどこからでも起動する

このフォルダをユーザーの PATH に足すと、カレントディレクトリがどこでも `zenn-book2pdf` と打てます。出力はラッパーがプロジェクトフォルダへ移動するため、**いつもこのフォルダの `output/`** にできます。

PowerShell（ユーザー環境変数。管理者権限は不要）:

```powershell
$dir = "C:\Users\kabuk\Documents\20260910_Zenn_Book2pdf"
$cur = [Environment]::GetEnvironmentVariable("Path", "User")
if ($cur -notlike "*$dir*") {
  [Environment]::SetEnvironmentVariable("Path", "$cur;$dir", "User")
}
```

設定後は **新しいターミナル** を開いて確認します。

```powershell
Get-Command zenn-book2pdf
zenn-book2pdf --help
zenn-book2pdf --version
```

Git Bash では `~/.bashrc` などに次を足します。

```bash
export PATH="$PATH:/c/Users/kabuk/Documents/20260910_Zenn_Book2pdf"
```

新しい Git Bash を開き、`zenn-book2pdf.sh --help` で確認します。

## 使い方

URL は本のトップでも、章ビューアでも構いません。省略時はスクリプト内の `DEFAULT_URL` の本を使います。

既定では HTML のあと PDF まで作ります。Kindle 用 EPUB は `--epub` を付けます（Calibre 必須）。

```powershell
# 本を取得して HTML + PDF
zenn-book2pdf https://zenn.dev/USER/books/BOOK-SLUG

# HTML + PDF + EPUB（Calibre が必要）
zenn-book2pdf --epub https://zenn.dev/USER/books/BOOK-SLUG

# HTML まで（PDF も EPUB も作らない）
zenn-book2pdf --html-only https://zenn.dev/USER/books/BOOK-SLUG

# HTML と EPUB だけ（PDF なし。Calibre が必要）
zenn-book2pdf --html-only --epub https://zenn.dev/USER/books/BOOK-SLUG

# 既存の結合 HTML から PDF だけ作り直す（再取得しない）
zenn-book2pdf --pdf-only

# 既存の結合 HTML から EPUB だけ作り直す（再取得しない。Calibre が必要）
zenn-book2pdf --epub-only

# URL を付けた --epub-only。出典が同じなら HTML を再利用、違う／無いなら取得からやり直す
zenn-book2pdf --epub-only https://zenn.dev/USER/books/BOOK-SLUG

# Kindle 電子ペーパー向けに図とコードを白黒にする
zenn-book2pdf --epub-only --bw

# 既存 HTML から PDF と EPUB の両方
zenn-book2pdf --pdf-only --epub-only
```

PowerShell から `.ps1` で呼ぶ場合も引数は同じです。

```powershell
.\zenn-book2pdf.ps1 --epub https://zenn.dev/USER/books/BOOK-SLUG
.\zenn-book2pdf.ps1 --epub-only
```

Git Bash:

```bash
./zenn-book2pdf.sh --epub https://zenn.dev/USER/books/BOOK-SLUG
./zenn-book2pdf.sh --epub-only
```

Python を直接実行することもできます（ラッパーと同じ引数です）。

```powershell
python fetch_zenn_book.py --epub https://zenn.dev/USER/books/BOOK-SLUG
```

`--html-only` と `--pdf-only` / `--epub-only` は同時に指定できません。

## 生成されるファイル

URL 付きの通常実行では、毎回 `output/images`・`output/chapters` と前回の結合 HTML / PDF / EPUB を作り直します。`--pdf-only` / `--epub-only` は既存の結合 HTML をキャッシュとして使い、Zenn からの再取得はしません。ただし URL を付けたときは、HTML の出典（奥付の Zenn URL）と一致する場合だけ再利用し、違う本・出典不明・HTML 無しのときは取得からやり直します。Mermaid の描画結果は `cache/mermaid/` に残り、再実行では描画を省略できます。

| ファイル / フォルダ | 内容 |
|---|---|
| `output/chapters/NN-slug.md` | 章ごとの Markdown |
| `output/images/` | 本文画像と Mermaid 図（SVG。EPUB 用に PNG も作る） |
| `output/<本のタイトル>.html` | 表紙・目次・全章の印刷用 HTML（巻末に出典 URL と著作権表示） |
| `output/<本のタイトル>.pdf` | A4 の結合 PDF（`--html-only` のときは作らない） |
| `output/<本のタイトル>.epub` | Kindle 向け EPUB（`--epub` / `--epub-only` のとき。Calibre 必須） |
| `output/_debug_book_api.json` | Zenn API の生レスポンス（調査用） |
| `cache/mermaid/` | 図のキャッシュ（`output/` の外。再実行で描画を省略） |

ファイル名は本のタイトルです。Windows で使えない文字は除きます。

## 引数

ラッパーに渡す引数です。`zenn-book2pdf --help` でも確認できます。

| 引数 | 必須 | 説明 |
|---|---|---|
| 第1引数（URL） | 任意 | 本のトップまたは章ビューアの URL。省略時は内蔵のデフォルト。`--pdf-only` / `--epub-only` に付けると、既存 HTML の出典と照合する |
| `--html-only` | 任意 | PDF を作らない。`--epub` と併用すれば HTML と EPUB だけ作る |
| `--pdf-only` | 任意 | 既存の結合 HTML から PDF だけ作る。URL を付けて出典が違う／無いときは再取得する |
| `--epub` | 任意 | EPUB も作る。**Calibre（ebook-convert）が必須** |
| `--epub-only` | 任意 | 既存の結合 HTML から EPUB だけ作る。URL を付けて出典が違う／無いときは再取得する。**Calibre が必須** |
| `--bw` | 任意 | EPUB の図・写真・コードを白黒の高コントラストにする（表紙はカラーのまま。Kindle 電子ペーパー向け）。PDF はカラーのまま |
| `--no-epub` | 任意 | `--epub` を打ち消す |
| `--output-dir DIR` | 任意 | 出力フォルダ。既定は `output`（ラッパー利用時はプロジェクト配下） |
| `--workers N` | 任意 | 章取得の並列数。既定は 6 |
| `--verbose` / `-v` | 任意 | Playwright や Calibre の詳細ログ |
| `--version` / `-V` | 任意 | バージョンを表示して終了する（現在は v1.1） |
| `--no-mermaid-cache` | 任意 | 図キャッシュを使わず全部描き直す |

## エラーメッセージと対処法

### `ModuleNotFoundError: No module named 'requests'`（または `bs4`, `markdown` 等）

```powershell
pip install -r requirements.txt
```

### Playwright がインストールされていません / Chromium を起動できませんでした

```powershell
pip install playwright
playwright install chromium
```

HTML までできているときは、導入後に `zenn-book2pdf --pdf-only` だけでも PDF を作れます。

### EPUB の生成には Calibre の ebook-convert が必要です

EPUB の前提条件を満たしていません。

1. `winget install --id calibre.calibre -e` または公式インストーラで Calibre を入れる
2. ターミナルを開き直す
3. `ebook-convert --version` が通るか確認する
4. 通らないときは `$env:EBOOK_CONVERT` に `ebook-convert.exe` のフルパスを入れる
5. 既存 HTML があれば `zenn-book2pdf --epub-only` で EPUB だけ作り直せる。HTML が無いときは URL を付けて `zenn-book2pdf --epub-only URL`

### Mermaid 図の描画で `mermaid.ink` のエラーが出る／時間がかかる

ローカル描画が失敗した図だけ `mermaid.ink` に送ります。`playwright install chromium` をやり直してください。`--verbose` で図ごとの失敗理由が出ます。

### EPUB で図の文字が消える／図がページ幅いっぱいに広がる／白い箱が浮かぶ

過去の版の既知の不具合です。最新版では EPUB 用に図を透明 PNG 化し、幅は PDF と同じ基準で％指定します。`output` を消してから `zenn-book2pdf --epub-only URL` で取得し直してください。

### 章が正しく取得できない／本の一部しか取得されない

非公開・有料部分や、Zenn API の変化が原因のことがあります。`output/_debug_book_api.json` で実際の応答を確認できます。

### その他

コンソールに出たメッセージ全文を共有してください。`--verbose` を付けると Playwright / Calibre 側の詳細も出ます。章の取得は、一部が失敗しても他の章を止めないようになっています。
