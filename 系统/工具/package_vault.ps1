param(
    [string]$OutputName = "GalVault-knowledge-base-2026-09-04.zip"
)

$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$output = [IO.Path]::GetFullPath((Join-Path $root $OutputName))

if (-not $output.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Output path must stay inside the vault root."
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$stream = [IO.File]::Open($output, [IO.FileMode]::Create, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try {
    $archive = [IO.Compression.ZipArchive]::new(
        $stream,
        [IO.Compression.ZipArchiveMode]::Create,
        $false,
        [Text.Encoding]::UTF8
    )
    try {
        $files = Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
            $relative = [IO.Path]::GetRelativePath($root, $_.FullName).Replace('\', '/')
            $_.Extension -ne ".zip" -and
            $_.Extension -ne ".pyc" -and
            -not $relative.StartsWith(".obsidian/", [StringComparison]::OrdinalIgnoreCase) -and
            -not $relative.StartsWith("系统/工具/vendor/", [StringComparison]::OrdinalIgnoreCase) -and
            -not $relative.StartsWith("系统/工具/vendor_local/", [StringComparison]::OrdinalIgnoreCase) -and
            -not $relative.Contains("/__pycache__/")
        }

        $count = 0
        foreach ($file in $files) {
            $entryName = [IO.Path]::GetRelativePath($root, $file.FullName).Replace('\', '/')
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $archive,
                $file.FullName,
                $entryName,
                [IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
            $count++
        }
    }
    finally {
        $archive.Dispose()
    }
}
finally {
    $stream.Dispose()
}

$result = Get-Item -LiteralPath $output
[pscustomobject]@{
    Path = $result.FullName
    Entries = $count
    SizeMiB = [math]::Round($result.Length / 1MB, 1)
}
