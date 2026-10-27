#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/wheelhouse
.venv/bin/python -c 'import sys; assert sys.version_info[:2]==(3,12) and sys.platform=="linux", "Offline Docker build requires a Linux Python 3.12 virtualenv"'
.venv/bin/python -m pip download --only-binary=:all: --cache-dir data/pip-cache --dest data/wheelhouse -r requirements.lock 'setuptools==80.9.0' 'wheel==0.45.1'
.venv/bin/python - <<'PY'
from pathlib import Path
import hashlib, json, shutil
root=Path('data/image-build')
root.mkdir(exist_ok=True)
for name in ('requirements.lock','pyproject.toml'):
    shutil.copy2(name,root/name)
shutil.copytree('nbabot',root/'nbabot',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
shutil.copytree('data/wheelhouse',root/'wheels',dirs_exist_ok=True)
manifest={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'wheels').glob('*.whl')}
(root/'wheel-checksums.json').write_text(json.dumps(manifest,indent=2))
docker=Path('Dockerfile').read_text().replace('COPY requirements.lock pyproject.toml ./','COPY requirements.lock pyproject.toml ./\nCOPY wheels /wheels')
docker=docker.replace('python -m pip install --no-cache-dir -r requirements.lock',
    'python -m pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.lock')
docker=docker.replace('python -m pip install --no-cache-dir --no-deps .',
    'PIP_NO_INDEX=1 PIP_FIND_LINKS=/wheels python -m pip install --no-cache-dir --no-deps .')
(root/'Dockerfile').write_text(docker)
PY
docker --config "$PWD/data/docker-client" build --tag nbappbot:local data/image-build
