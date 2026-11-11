param([string]$DistDirectory = 'dist')
$ErrorActionPreference = 'Stop'
$distPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $DistDirectory))
if (-not $distPath.StartsWith($PSScriptRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw '输出目录必须位于 WordBook 项目内。'
}
$python = Join-Path $PSScriptRoot '.venv310\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw '请先运行项目虚拟环境安装依赖。'
}
$previousPath = $env:PATH
try {
    # DLL discovery must use Windows and this project, not unrelated native tools.
    $env:PATH = @((Join-Path $PSScriptRoot '.venv310\Scripts'),
                  (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
    & $python -m PyInstaller --noconfirm --clean --onedir --windowed --noupx --name WordBook `
        --distpath $distPath `
        --workpath (Join-Path $PSScriptRoot 'build') `
        --hidden-import keyring.backends.Windows `
        (Join-Path $PSScriptRoot 'main.py')
    if ($LASTEXITCODE -ne 0) { throw '打包失败。' }
    & $python (Join-Path $PSScriptRoot 'tools\audit_bundle.py') `
        (Join-Path $PSScriptRoot 'build\WordBook\Analysis-00.toc') `
        (Join-Path $PSScriptRoot 'evidence\bundle-audit.json')
    if ($LASTEXITCODE -ne 0) { throw '打包依赖来源审查失败。' }
    foreach ($name in @('README.md', '验收报告.md')) {
        $document = Join-Path $PSScriptRoot $name
        if (Test-Path -LiteralPath $document) {
            Copy-Item -LiteralPath $document -Destination (Join-Path $distPath 'WordBook') -Force
        }
    }
} finally {
    $env:PATH = $previousPath
}
Write-Host (Join-Path $distPath 'WordBook\WordBook.exe')
