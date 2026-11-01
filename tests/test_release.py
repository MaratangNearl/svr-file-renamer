import importlib.util
from pathlib import Path
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepare_release', ROOT/'scripts/prepare-release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_export_contains_only_public_files(tmp_path):
    folder, archive = release.export(ROOT, tmp_path/'public')
    files = {str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file()}
    expected = set(release.PUBLIC_FILES)
    if (ROOT/'LICENSE').is_file(): expected.add('LICENSE')
    assert files == expected
    assert 'PUTTY_TEST_GUIDE.md' not in files and '.env' not in files
    assert not any('maintainer' in name.lower() for name in files)
    assert not any('sqlite' in name or '__pycache__' in name or '.venv' in name for name in files)
    with tarfile.open(archive) as bundle:
        assert set(bundle.getnames()) == {'svr-file-renamer/'+name for name in expected}


def test_export_does_not_overwrite_existing_output(tmp_path):
    folder = tmp_path/'existing'; folder.mkdir(); (folder/'keep').write_text('keep')
    with pytest.raises(FileExistsError): release.export(ROOT, folder)
    assert (folder/'keep').read_text() == 'keep'


def test_export_rejects_private_symlink(tmp_path):
    source = tmp_path/'source'; source.mkdir()
    secret = tmp_path/'secret'; secret.write_text('secret')
    (source/release.PUBLIC_FILES[0]).symlink_to(secret)
    with pytest.raises(ValueError): release.export(source, tmp_path/'public')
    assert not (tmp_path/'public').exists()
