"""Host-only project registry. Never enumerate or read repository contents."""
import contextlib
import fcntl
import json
import os
import pathlib
import stat
import re
import uuid

from configure import ROOT, RUNTIME, envfile, write


def validate_path(value):
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_dir():
        raise ValueError('Choose an existing directory on this host.')
    deployment = ROOT.resolve()
    if path == deployment or path in deployment.parents:
        raise ValueError('A project must not contain this deployment and its secrets.')
    forbidden = ['/dev', '/proc', '/sys', '/run', '/etc', '/var/run', '/var/lib/docker', '/var/lib/containerd']
    forbidden += [str(pathlib.Path.home() / name) for name in ('.ssh', '.aws', '.kube', '.docker', '.gnupg', '.codex')]
    for value in forbidden:
        unsafe = pathlib.Path(value).resolve()
        if path == unsafe or unsafe in path.parents or path in unsafe.parents:
            raise ValueError('System, runtime and credential directories cannot be registered as projects.')
    validate_tree(path)
    return str(path)


def validate_tree(path):
    # Metadata only. Symlink directories are not traversed; their host targets are
    # not exposed by a bind mount. Reject direct/symlinked sockets, FIFOs and devices.
    pending = [path]
    try:
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    mode = entry.stat(follow_symlinks=False).st_mode
                    if stat.S_ISDIR(mode):
                        pending.append(pathlib.Path(entry.path))
                    elif stat.S_ISLNK(mode):
                        try:
                            target = entry.stat(follow_symlinks=True).st_mode
                        except FileNotFoundError:
                            continue
                        if not (stat.S_ISDIR(target) or stat.S_ISREG(target)):
                            raise ValueError('Project contains a socket, FIFO or device. Remove it from the project before opening.')
                    elif not stat.S_ISREG(mode):
                        raise ValueError('Project contains a socket, FIFO or device. Remove it from the project before opening.')
    except OSError:
        raise ValueError('Cannot safely inspect project file types. Check local permissions and retry.') from None


def validate_access(access):
    if access not in ('read-only', 'read-write'):
        raise ValueError('Access must be read-only or read-write.')
    return access


@contextlib.contextmanager
def locked(name='projects', blocking=True, shared=False):
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    with (RUNTIME / (name + '.lock')).open('a') as handle:
        (RUNTIME / (name + '.lock')).chmod(0o600)
        fcntl.flock(handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if blocking else fcntl.LOCK_NB))
        yield


def _load():
    path = RUNTIME / 'projects.json'
    if path.exists():
        data = json.loads(path.read_text())
        if data.get('version') != 1 or not isinstance(data.get('projects'), list):
            raise ValueError('Invalid project registry. Restore runtime/projects.json from backup.')
        return data
    # Import the old profile once. Its history stays with this one project only.
    env = envfile()
    data = {'version': 1, 'selected': None, 'projects': []}
    legacy_path = env.get('REPO_PATH')
    if legacy_path:
        try:
            legacy_path = validate_path(legacy_path)
            project = {'id': uuid.uuid4().hex, 'name': pathlib.Path(legacy_path).name,
                       'path': legacy_path, 'access': validate_access(env.get('REPO_ACCESS', 'read-only')),
                       'legacy_home': 'home-private'}
            data['projects'].append(project)
            data['selected'] = project['id']
        except ValueError:
            pass  # An obsolete path must not prevent registering a new project.
    _save(data)
    return data


def _save(data):
    write(RUNTIME / 'projects.json', data, 0o600)


def list_projects():
    with locked():
        return _load()


def add_project(name, path, access='read-only'):
    name = name.strip()
    if not name or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise ValueError('Use a project name of 1–100 printable characters.')
    path = validate_path(path)
    validate_access(access)
    with locked():
        data = _load()
        if any(p['path'] == path for p in data['projects']):
            raise ValueError('This folder is already registered.')
        project = {'id': uuid.uuid4().hex, 'name': name, 'path': path, 'access': access}
        data['projects'].append(project)
        _save(data)
        return project


def get_project(project_id=None):
    with locked():
        data = _load()
        project_id = project_id or data['selected']
        project = next((p.copy() for p in data['projects'] if p['id'] == project_id), None)
        if not project:
            raise ValueError('Select a registered project in the project manager.')
        if not re.fullmatch(r'[0-9a-f]{32}', project['id']):
            raise ValueError('Invalid project ID.')
        # Revalidate on every open: a directory may have moved or become a symlink.
        if validate_path(project['path']) != project['path']:
            raise ValueError('Project path changed. Register its new location as a new project.')
        validate_access(project['access'])
        if project.get('legacy_home') not in (None, 'home-private'):
            raise ValueError('Invalid legacy home volume.')
        return project


def update_access(project_id, access):
    validate_access(access)
    with locked():
        data = _load()
        project = next((p for p in data['projects'] if p['id'] == project_id), None)
        if not project:
            raise ValueError('Unknown project.')
        project['access'] = access
        _save(data)


def select_project(project_id):
    with locked():
        data = _load()
        if not any(p['id'] == project_id for p in data['projects']):
            raise ValueError('Unknown project.')
        data['selected'] = project_id
        _save(data)


def home_volume(project):
    return project.get('legacy_home') or 'home-project-' + project['id']
