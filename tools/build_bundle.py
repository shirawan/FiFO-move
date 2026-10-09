#!/usr/bin/env python3
"""Create complete release ZIPs from a clean immutable Git commit."""
import ast
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tarfile
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

repo = Path(__file__).resolve().parents[1]
if subprocess.check_output(['git', 'status', '--porcelain'], cwd=repo).strip():
    raise SystemExit('Commit reviewed changes before bundling; the checkout must be clean.')
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
output = Path(sys.argv[1] if len(sys.argv) > 1 else '/workspace/artifacts')
output.mkdir(parents=True, exist_ok=True)
modules = ['company_financial_cutover', 'company_purchase_cutover', 'company_stock_fifo_migration']
common = ['README.md', 'INSTALL.txt', 'tools']

def build(name, targets):
    tar_bytes = subprocess.check_output(['git', 'archive', commit, *targets], cwd=repo)
    archive_path = output / name
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as source, ZipFile(archive_path, 'w', ZIP_DEFLATED) as result:
        for member in source.getmembers():
            if not member.isfile():
                continue
            assert '__pycache__' not in member.name and not member.name.endswith('.pyc')
            info = ZipInfo(member.name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = (member.mode & 0o777) << 16
            result.writestr(info, source.extractfile(member).read())
        info = ZipInfo('BUILD.txt', (2026, 1, 1, 0, 0, 0))
        result.writestr(info, 'Source commit: ' + commit + '\n')
    with ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        assert {'README.md', 'INSTALL.txt', 'tools/runtime.sh', 'tools/test_financial_only.sh'} <= names
        for target in targets:
            if target in modules:
                assert target + '/__manifest__.py' in names
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    print(digest + '  ' + archive_path.name)
    return digest + '  ' + archive_path.name

checksums = [build('FiFO-move-odoo19-' + commit[:7] + '.zip', modules + common + ['tests'])]
for module in modules[:2]:
    version = ast.literal_eval((repo / module / '__manifest__.py').read_text())['version']
    checksums.append(build(module + '-' + version + '.zip', [module] + common))
(output / ('SHA256-' + commit[:7] + '.txt')).write_text('\n'.join(checksums) + '\n')
