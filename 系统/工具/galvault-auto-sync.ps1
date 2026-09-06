[CmdletBinding()]
param(
    [string]$Message,
    [switch]$DryRun,
    [switch]$Quiet
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$vaultRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$expectedRemote = "git@github.com:wandererpg/GalVault.git"
$env:GIT_SSH_COMMAND = "ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=2"
$logDirectory = Join-Path $env:LOCALAPPDATA "GalVault"
$logPath = Join-Path $logDirectory "auto-sync.log"
$mutex = New-Object System.Threading.Mutex($false, "Local\GalVaultAutoSync")

function Write-Log {
    param([Parameter(Mandatory)][string]$Text)

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    Add-Content -LiteralPath $logPath -Value ("{0} {1}" -f (Get-Date -Format o), $Text) -Encoding UTF8
    if (-not $Quiet) {
        Write-Output $Text
    }
}

function Invoke-GitChecked {
    param([Parameter(Mandatory)][string[]]$Arguments)

    # Native Git writes normal progress messages to stderr on Windows. Keep
    # stdout and stderr separate so a successful fetch/push or a line-ending
    # warning cannot be mistaken for a command result.
    $stderrPath = Join-Path ([IO.Path]::GetTempPath()) ("galvault-git-{0}.err" -f [guid]::NewGuid().ToString("N"))
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell promotes native stderr to NativeCommandError when
        # ErrorActionPreference is Stop, even if stderr is redirected.
        $ErrorActionPreference = "Continue"
        $output = & git -C $vaultRoot @Arguments 2> $stderrPath | ForEach-Object { [string]$_ }
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            $stdoutText = ($output | Out-String).Trim()
            $stderrText = if (Test-Path -LiteralPath $stderrPath) {
                (Get-Content -LiteralPath $stderrPath -Raw).Trim()
            }
            else {
                ""
            }
            $details = (@($stdoutText, $stderrText) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join "`n"
        }
        if ($exitCode -ne 0) {
            if ([string]::IsNullOrWhiteSpace($details)) {
                $details = "git exited with code $exitCode"
            }
            throw "git $($Arguments -join ' ') failed: $details"
        }
        return $output
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function Get-GitValue {
    param([Parameter(Mandatory)][string[]]$Arguments)

    $value = Invoke-GitChecked -Arguments $Arguments | Select-Object -First 1
    return ([string]$value).Trim()
}

function Assert-RemoteIsSafe {
    $remote = Get-GitValue -Arguments @("remote", "get-url", "origin")
    if ($remote -ne $expectedRemote) {
        throw "origin must be the expected SSH remote ($expectedRemote), but is $remote"
    }
}

function Assert-RemoteIsNotAhead {
    $remoteRef = & git -C $vaultRoot rev-parse --verify --quiet "origin/main" 2>$null
    if ($LASTEXITCODE -ne 0) {
        return
    }

    $counts = (Invoke-GitChecked -Arguments @("rev-list", "--left-right", "--count", "HEAD...origin/main") | Select-Object -First 1)
    $parts = ([string]$counts).Trim() -split "\s+"
    if ($parts.Count -ne 2) {
        throw "Could not determine whether origin/main is ahead of the local branch"
    }

    $remoteOnly = [int]$parts[1]
    if ($remoteOnly -gt 0) {
        throw "origin/main contains $remoteOnly commit(s) not present locally; reconcile them manually before automatic upload"
    }
}

if (-not $mutex.WaitOne(0)) {
    Write-Log "Another automatic upload is already running; skipped."
    exit 0
}

try {
    $gitRoot = Get-GitValue -Arguments @("rev-parse", "--show-toplevel")
    if ([IO.Path]::GetFullPath($gitRoot) -ne [IO.Path]::GetFullPath($vaultRoot)) {
        throw "The vault is not an independent Git repository. Run the setup commands from the vault root first."
    }

    $branch = Get-GitValue -Arguments @("branch", "--show-current")
    if ($branch -ne "main") {
        throw "Automatic upload only operates on the main branch; current branch is $branch"
    }

    Assert-RemoteIsSafe

    # Query the index directly; unlike a working-tree diff this does not emit
    # line-ending advice for files edited since the previous commit.
    $unmerged = @(Invoke-GitChecked -Arguments @("ls-files", "--unmerged"))
    if ($unmerged.Count -gt 0) {
        throw "The repository has unresolved merge conflicts; automatic upload stopped."
    }

    $before = @(Invoke-GitChecked -Arguments @("status", "--porcelain=v1", "--untracked-files=all"))
    if ($before.Count -eq 0) {
        Write-Log "No changes to upload."
        exit 0
    }

    $null = Invoke-GitChecked -Arguments @("fetch", "--prune", "origin", "main")
    Assert-RemoteIsNotAhead

    if ($DryRun) {
        Write-Log ("Dry run; changes detected: {0}" -f $before.Count)
        exit 0
    }

    $null = Invoke-GitChecked -Arguments @("add", "--all")
    $staged = @(Invoke-GitChecked -Arguments @("diff", "--cached", "--name-only"))
    if ($staged.Count -eq 0) {
        Write-Log "No non-ignored changes to upload."
        exit 0
    }

    if ([string]::IsNullOrWhiteSpace($Message)) {
        $Message = "sync: update vault $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    }

    $null = Invoke-GitChecked -Arguments @("commit", "-m", $Message)
    $null = Invoke-GitChecked -Arguments @("fetch", "--prune", "origin", "main")
    Assert-RemoteIsNotAhead
    $null = Invoke-GitChecked -Arguments @("push", "--set-upstream", "origin", "main")
    Write-Log ("Uploaded commit {0} ({1} file(s))." -f (Get-GitValue -Arguments @("rev-parse", "--short", "HEAD")), $staged.Count)
}
catch {
    $message = $_.Exception.Message
    Write-Log "Automatic upload failed: $message"
    if (-not $Quiet) {
        Write-Error $message
    }
    exit 1
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
