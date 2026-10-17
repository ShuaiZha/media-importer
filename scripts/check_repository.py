"""Reject runtime data and obvious credential material from the tracked source tree."""
import re
import subprocess
from pathlib import Path, PurePosixPath


def check(path, content):
    parts = PurePosixPath(path).parts
    name = parts[-1]
    if (parts[0] in ('data', 'cache', 'backups', 'secrets', 'credentials', 'work', '.build') or
        parts[0] == 'config' and path != 'config/settings.example.json' or
        name.startswith('.env') and name != '.env.example' or
        name.startswith('._') or name == '.DS_Store' or
        re.search(r'\.(sqlite3?|db)(-wal|-shm)?$|\.(mp4|mov|lrv|thm|bin|download|key|pem|p12)$', name, re.I)):
        return '运行数据或敏感文件路径'
    if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----', content):
        return '私钥内容'
    if re.search(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{60,})', content):
        return '访问令牌'
    return None


def main():
    root = Path(__file__).resolve().parents[1]
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
    problems = [(path, reason) for path in paths if path and
                (reason := check(path, (root / path).read_bytes()))]
    for path, reason in problems:
        print(f'{path}: {reason}')  # Never print matched secret values.
    print(f'源文件检查：{len(problems)} 个问题；此检查不替代人工凭据复核。')
    return bool(problems)


if __name__ == '__main__':
    raise SystemExit(main())
