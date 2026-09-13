<#
.SYNOPSIS
把 SekaiSync Desktop 从主仓 frontends/WinUI3 拆成一个独立公开仓库，并用 git submodule 把主仓接回去。

.DESCRIPTION
背景：frontends/WinUI3 目前根本没进过 git 历史（被 .git/info/exclude 本地挡着，远端也没有），
所以这次拆分不需要拆历史，本质是"导出一份干净源码树 -> 推成新仓库 -> 主仓改成 submodule"。

五个阶段，各自独立，按顺序跑：

  Status    只读体检。源目录、staging、远端、submodule、exclude 现状，一次看全。
  Export    导出干净源码树到 staging（剔除 bin/obj/.vs），套用 seed 骨架文件，git init + 首次提交。
  Build     在 staging 里跑 dotnet build，验证脱离主仓后仍能编译。
  Publish   把 staging 推到远端。远端仓库需要你先在网页上创建好（脚本不建库、不碰 token）。
  Link      主仓改造：备份并移走 frontends/WinUI3，从 .git/info/exclude 摘掉相关行，
            git submodule add，本地提交。这一步不会 push，主仓的推送留给你手工确认后再做。

所有写操作都支持 -DryRun 只打印不执行。任何阶段都不删除数据：Export 拒绝覆盖非空目录（除非 -Force），
Link 把原目录整体移动到仓库外的备份位置而不是删掉。

.EXAMPLE
  .\split-desktop-repo.ps1 -Stage Status
  .\split-desktop-repo.ps1 -Stage Export -DryRun
  .\split-desktop-repo.ps1 -Stage Export
  .\split-desktop-repo.ps1 -Stage Build
  .\split-desktop-repo.ps1 -Stage Publish
  .\split-desktop-repo.ps1 -Stage Link -DryRun
  .\split-desktop-repo.ps1 -Stage Link
#>

