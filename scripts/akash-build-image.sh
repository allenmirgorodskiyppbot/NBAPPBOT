#!/usr/bin/env bash
# Local offline-compatible image build. Pushing a registry image is a separate step.
set -euo pipefail
cd "$(dirname "$0")/.."
task_image_tag="${1:-nbappbot:akash}"
mkdir -p data/wheelhouse
.venv/bin/python -c 'import sys; assert sys.version_info[:2]==(3,12) and sys.platform=="linux"'
.venv/bin/python -m pip download --only-binary=:all: --cache-dir data/pip-cache --dest data/wheelhouse -r requirements.lock 'setuptools==80.9.0' 'wheel==0.45.1'
.venv/bin/python - <<'PY'
from pathlib import Path
import hashlib, json, shutil
root=Path('data/akash-image-build')
if root.exists():
    shutil.rmtree(root)
root.mkdir()
for name in ('requirements.lock','pyproject.toml','config.example.json'):
    shutil.copy2(name,root/name)
shutil.copytree('nbabot',root/'nbabot',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
shutil.copytree('deploy/akash',root/'deploy/akash')
shutil.copytree('scripts',root/'scripts',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
shutil.copytree('data/wheelhouse',root/'wheels')
(root/'wheel-checksums.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'wheels').glob('*.whl')},indent=2))
docker=Path('deploy/akash/Dockerfile').read_text().replace('COPY requirements.lock pyproject.toml ./','COPY requirements.lock pyproject.toml ./\nCOPY wheels /wheels')
docker=docker.replace('python -m pip install --no-cache-dir -r requirements.lock','python -m pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.lock')
docker=docker.replace('python -m pip install --no-cache-dir --no-deps .','PIP_NO_INDEX=1 PIP_FIND_LINKS=/wheels python -m pip install --no-cache-dir --no-deps .')
(root/'Dockerfile').write_text(docker)
PY
docker --config "$PWD/data/docker-client" build --tag "$task_image_tag" data/akash-image-build
