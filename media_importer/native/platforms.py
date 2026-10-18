"""Per-user service registration; no administrator privileges or credentials."""
import hashlib
import getpass
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import xml.etree.ElementTree as ET


def default_home(platform=None):
    platform = platform or sys.platform
    if platform == 'darwin':
        return Path.home() / 'Library/Application Support/MediaImporter'
    if platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'MediaImporter'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'media-importer'


def label(home):
    return 'org.media-importer.' + hashlib.sha256(str(home).encode()).hexdigest()[:12]


def service_path(home, platform=None):
    platform = platform or sys.platform
    if platform == 'darwin':
        return Path.home() / 'Library/LaunchAgents' / (label(home) + '.plist')
    if platform == 'win32':
        return home / 'service.xml'
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'systemd/user' / (label(home) + '.service')


def definition(home, executable, platform=None):
    platform = platform or sys.platform
    args = [str(executable), '--home', str(home), 'run']
    if platform == 'darwin':
        return plistlib.dumps({'Label': label(home), 'ProgramArguments': args,
            'WorkingDirectory': str(home), 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 30, 'ExitTimeOut': 60,
            'StandardOutPath': str(home / 'logs/launcher.log'),
            'StandardErrorPath': str(home / 'logs/launcher.log')})
    if platform == 'win32':
        ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
        ET.register_namespace('', ns)
        def element(parent, name, text=None, **attrs):
            item = ET.SubElement(parent, '{' + ns + '}' + name, attrs)
            item.text = text
            return item
        task = ET.Element('{' + ns + '}Task', {'version': '1.2'})
        triggers = element(task, 'Triggers'); logon = element(triggers, 'LogonTrigger')
        element(logon, 'Enabled', 'true')
        user = (os.environ.get('USERDOMAIN', '') + '\\' if os.environ.get('USERDOMAIN') else '') + getpass.getuser()
        element(logon, 'UserId', user)
        principals = element(task, 'Principals'); principal = element(principals, 'Principal', id='User')
        element(principal, 'UserId', user)
        element(principal, 'LogonType', 'InteractiveToken'); element(principal, 'RunLevel', 'LeastPrivilege')
        settings = element(task, 'Settings')
        for key, value in {'MultipleInstancesPolicy': 'IgnoreNew', 'DisallowStartIfOnBatteries': 'false',
                           'StopIfGoingOnBatteries': 'false', 'ExecutionTimeLimit': 'PT0S'}.items():
            element(settings, key, value)
        restart = element(settings, 'RestartOnFailure')
        element(restart, 'Interval', 'PT1M'); element(restart, 'Count', '3')
        actions = element(task, 'Actions', Context='User'); command = element(actions, 'Exec')
        element(command, 'Command', str(executable))
        element(command, 'Arguments', subprocess.list2cmdline(args[1:]))
        element(command, 'WorkingDirectory', str(home))
        return ET.tostring(task, encoding='utf-16', xml_declaration=True)
    def quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    return ('[Unit]\nDescription=Media Importer\nAfter=network-online.target\n'
            '[Service]\nType=simple\nExecStart=' + ' '.join(map(quote, args)) + '\n'
            'WorkingDirectory=' + quote(home) + '\nRestart=on-failure\nRestartSec=30\nTimeoutStopSec=60\n'
            '[Install]\nWantedBy=default.target\n').encode()


def call(args, *, optional=False):
    try:
        result = subprocess.run(args, capture_output=True, timeout=55)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('后台服务命令不可用或超时，请检查当前用户的服务管理器') from None
    if result.returncode and not optional:
        raise RuntimeError('后台服务操作失败；查看原生部署说明和 logs/launcher.log，配置与数据已保留')
    return result.returncode == 0


def register(home, executable):
    path = service_path(home)
    content = definition(home, executable)
    if path.exists() and path.read_bytes() != content:
        # This is our deterministic, installation-specific service name.
        stop(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if sys.platform == 'darwin':
        call(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)])
    elif sys.platform == 'win32':
        call(['schtasks', '/Create', '/TN', label(home), '/XML', str(path), '/F'])
        call(['schtasks', '/Run', '/TN', label(home)])
    else:
        call(['systemctl', '--user', 'daemon-reload'])
        call(['systemctl', '--user', 'enable', '--now', path.name])


def stop(home):
    if not service_path(home).exists():
        return
    if sys.platform == 'darwin':
        call(['launchctl', 'bootout', f'gui/{os.getuid()}/{label(home)}'], optional=True)
    elif sys.platform == 'win32':
        call(['schtasks', '/End', '/TN', label(home)], optional=True)
    else:
        call(['systemctl', '--user', 'disable', '--now', service_path(home).name], optional=True)


def unregister(home):
    if not service_path(home).exists():
        return
    stop(home)
    if sys.platform == 'win32':
        call(['schtasks', '/Delete', '/TN', label(home), '/F'], optional=True)
    service_path(home).unlink(missing_ok=True)
    if sys.platform not in ('darwin', 'win32'):
        call(['systemctl', '--user', 'daemon-reload'])