[CmdletBinding()]
param(
    [ValidateSet('Status', 'Export', 'Build', 'Publish', 'Link')]
    [string]$Stage = 'Status',

    # 主仓里桌面端源码的当前位置
    [string]$SourcePath = 'C:\dsh_projects\sekaisync-handoff-2026-08-14\frontends\WinUI3',

    # 新仓库的本地检出位置（脚本的产出物）
    [string]$TargetPath = 'C:\dsh_projects\sekaisync-winui3',

    # 新仓库的远端地址
    [string]$RepoUrl = 'https://github.com/omoinoki/sekaisync-winui3.git',

    # 主仓路径
    [string]$MainRepoPath = 'C:\dsh_projects\sekaisync-handoff-2026-08-14',

    # Link 阶段把原 frontends/WinUI3 移到哪里；默认为主仓的上一级目录
    [string]$BackupRoot = '',

    # 只打印将要执行的命令，不做任何修改
    [switch]$DryRun,

    # Export 阶段允许写入已存在的非空目标目录（合并式，绝不删除已有文件）
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# 探针类 git 调用禁止弹凭据窗口，否则在无交互环境里会挂住
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = 'never'

$SubmodulePath = 'frontends/WinUI3'
$SeedDir = Join-Path $PSScriptRoot 'seed'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

# ---------------------------------------------------------------- 输出与执行辅助

function Write-Head {
    param([string]$Text)
    Write-Host ''
    Write-Host ('=' * 64) -ForegroundColor DarkCyan
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ('=' * 64) -ForegroundColor DarkCyan
}

function Write-Section {
    param([string]$Text)
    Write-Host ''
    Write-Host ("-- " + $Text) -ForegroundColor Cyan
}

function Write-Info {
    param([string]$Text)
    Write-Host ("   " + $Text)
}

function Write-Ok {
    param([string]$Text)
    Write-Host ("   [ok] " + $Text) -ForegroundColor Green
}

function Write-Warn {
    param([string]$Text)
    Write-Host ("   [!] " + $Text) -ForegroundColor Yellow
}

function Write-Bad {
    param([string]$Text)
    Write-Host ("   [x] " + $Text) -ForegroundColor Red
}

function Show-Cmd {
    param([string]$Exe, [string[]]$Arguments)
    $label = $Exe
    if ($Exe -match '[\\/]') { $label = [System.IO.Path]::GetFileNameWithoutExtension($Exe) }
    $shown = @()
    foreach ($a in $Arguments) {
        if ($a -match '\s') { $shown += ('"' + $a + '"') } else { $shown += $a }
    }
    Write-Host ("   > " + $label + " " + ($shown -join ' ')) -ForegroundColor DarkGray
}

# 解析外部工具的真实路径。PowerShell 会话的 PATH 未必和用户终端一致，
# 所以除了 Get-Command，再补一组常见安装位置做兜底。
function Resolve-ToolPath {
    param(
        [Parameter(Mandatory)][string]$Name,
        [string[]]$Fallbacks = @()
    )
    $found = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    foreach ($candidate in $Fallbacks) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

# 执行会改变状态的命令；DryRun 下只打印
function Invoke-Mutation {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$WorkDir = ''
    )
    Show-Cmd -Exe $Exe -Arguments $Arguments
    if ($DryRun) { return }

    $pushed = $false
    if ($WorkDir) { Push-Location -LiteralPath $WorkDir; $pushed = $true }
    try {
        & $Exe @Arguments | Out-Host
        $code = $LASTEXITCODE
    }
    finally {
        if ($pushed) { Pop-Location }
    }
    if ($code -ne 0) {
        throw ("命令失败（exit " + $code + "）：" + $Exe + " " + ($Arguments -join ' '))
    }
}

# 执行会改变状态的 PowerShell cmdlet；DryRun 下只打印。
# 单独一个函数的原因：cmdlet 不会更新 $LASTEXITCODE，套用 Invoke-Mutation 会读到上一次 git 的残留退出码。
function Invoke-CmdletMutation {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$WorkDir = ''
    )
    Show-Cmd -Exe $Name -Arguments $Arguments
    if ($DryRun) { return }

    $pushed = $false
    if ($WorkDir) { Push-Location -LiteralPath $WorkDir; $pushed = $true }
    try {
        & $Name @Arguments | Out-Host
    }
    catch {
        throw ("命令失败：" + $Name + " :: " + $_.Exception.Message)
    }
    finally {
        if ($pushed) { Pop-Location }
    }
}

# 执行只读的 git 调用并拿到输出；无论 DryRun 与否都会真的跑（只读，安全）
function Get-GitOutput {
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$WorkDir = ''
    )
    $pushed = $false
    if ($WorkDir) { Push-Location -LiteralPath $WorkDir; $pushed = $true }
    try {
        $out = & $GitExe @Arguments 2>$null
        $code = $LASTEXITCODE
    }
    finally {
        if ($pushed) { Pop-Location }
    }
    return @{ Code = $code; Lines = @($out) }
}

function Get-NativeOutput {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$WorkDir = ''
    )
    $pushed = $false
    if ($WorkDir) { Push-Location -LiteralPath $WorkDir; $pushed = $true }
    try {
        $out = & $Exe @Arguments 2>$null
        $code = $LASTEXITCODE
    }
    finally {
        if ($pushed) { Pop-Location }
    }
    return @{ Code = $code; Lines = @($out) }
}

function Test-GitRepo {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    return (Test-Path -LiteralPath (Join-Path $Path '.git'))
}

function Get-RemoteHeads {
    param([string]$Url)
    return (Get-GitOutput -Arguments @('ls-remote', '--heads', $Url))
}

# ---------------------------------------------------------------- 外部工具

$GitExe = Resolve-ToolPath -Name 'git' -Fallbacks @(
    'C:\Program Files\Git\cmd\git.exe',
    (Join-Path $env:LOCALAPPDATA 'Programs\Git\cmd\git.exe')
)
$DotNetExe = Resolve-ToolPath -Name 'dotnet' -Fallbacks @('C:\Program Files\dotnet\dotnet.exe')
$RobocopyExe = Resolve-ToolPath -Name 'robocopy' -Fallbacks @((Join-Path $env:SystemRoot 'System32\Robocopy.exe'))

