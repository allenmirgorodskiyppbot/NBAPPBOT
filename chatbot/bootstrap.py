"""Download the pinned app and workbook, then start the Akash chat service."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

REPOSITORY = 'allenmirgorodskiyppbot/NBAPPBOT'
WORKBOOK_REF = '1188dd88beb128acba8afc66fb6918b6cc82ae6e'
WORKBOOK_SHA256 = '02fba50eeda757f140fc7c4fb4a500ca6d565f6646c45481436d2b78db22ba04'


def download(url, destination):
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                destination.write_bytes(response.read())
            return
        except Exception:
            if attempt == 4:
                raise
            time.sleep(3 * (attempt + 1))


def main():
    ref = os.environ['SOURCE_REF']
    if not re.fullmatch(r'[0-9a-f]{40}', ref):
        raise ValueError('SOURCE_REF must be the full GitHub commit SHA.')
    root = Path('/app')
    root.mkdir(parents=True, exist_ok=True)
    for name in ['app.py', 'workbook.py', 'requirements.txt', 'index.html']:
        print('Downloading ' + name, flush=True)
        download(f'https://raw.githubusercontent.com/{REPOSITORY}/{ref}/chatbot/{name}', root / name)
    workbook = root / 'Knicks_2026_27_Completed.xlsx'
    print('Downloading Knicks workbook', flush=True)
    download(f'https://raw.githubusercontent.com/{REPOSITORY}/{WORKBOOK_REF}'
             '/data/Knicks_2026_27_Completed.xlsx', workbook)
    if hashlib.sha256(workbook.read_bytes()).hexdigest() != WORKBOOK_SHA256:
        raise ValueError('Workbook checksum does not match the approved snapshot.')
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-cache-dir',
                    '-r', str(root / 'requirements.txt')], check=True)
    os.environ['WORKBOOK_PATH'] = str(workbook)
    os.chdir(root)
    os.execv(sys.executable, [sys.executable, '-m', 'uvicorn', 'app:app',
                             '--host', '0.0.0.0', '--port', '8000', '--workers', '1',
                             '--no-access-log'])


if __name__ == '__main__':
    main()
