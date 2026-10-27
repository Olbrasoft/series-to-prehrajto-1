"""Exercise the status workflow in the dependency-free Python it deploys."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def test_status_cli_runs_without_site_packages(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    src = tmp_path / 'src'
    shutil.copytree(repo / 'src', src, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    gh = bin_dir / 'gh'
    gh.write_text('#!/bin/sh\nprintf "[]\\n"\n')
    gh.chmod(0o755)
    code = f"import sys,runpy; sys.path.insert(0,{str(src)!r}); runpy.run_path({str(src / 'ops_status.py')!r},run_name='__main__')"
    env = {**os.environ, 'PATH': str(bin_dir)}
    env.pop('UPLOAD_BACKLOG_PATH', None)
    result = subprocess.run([sys.executable, '-I', '-S', '-c', code], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / 'reports' / 'ops-status.json').read_text())
    assert report['counts']['uploaded_episodes'] == 0
    assert report['workflow_runs'] == []