if (-not $GitExe) {
    throw '找不到 git。确认 Git for Windows 已安装，或把 git.exe 所在目录加进 PATH 后重试。'
}

# ---------------------------------------------------------------- Status

function Invoke-StatusStage {
    Write-Head '阶段 1/5  Status  只读体检'

    Write-Section '源目录（主仓里的桌面端）'
    if (Test-Path -LiteralPath $SourcePath) {
        $csproj = Join-Path $SourcePath 'SekaiSync.Desktop.csproj'
        $srcCount = @(Get-ChildItem -LiteralPath $SourcePath -Recurse -File -Force |
            Where-Object { $_.FullName -notmatch '\\(bin|obj|\.vs)\\' }).Count
        Write-Ok ("存在：" + $SourcePath)
        Write-Info ("项目文件：" + (Test-Path -LiteralPath $csproj))
        Write-Info ("源码文件数（不含 bin/obj/.vs）：" + $srcCount)
        if (Test-Path -LiteralPath $SourcePath) {
            $tracked = Get-GitOutput -Arguments @('ls-files', '--', $SubmodulePath) -WorkDir $MainRepoPath
            if ($tracked.Code -ne 0) {
                Write-Warn 'git 调用失败，无法判断该目录的追踪状态'
            }
            elseif (@($tracked.Lines).Count -eq 0) {
                Write-Ok '主仓 git 未追踪该目录（符合预期：拆分无需处理历史）'
            }
            else {
                Write-Warn ("主仓 git 追踪了 " + @($tracked.Lines).Count + " 个文件，拆分前需要先评估历史处理")
            }
        }
    }
    else {
        Write-Warn ("源目录不存在：" + $SourcePath)
    }

    Write-Section 'staging（新仓库本地检出）'
    if (Test-Path -LiteralPath $TargetPath) {
        if (Test-GitRepo -Path $TargetPath) {
            $log = Get-GitOutput -Arguments @('log', '--oneline', '-3') -WorkDir $TargetPath
            Write-Ok ("已是 git 仓库：" + $TargetPath)
            foreach ($line in $log.Lines) { Write-Info $line }
            $rem = Get-GitOutput -Arguments @('remote', '-v') -WorkDir $TargetPath
            if (@($rem.Lines).Count -gt 0) { foreach ($line in $rem.Lines) { Write-Info $line } }
            else { Write-Info '尚未配置 remote' }
        }
        else {
            Write-Warn ("已存在但不是 git 仓库：" + $TargetPath)
        }
    }
    else {
        Write-Info ("尚未创建：" + $TargetPath)
    }

    Write-Section '远端仓库'
    $heads = Get-RemoteHeads -Url $RepoUrl
    if ($heads.Code -ne 0) {
        Write-Warn ("访问不到（多半是还没在网页上创建）：" + $RepoUrl)
    }
    elseif (@($heads.Lines).Count -eq 0) {
        Write-Ok ("仓库存在且为空：" + $RepoUrl)
    }
    else {
        Write-Ok ("仓库存在，已有分支：")
        foreach ($line in $heads.Lines) { Write-Info $line }
    }

    Write-Section '主仓 submodule 现状'
    $gitmodules = Join-Path $MainRepoPath '.gitmodules'
    if (Test-Path -LiteralPath $gitmodules) {
        Write-Info '.gitmodules 已存在：'
        foreach ($line in (Get-Content -LiteralPath $gitmodules)) { Write-Info $line }
    }
    else {
        Write-Ok '.gitmodules 不存在（尚未 submodule 化）'
    }
    $subState = Get-GitOutput -Arguments @('submodule', 'status') -WorkDir $MainRepoPath
    if ($subState.Code -eq 0 -and @($subState.Lines).Count -gt 0) {
        foreach ($line in $subState.Lines) { Write-Info $line }
    }

    Write-Section '.git/info/exclude 中的相关行'
    $excludePath = Join-Path $MainRepoPath '.git\info\exclude'
    if (Test-Path -LiteralPath $excludePath) {
        $hits = @(Get-Content -LiteralPath $excludePath | Where-Object { $_.Trim() -like 'frontends/WinUI3*' })
        if ($hits.Count -gt 0) {
            Write-Info 'Link 阶段会移除以下几行：'
            foreach ($line in $hits) { Write-Info ("  " + $line) }
        }
        else {
            Write-Ok '没有 frontends/WinUI3 相关排除行'
        }
    }
    else {
        Write-Warn '.git/info/exclude 不存在'
    }

    Write-Section '补充'
    $dotnet = @{ Code = -1; Lines = @() }
    if ($DotNetExe) { $dotnet = Get-NativeOutput -Exe $DotNetExe -Arguments @('--version') }
    if ($dotnet.Code -eq 0) { Write-Ok ("dotnet " + (@($dotnet.Lines) -join ' ')) }
    else { Write-Warn '找不到 dotnet，Build 阶段不可用' }
}

