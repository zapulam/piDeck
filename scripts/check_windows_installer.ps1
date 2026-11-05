#Requires -Version 5.1
<# Exercise install, upgrade, shortcuts, startup, native launch, and uninstall on a disposable CI VM. #>
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string] $Installer)
$ErrorActionPreference = 'Stop'
if ($env:CI -ne 'true' -or -not $env:RUNNER_TEMP) { throw 'Run this check only on a disposable CI runner.' }
$checkRoot = Join-Path $env:RUNNER_TEMP 'pideck-install-check'
$target = [IO.Path]::GetFullPath((Join-Path $checkRoot 'app'))
$allowedRoot = [IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\') + '\'
if (-not $target.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Installer test target is outside the runner workspace.' }
New-Item -ItemType Directory -Path $checkRoot -Force | Out-Null
$arguments = @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',('/DIR="' + $target + '"'),'/TASKS=startup')
$setup = Start-Process -FilePath (Resolve-Path $Installer).Path -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
if ($setup.ExitCode -ne 0) { throw "Installer failed: $($setup.ExitCode)" }
$executable = Join-Path $target 'piDeck.exe'
$shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'piDeck.lnk'
$link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
if ($link.TargetPath -ne $executable) { throw 'Start menu shortcut does not launch the packaged executable.' }
$shell = New-Object -ComObject Shell.Application
$item = $shell.NameSpace((Split-Path $shortcut)).ParseName((Split-Path $shortcut -Leaf))
if ($item.ExtendedProperty('System.AppUserModel.ID') -ne 'piDeck.Desktop') { throw 'Shortcut application identity is missing.' }
$startupKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
if ((Get-ItemProperty $startupKey).piDeck -notlike '*piDeck.exe*') { throw 'Start-at-login registration is missing.' }
$report = Join-Path $checkRoot 'native.json'
$native = Start-Process -FilePath $executable -ArgumentList @('--smoke-window','--report',('"' + $report + '"')) -WindowStyle Hidden -PassThru
if (-not $native.WaitForExit(90000)) { $native.Kill(); throw 'Installed native window timed out.' }
if (-not (Test-Path $report) -or -not (Get-Content $report -Raw | ConvertFrom-Json).ok) { throw 'Installed native window failed authentication.' }
$profile = Join-Path $env:LOCALAPPDATA 'piDeck'
New-Item -ItemType Directory -Path $profile -Force | Out-Null
$retained = Join-Path $profile 'installer-retention-check.txt'
'Keep this user configuration across upgrades and uninstall.' | Set-Content -LiteralPath $retained
$upgrade = Start-Process -FilePath (Resolve-Path $Installer).Path -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
if ($upgrade.ExitCode -ne 0 -or -not (Test-Path $retained)) { throw 'Upgrade did not preserve user configuration.' }
$uninstaller = Join-Path $target 'unins000.exe'
$remove = Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART') -WindowStyle Hidden -Wait -PassThru
if ($remove.ExitCode -ne 0) { throw 'Uninstall failed.' }
if (Test-Path $shortcut) { throw 'Uninstall left its Start menu shortcut.' }
if ((Get-ItemProperty $startupKey -ErrorAction SilentlyContinue).piDeck) { throw 'Uninstall left its startup registration.' }
if (-not (Test-Path $retained)) { throw 'Uninstall removed user configuration.' }
Write-Output 'Installer lifecycle passed: shortcut identity, startup, native authentication, upgrade retention, and uninstall.'
