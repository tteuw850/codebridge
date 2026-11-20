param(
  [string]$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
  [string]$ToolsRoot = (Join-Path $env:LOCALAPPDATA 'Programs\CodeBridge\tools'),
  [string]$OutputFolder = '',
  [string]$NSISCompiler = ''
)
$ErrorActionPreference = 'Stop'
if (-not $OutputFolder) { $OutputFolder = Join-Path $SourceRoot 'dist' }
if (-not $NSISCompiler) {
  $NSISCompiler = @('C:\Program Files (x86)\NSIS\makensis.exe', 'C:\Program Files\NSIS\makensis.exe') |
    Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $NSISCompiler -or -not (Test-Path -LiteralPath $NSISCompiler)) {
  throw 'Instale NSIS 3 ou especifique -NSISCompiler.'
}
foreach ($name in @('cloudflared.exe', 'tunnel-client.exe')) {
  $path = Join-Path $ToolsRoot $name
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Ferramenta ausente: $path" }
}
New-Item -ItemType Directory -Force -Path $OutputFolder | Out-Null
$output = Join-Path $OutputFolder 'CodeBridge-Setup-2.0.12-prealpha.exe'
$argsList = @(
  '/V2',
  ('/DSOURCE_ROOT=' + $SourceRoot),
  ('/DTOOLS_ROOT=' + $ToolsRoot),
  ('/DOUTPUT_FILE=' + $output),
  (Join-Path $PSScriptRoot 'codebridge.nsi')
)
& $NSISCompiler @argsList
if ($LASTEXITCODE -ne 0) { throw "Falha na compilacao NSIS: $LASTEXITCODE" }
if (-not (Test-Path -LiteralPath $output)) { throw 'O executavel nao foi criado.' }
$hash = (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant()
$hashFile = Join-Path $OutputFolder 'CodeBridge-Setup-2.0.12-prealpha.sha256'
[IO.File]::WriteAllText($hashFile, ($hash + '  CodeBridge-Setup-2.0.12-prealpha.exe' + [Environment]::NewLine), [Text.UTF8Encoding]::new($false))
Write-Output ('SETUP=' + $output)
Write-Output ('SIZE_BYTES=' + (Get-Item -LiteralPath $output).Length)
Write-Output ('SHA256=' + $hash)
