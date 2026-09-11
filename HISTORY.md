# 更新履歴

## 2026-09-11 (v1.0)

- バージョンを v1.0 とした。`--version` / `-V` で表示できる。

## 2026-09-11 (README)

- README を現行機能に合わせて書き直した。起動はラッパー（`zenn-book2pdf`）と PATH 追加を主、Python 直接実行を従にした。
- EPUB の前提として Calibre（`ebook-convert`）のインストール・確認方法を独立した節にした。

## 2026-09-11 (EPUB 図サイズ)

- EPUB の Mermaid 図がページ幅いっぱいに伸びて文字サイズがバラバラになる問題を修正。PDF と同じ mm 基準で幅を％指定する。

## 2026-09-11 (EPUB Kindle 図)

- Kindle が Mermaid SVG の `foreignObject` を描かず文字が消える問題に対し、電子書籍用には図を透明 PNG へラスタライズするようにした。
- セピア／ダークモードで白い矩形が浮かないよう、透明背景と `mix-blend-mode` を EPUB 用 CSS に足した。

## 2026-09-11 (EPUB)

- EPUB 出力を追加した（`--epub` / `--epub-only`）。変換には Calibre の `ebook-convert` を使う。
- タイトル・著者・表紙を HTML から渡し、章見出し（h1）で目次と改ページを付ける。
- Windows で日本語ファイル名を直接渡すと Calibre が使用法エラーになるため、ASCII の一時ファイル経由で変換するようにした。

## 2026-09-11 (出力ファイル名)

- 結合 HTML / PDF のファイル名を固定の `book.html` / `book.pdf` から、本のタイトルに変更した。Windows で使えない文字は除く。
- `--pdf-only` は `output/` 直下の結合 HTML を探し、同じ名前の PDF を書く。

## 2026-09-11 (ラッパー / PDF 進捗)

- 起動用ラッパー `zenn-book2pdf.ps1` / `zenn-book2pdf.sh` / `zenn-book2pdf.cmd` を追加した。フォルダを PATH に通せばどこからでも起動できる。
- PDF 生成中に、画像読み込みの実数進捗と、変換処理の経過時間付きバーを表示するようにした。

## 2026-09-11

- PDF を Playwright の `page.pdf()` で自動出力するようにした。Chrome の `--print-to-pdf` 手動実行は不要。
- Mermaid 図はブラウザを図ごとに開き直さず、同一ページ上で `mermaid.render()` を順に呼ぶようにした。
- 図ソースのハッシュで `cache/mermaid/` に SVG を残し、再実行時は描画を省略する。
- 進捗を日本語の段階表示（`[1/5]` …）にした。
- CLI を追加した（`--html-only` / `--pdf-only` / `--output-dir` / `--workers` / `--verbose` / `--no-mermaid-cache`）。
- `mermaid.ink` はローカル描画が失敗したときだけの手段にし、リトライ回数を減らした。
- 通常の図は `mermaid.render()`、一部だけ旧来の一時 HTML 経路にフォールバックする。
