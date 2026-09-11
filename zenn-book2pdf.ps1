# zenn-book2pdf.ps1 -- Zenn の本を HTML/PDF/EPUB 化する起動ラッパー
# このファイルがあるフォルダを PATH に入れれば、どこからでも呼べます。
#   zenn-book2pdf.ps1 https://zenn.dev/USER/books/BOOK-SLUG
#   zenn-book2pdf.ps1 --epub URL
#   zenn-book2pdf.ps1 --pdf-only
#   zenn-book2pdf.ps1 --epub-only
# CmdletBinding / param は使わない（--help や -v が PowerShell に吸われるため）

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
if (-not $Root) {
    $Root = Split-Path -Parent $MyInvocation.MyCommand.Path
}

$PyScript = Join-Path $Root 'fetch_zenn_book.py'
if (-not (Test-Path -LiteralPath $PyScript)) {
    Write-Error "fetch_zenn_book.py が見つかりません: $PyScript"
    exit 1
}

$pyExe = $null
$pyPrefix = @()
foreach ($name in @('py', 'python', 'python3')) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) {
        if ($name -eq 'py') {
            $pyExe = $cmd.Source
            $pyPrefix = @('-3')
        } else {
            $pyExe = $cmd.Source
            $pyPrefix = @()
        }
        break
    }
}

if (-not $pyExe) {
    Write-Host 'Python が見つかりません。Python 3 をインストールし、PATH を通してください。' -ForegroundColor Red
    exit 1
}

Set-Location -LiteralPath $Root
& $pyExe @pyPrefix $PyScript @args
exit $LASTEXITCODE
