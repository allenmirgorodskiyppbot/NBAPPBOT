"""A clean deployment must contain all application modules, including lazy imports."""
import ast
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from bootstrap import APP_FILES


ROOT = Path(__file__).resolve().parent


class BootstrapPackageTests(unittest.TestCase):
    def test_every_local_runtime_import_is_in_the_deployment_manifest(self):
        packaged = set(APP_FILES)
        self.assertEqual(len(packaged),len(APP_FILES))
        missing = set()
        for name in APP_FILES:
            self.assertTrue((ROOT/name).is_file(), name)
            if not name.endswith('.py'):
                continue
            for node in ast.walk(ast.parse((ROOT/name).read_text())):
                modules = ([node.module] if isinstance(node,ast.ImportFrom) and node.module else
                           [alias.name for alias in node.names] if isinstance(node,ast.Import) else [])
                for module in modules:
                    local = module.split('.')[0]+'.py'
                    if (ROOT/local).is_file() and local not in packaged:
                        missing.add((name,local))
        self.assertEqual(missing,set(), 'A lazy import can fail after startup if its module is not downloaded.')

    def test_clean_downloaded_package_imports_without_workspace_modules(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in APP_FILES:
                shutil.copyfile(ROOT/name,Path(folder)/name)
            result = subprocess.run([sys.executable,'-I','-c',
                'import sys; sys.path.insert(0,sys.argv[1]); import app, chat_flow, game_schedule_check, pregame_freshness',folder],
                cwd=folder, check=False, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)


if __name__ == '__main__':
    unittest.main()