# ---------------------------------------------------------------- Export

function Invoke-ExportStage {
    Write-Head '阶段 2/5  Export  导出干净源码树并做首次提交'

    if (-not (Test-Path -LiteralPath (Join-Path $SourcePath 'SekaiSync.Desktop.csproj'))) {
        throw ("源目录里找不到 SekaiSync.Desktop.csproj：" + $SourcePath)
    }
    if (-not (Test-Path -LiteralPath (Join-Path $SeedDir 'README.md'))) {
        throw ("种子目录不完整：" + $SeedDir)
    }

    if ((Test-Path -LiteralPath $TargetPath) -and -not $Force) {
        $existing = @(Get-ChildItem -LiteralPath $TargetPath -Force)
        if ($existing.Count -gt 0) {
            throw ("目标目录已存在且非空，拒绝覆盖：" + $TargetPath + "`n         确认要合并进去的话，加 -Force 重跑。")
        }
    }

    Write-Section '1) 复制源码（剔除 bin / obj / .vs）'
    if (-not (Test-Path -LiteralPath $TargetPath)) {
        Invoke-CmdletMutation -Name 'New-Item' -Arguments @('-ItemType', 'Directory', '-Force', '-Path', $TargetPath)
    }
    if (-not $RobocopyExe) { throw '找不到 robocopy，无法导出源码树。' }
    $rcArgs = @($SourcePath, $TargetPath, '/E', '/XD', 'bin', 'obj', '.vs', '/XF', '*.user', '/NFL', '/NDL', '/NJH', '/NJS', '/NP')
    Show-Cmd -Exe $RobocopyExe -Arguments $rcArgs
    if (-not $DryRun) {
        & $RobocopyExe @rcArgs | Out-Null
        # robocopy 的退出码 0-7 都算成功，>=8 才是错误
        if ($LASTEXITCODE -ge 8) { throw ("robocopy 失败（exit " + $LASTEXITCODE + "）") }
        Write-Ok '源码已复制'
    }

    Write-Section '2) 套用 seed 骨架文件（会覆盖同名的 README.md）'
    if (-not $DryRun) {
        Get-ChildItem -LiteralPath $SeedDir -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $TargetPath -Recurse -Force
            Write-Info ("+ " + $_.Name)
        }
        Write-Ok '骨架文件已就位'
    }
    else {
        Get-ChildItem -LiteralPath $SeedDir -Force | ForEach-Object { Write-Info ("+ " + $_.Name) }
    }

    Write-Section '3) 初始化仓库并首次提交'
    if (-not (Test-GitRepo -Path $TargetPath)) {
        Invoke-Mutation -Exe $GitExe -Arguments @('init', '-b', 'main') -WorkDir $TargetPath
    }
    else {
        Write-Info '已是 git 仓库，跳过 init'
    }

    # 提交身份统一走全局配置。GitHub 勾选 keep-my-email-private 后会下发
    # <数字ID>+<用户名>@users.noreply.github.com，用它署名提交才会归属到账号，
    # 所以新仓库不写任何 local 覆盖，直接继承全局。
    # 若存在遗留的 local 覆盖（例如早先误设），这里主动清掉，保证结果确定、可重复。
    $globalName = @(Get-GitOutput -Arguments @('config', '--global', 'user.name') -WorkDir $TargetPath).Lines
    $globalMail = @(Get-GitOutput -Arguments @('config', '--global', 'user.email') -WorkDir $TargetPath).Lines
    if ($globalName.Count -eq 0 -or $globalMail.Count -eq 0) {
        throw '全局 git 身份未配置。先执行：git config --global user.name <GitHub 用户名> 与 git config --global user.email <GitHub noreply 邮箱>'
    }
    Write-Info ("提交身份（全局）：" + $globalName[0] + " <" + $globalMail[0] + ">")
    if ($globalMail[0] -notmatch '@users\.noreply\.github\.com$') {
        Write-Warn '全局邮箱不是 GitHub noreply 形式，推送后提交可能无法归属到账号'
    }

    $localName = @(Get-GitOutput -Arguments @('config', '--local', 'user.name') -WorkDir $TargetPath).Lines
    $localMail = @(Get-GitOutput -Arguments @('config', '--local', 'user.email') -WorkDir $TargetPath).Lines
    if ($localName.Count -gt 0) {
        Write-Warn ('清除新仓库遗留的 local 覆盖 user.name = ' + $localName[0])
        Invoke-Mutation -Exe $GitExe -Arguments @('config', '--local', '--unset', 'user.name') -WorkDir $TargetPath
    }
    if ($localMail.Count -gt 0) {
        Write-Warn ('清除新仓库遗留的 local 覆盖 user.email = ' + $localMail[0])
        Invoke-Mutation -Exe $GitExe -Arguments @('config', '--local', '--unset', 'user.email') -WorkDir $TargetPath
    }

    Invoke-Mutation -Exe $GitExe -Arguments @('add', '-A') -WorkDir $TargetPath
    Invoke-Mutation -Exe $GitExe -Arguments @(
        'commit',
        '-m', 'chore: initial import of SekaiSync Desktop',
        '-m', 'Exported from the SekaiSync monorepo path frontends/WinUI3. Build artifacts (bin/obj) are excluded and reproduced by dotnet build.'
    ) -WorkDir $TargetPath

    Write-Section '4) 结果'
    if (-not $DryRun) {
        $count = @(Get-GitOutput -Arguments @('ls-files') -WorkDir $TargetPath).Lines.Count
        $log = Get-GitOutput -Arguments @('log', '--oneline', '-1') -WorkDir $TargetPath
        Write-Ok ("本地仓库就绪，追踪 " + $count + " 个文件")
        foreach ($line in $log.Lines) { Write-Info $line }
        Write-Host ''
        Write-Info '下一步：确认远端仓库已在网页上创建好，然后跑  -Stage Publish'
    }
}

