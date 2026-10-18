param([string]$Project, [string]$BinDir)
$ErrorActionPreference = 'Stop'
$Platform = 'windows-amd64'
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw '此脚本目前仅提供 Windows x64 安装包；其他架构请使用 Docker。' }
$Base = 'https://github.com/ShuaiZha/media-importer/releases'
$Temporary = Join-Path ([IO.Path]::GetTempPath()) ('media-importer-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Temporary | Out-Null
try {
    $Manifest = Invoke-RestMethod "$Base/latest/download/native-release.json"
    if ($Manifest.format -ne 1 -or $Manifest.version -notmatch '^\d+\.\d+\.\d+$') { throw '发行清单无效。' }
    $Asset = $Manifest.assets.$Platform
    $Name = "media-importer-$($Manifest.version)-$Platform.tar.gz"
    if ($Asset.name -ne $Name -or $Asset.sha256 -notmatch '^[0-9a-f]{64}$') { throw '此版本没有有效的 Windows 原生安装包。' }
    $Archive = Join-Path $Temporary 'package.tar.gz'
    Invoke-WebRequest "$Base/download/v$($Manifest.version)/$Name" -OutFile $Archive
    if ((Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLower() -ne $Asset.sha256) { throw '安装包校验失败，未安装。' }
    tar -xzf $Archive -C $Temporary
    if ($LASTEXITCODE -ne 0) { throw '安装包解压失败。' }
    $Arguments = @('install')
    if ($Project) { $Arguments += @('--project', $Project) }
    if ($BinDir) { $Arguments += @('--bin-dir', $BinDir) }
    & (Join-Path $Temporary 'media-importer/media-importer.exe') @Arguments
    if ($LASTEXITCODE -ne 0) { throw '安装未完成，请按程序提示处理；已有数据保留。' }
} finally {
    Remove-Item -Recurse -Force $Temporary
}
