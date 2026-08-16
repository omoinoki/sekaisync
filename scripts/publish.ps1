[CmdletBinding()]
param(
    [string]$Remote,
    [string]$Tag = "v0.3.0",
    [string]$CommitMessage = "SekaiSync v0.3.0 initial public release",
    [switch]$Push
)

# SekaiSync GitHub release helper:
#   .\scripts\publish.ps1                          # git init + commit + tag (local)
#   .\scripts\publish.ps1 --push --remote <url>    # plus push main + tags to origin

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Fail([string]$msg) { Write-Error $msg; exit 1 }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "未找到 git，请先安装 Git for Windows（https://git-scm.com）。"
}

Push-Location $root
try {
    # 1. Warn about local runtime data that must never be committed.
    foreach ($name in 'store', 'store-v1', 'work', 'dist', 'build', '.tmp-test', '.venv') {
        if (Test-Path $name) {
            Write-Warning "存在本地数据/产物目录: $name（.gitignore 已排除，不会提交）"
        }
    }

    # 2. Initialize (if needed) and stage everything.
    if (-not (Test-Path .git)) { git init -b main }
    git add -A
    $staged = @(git diff --cached --name-only)
    if ($staged.Count -eq 0) { Fail "没有可提交的文件。" }

    $bad = @($staged | Where-Object {
        $_ -match '^(store|store-v1|work|dist|build|\.tmp-test|\.venv)[\\/]' -or
        $_ -match '__pycache__|\.pyc$' -or
        $_ -eq 'sekaisync.db'
    })
    if ($bad.Count -gt 0) {
        Fail "以下文件不应进入仓库，请检查 .gitignore:`n$($bad -join "`n")"
    }

    # 3. Commit and tag.
    $authorName = if ($env:GIT_AUTHOR_NAME) { $env:GIT_AUTHOR_NAME } else { 'SekaiSync contributors' }
    $authorEmail = if ($env:GIT_AUTHOR_EMAIL) { $env:GIT_AUTHOR_EMAIL } else { 'sekaisync@users.noreply.github.com' }
    git -c user.name="$authorName" -c user.email="$authorEmail" commit -m $CommitMessage
    if (git tag -l $Tag) { git tag -d $Tag }
    git tag -a $Tag -m "SekaiSync $Tag"

    Write-Host "已提交 $($staged.Count) 个文件并打 tag $Tag。"

    # 4. Push.
    if ($Push) {
        if (-not $Remote) { Fail "--push 需要 --remote <repo-url>" }
        git remote remove origin 2>$null
        git remote add origin $Remote
        git push -u origin main --tags
        Write-Host "已推送 main 与 tag $Tag 到 $Remote"
    } else {
        Write-Host "推送到 GitHub（先建好空仓库）："
        Write-Host "  .\scripts\publish.ps1 --push --remote <repo-url>"
    }
} finally {
    Pop-Location
}