# ---------------------------------------------------------------- Build

function Invoke-BuildStage {
    Write-Head '阶段 3/5  Build  验证导出树能独立编译'

    if (-not (Test-Path -LiteralPath (Join-Path $TargetPath 'SekaiSync.Desktop.csproj'))) {
        throw ("目标目录里找不到项目文件，先跑 Export：" + $TargetPath)
    }

    Write-Info ("工作目录：" + $TargetPath)
    Invoke-Mutation -Exe $DotNetExe -Arguments @('build', 'SekaiSync.Desktop.csproj', '-p:Platform=x64') -WorkDir $TargetPath
    if (-not $DryRun) { Write-Ok '编译通过' }
}

# ---------------------------------------------------------------- Publish

function Invoke-PublishStage {
    Write-Head '阶段 4/5  Publish  把本地仓库推到远端'

    if (-not (Test-GitRepo -Path $TargetPath)) {
        throw ("目标目录不是 git 仓库，先跑 Export：" + $TargetPath)
    }
    $log = Get-GitOutput -Arguments @('log', '--oneline', '-1') -WorkDir $TargetPath
    if (@($log.Lines).Count -eq 0 -or $log.Code -ne 0) {
        throw '本地仓库还没有提交，先跑 Export'
    }
    Write-Info ("待推送提交：" + (@($log.Lines) -join ' '))

    Write-Section '检查远端仓库'
    $heads = Get-RemoteHeads -Url $RepoUrl
    if ($heads.Code -ne 0) {
        Write-Bad ("访问不到远端：" + $RepoUrl)
        Write-Host ''
        Write-Info '请先在浏览器里创建空仓库（不要勾选 README / .gitignore / license 初始化）：'
        Write-Info '  https://github.com/new'
        Write-Info ("  Owner: omoinoki    Repository name: " + [System.IO.Path]::GetFileNameWithoutExtension($RepoUrl))
        Write-Info '  Visibility: Public'
        Write-Info '建好后重跑本阶段。'
        throw '远端仓库不存在，已停止'
    }
    if (@($heads.Lines).Count -eq 0) {
        Write-Ok '远端为空仓库，可以推送'
    }
    else {
        Write-Warn '远端已有分支，推送可能被拒：'
        foreach ($line in $heads.Lines) { Write-Info $line }
        Write-Info '如果远端内容不是你想要的，先在网页上清空或删除重建。'
    }

    Write-Section '配置 origin'
    $rem = Get-GitOutput -Arguments @('remote') -WorkDir $TargetPath
    if (@($rem.Lines) -contains 'origin') {
        Invoke-Mutation -Exe $GitExe -Arguments @('remote', 'set-url', 'origin', $RepoUrl) -WorkDir $TargetPath
    }
    else {
        Invoke-Mutation -Exe $GitExe -Arguments @('remote', 'add', 'origin', $RepoUrl) -WorkDir $TargetPath
    }

    Write-Section '推送'
    Invoke-Mutation -Exe $GitExe -Arguments @('push', '-u', 'origin', 'main') -WorkDir $TargetPath
    if (-not $DryRun) {
        Write-Ok '已推送'
        Write-Info ("仓库地址：" + ($RepoUrl -replace '\.git$', ''))
        Write-Host ''
        Write-Info '下一步：-Stage Link，把主仓接到这个仓库上'
    }
}

