"""Download public, fixed-version build assets only. Never inspect .env/repository."""
import hashlib
import json
import pathlib
import platform
import subprocess

root=pathlib.Path(__file__).resolve().parents[1]
lockpath=root/'vendor/assets.lock.json'
lock=json.loads(lockpath.read_text())
machine=platform.machine().lower()
if machine not in ('arm64','aarch64') or lock.get('architecture') != 'arm64':
    raise SystemExit('This release contains checksums only for Linux arm64 artifacts.')
assets=lock.get('assets',{})
if set(assets) != {'kilo.vsix','code-server.tar.gz'}:
    raise SystemExit('Asset lock must contain exactly kilo.vsix and code-server.tar.gz.')
for name,item in assets.items():
    url=item.get('url','')
    expected=item.get('sha256','')
    if not url.startswith('https://github.com/') or len(expected) != 64:
        raise SystemExit('Invalid locked asset metadata: '+name)
    path=root/'vendor'/name
    if not path.exists():
        partial=path.with_suffix(path.suffix+'.partial')
        subprocess.run(['curl','--fail','--location','--proto','=https','--proto-redir','=https',
                        '--retry','3','--continue-at','-','--max-time','900',url,'--output',str(partial)],check=True)
        digest=hashlib.file_digest(partial.open('rb'),'sha256').hexdigest()
        if digest != expected:
            partial.unlink(missing_ok=True)
            raise SystemExit('Asset checksum mismatch: '+name)
        partial.replace(path)
    digest=hashlib.file_digest(path.open('rb'),'sha256').hexdigest()
    if digest != expected:raise SystemExit('Asset checksum mismatch: '+name)
print('Fixed-version Linux arm64 assets match the committed SHA-256 manifest.')
