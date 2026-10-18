"""Windows command entry point and cleanup after the bundled process exits."""
import base64
from pathlib import Path

from . import platforms


def purge_receipt(command):
    # Keep recovery outside the directory being removed. A failed recursive
    # delete may already have removed the executable and installation record.
    return Path(str(command) + '.purge.json')


PURGE_WAIT_SECONDS = 30

_CLEANUP = r'''
$ErrorActionPreference = 'Stop'
$installRoot = $env:MEDIA_IMPORTER_PURGE_HOME
$command = $env:MEDIA_IMPORTER_COMMAND_WRAPPER
$receipt = $command + '.purge.json'
try {
    $item = Get-Item -LiteralPath $receipt -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Cleanup receipt was replaced.'
    }
    $approved = Get-Content -LiteralPath $receipt -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($approved.home -ine $installRoot -or $approved.command -ine $command) {
        throw 'Cleanup receipt does not match this installation.'
    }
    foreach ($path in @($approved.directories) + @($approved.env)) {
        if (Test-Path -LiteralPath $path) {
            throw 'Registered local data still exists; installation cleanup was not started.'
        }
    }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $reportedWait = $false
    while (Test-Path -LiteralPath $installRoot) {
        $rootItem = Get-Item -LiteralPath $installRoot -Force
        if (-not $rootItem.PSIsContainer -or ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Installation directory was replaced.'
        }
        try {
            Remove-Item -LiteralPath $installRoot -Recurse -Force -ErrorAction Stop
        } catch {
            if (-not $reportedWait) {
                [Console]::Out.WriteLine('Waiting for installation files to become available...')
                $reportedWait = $true
            }
            if ($timer.Elapsed.TotalSeconds -ge __WAIT_SECONDS__) {
                throw ('Installation directory is still in use or inaccessible: ' + $installRoot)
            }
            Start-Sleep -Milliseconds 250
        }
    }
    Remove-Item -LiteralPath $receipt -Force -ErrorAction Stop
} catch {
    [Console]::Error.WriteLine('Native uninstall incomplete: ' + $_.Exception.Message)
    [Console]::Error.WriteLine('Close processes using the installation, then retry: "' + $command + '" uninstall --purge --yes')
    exit 1
}
exit 0
'''


def launcher(home, executable):
    """No cleanup executable or script may depend on the installation surviving."""
    label = platforms.label(home)
    home = str(home).replace('%', '%%')
    executable = str(executable).replace('%', '%%')
    cleanup = _CLEANUP.replace('__WAIT_SECONDS__', str(PURGE_WAIT_SECONDS))
    encoded = base64.b64encode(cleanup.encode('utf-16-le')).decode('ascii')
    # Do not delete the active .cmd until cmd.exe has left its batch context.
    # PowerShell is built into supported Windows versions; no Python install or
    # additional cleanup program is needed, even after a partial removal.
    return ('@echo off\nsetlocal DisableDelayedExpansion\n'
            'rem media-importer managed launcher ' + label + '\n'
            'set "MEDIA_IMPORTER_PURGE_HOME=' + home + '"\n'
            'set "MEDIA_IMPORTER_PURGE_WRAPPER=' + label + '"\n'
            'set "MEDIA_IMPORTER_COMMAND_WRAPPER=%~f0"\n'
            'if exist "%~f0.purge.json" goto pending_purge\n'
            '"' + executable + '" --home "' + home + '" %*\n'
            'if errorlevel 1 exit /b %errorlevel%\n'
            'if exist "%~f0.purge.json" goto purge\n'
            'if exist "' + home + '\\installation.json" exit /b 0\n'
            'goto remove_command\n'
            ':pending_purge\n'
            'if not "%~1"=="uninstall" goto pending_help\n'
            'if not "%~4"=="" goto pending_help\n'
            'if "%~2 %~3"=="--purge --yes" goto purge\n'
            'if "%~2 %~3"=="--yes --purge" goto purge\n'
            ':pending_help\n'
            'echo Native uninstall is pending. Retry: "%~f0" uninstall --purge --yes 1>&2\n'
            'exit /b 1\n'
            ':purge\n'
            'cd /d "%TEMP%"\n'
            'powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand ' + encoded + '\n'
            'if errorlevel 1 exit /b %errorlevel%\n'
            'echo Native uninstall complete.\n'
            ':remove_command\n'
            '(goto) 2>nul & del "%~f0" & exit /b 0\n')