# ---------------------------------------------------------------- Link

function Invoke-LinkStage {
    Write-Head '阶段 5/5  Link  主仓改用 submodule 指向新仓库'

    if (-not (Test-GitRepo -Path $MainRepoPath)) {
        throw ("主仓不是 git 仓库：" + $MainRepoPath)
    }

    Write-Section '前置检查 1/3：远端必须已经有 main'
    $heads = Get-RemoteHeads -Url $RepoUrl
    if ($heads.Code -ne 0 -or @($heads.Lines).Count -eq 0) {
        throw ('远端还没有 main 分支，先跑 Publish：' + $RepoUrl)
    }
    Write-Ok '远端 main 存在'

    Write-Section '前置检查 2/3：主仓已追踪文件必须干净'
    $dirty = Get-GitOutput -Arguments @('status', '--porcelain', '--untracked-files=no') -WorkDir $MainRepoPath
    if (@($dirty.Lines).Count -gt 0) {
        Write-Warn '主仓有未提交的已追踪改动：'
        foreach ($line in $dirty.Lines) { Write-Info $line }
        throw '请先提交或 stash 主仓的改动，再跑 Link'
    }
    Write-Ok '主仓已追踪文件无未提交改动'

    # 这一项必须排在移走目录之前，否则检查失败时目录已经动了
    Write-Section '前置检查 3/3：.gitmodules 不能已存在'
    $gm = Join-Path $MainRepoPath '.gitmodules'
    if (Test-Path -LiteralPath $gm) {
        Write-Warn '.gitmodules 已存在，内容如下：'
        foreach ($line in (Get-Content -LiteralPath $gm)) { Write-Info $line }
        throw '.gitmodules 已存在，请先确认或移除后再跑 Link'
    }
    Write-Ok '.gitmodules 不存在'

    $backupBase = $BackupRoot
    if (-not $backupBase) { $backupBase = Split-Path -Parent $MainRepoPath }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupPath = Join-Path $backupBase ('sekaisync-winui3-backup-' + $stamp)
    $sourceFull = Join-Path (Join-Path $MainRepoPath 'frontends') 'WinUI3'

    Write-Section '1) 备份并移走原 frontends/WinUI3'
    if (Test-Path -LiteralPath $sourceFull) {
        Write-Info ("原目录：" + $sourceFull)
        Write-Info ("备份到：" + $backupPath)
        Invoke-CmdletMutation -Name 'Move-Item' -Arguments @('-LiteralPath', $sourceFull, '-Destination', $backupPath)
        if (-not $DryRun) { Write-Ok '已移走（原目录未删除，确认无误后可自行清理备份）' }
    }
    else {
        Write-Info '原目录不存在，跳过'
    }

    Write-Section '2) 从 .git/info/exclude 摘掉 frontends/WinUI3 相关行'
    $excludePath = Join-Path $MainRepoPath '.git\info\exclude'
    if (Test-Path -LiteralPath $excludePath) {
        $all = @(Get-Content -LiteralPath $excludePath)
        $kept = @($all | Where-Object { $_.Trim() -notlike 'frontends/WinUI3*' })
        $removed = $all.Count - $kept.Count
        if ($removed -gt 0) {
            foreach ($line in ($all | Where-Object { $_.Trim() -like 'frontends/WinUI3*' })) {
                Write-Info ("- " + $line)
            }
            if (-not $DryRun) {
                [System.IO.File]::WriteAllLines($excludePath, [string[]]$kept, $Utf8NoBom)
                Write-Ok ("已移除 " + $removed + " 行")
            }
        }
        else {
            Write-Info '没有需要移除的行'
        }
    }

    Write-Section '3) 添加 submodule'
    Invoke-Mutation -Exe $GitExe -Arguments @('submodule', 'add', $RepoUrl, $SubmodulePath) -WorkDir $MainRepoPath

    Write-Section '4) 本地提交'
    Invoke-Mutation -Exe $GitExe -Arguments @(
        'commit',
        '-m', 'chore: point frontends/WinUI3 at the sekaisync-winui3 repository',
        '-m', 'The desktop front-end now lives in its own repository and is vendored here as a submodule. Clone with --recurse-submodules, or run git submodule update --init after a plain clone.'
    ) -WorkDir $MainRepoPath

    if (-not $DryRun) {
        Write-Ok '主仓已 submodule 化（尚未推送）'
        Write-Host ''
        Write-Info '收尾三件事：'
        Write-Info '  1. 检查改动的文档引用（docs/ONBOARDING.md、docs/ARCHITECTURE.md、HANDOFF.md 里提到 frontends/WinUI3 的地方）'
        Write-Info '  2. 确认无误后推送主仓：git push origin main'
        Write-Info '  3. 在 GitHub 上确认 frontends/WinUI3 已显示为指向新仓库的链接'
        Write-Info ("  4. 备份确认无用后再删除：" + $backupPath)
    }
}

# ---------------------------------------------------------------- 分发

Write-Head 'SekaiSync Desktop 拆仓准备工具'
Write-Info ("阶段         ：" + $Stage)
Write-Info ("源目录       ：" + $SourcePath)
Write-Info ("目标目录     ：" + $TargetPath)
Write-Info ("远端地址     ：" + $RepoUrl)
Write-Info ("主仓         ：" + $MainRepoPath)
if ($DryRun) { Write-Warn 'DryRun 模式：只打印，不执行' }

switch ($Stage) {
    'Status' { Invoke-StatusStage }
    'Export' { Invoke-ExportStage }
    'Build' { Invoke-BuildStage }
    'Publish' { Invoke-PublishStage }
    'Link' { Invoke-LinkStage }
}

Write-Host ''
Write-Host ('阶段 ' + $Stage + ' 结束') -ForegroundColor Cyan
