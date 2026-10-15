"""Export the tested immutable image pair, without test data or diagnostics."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    receipt = json.loads(args.manifest.read_text())
    source = receipt['source']
    commit = subprocess.check_output(['git','rev-parse','HEAD'], cwd=Path(__file__).resolve().parents[1], text=True).strip()
    assert source['measurement_checkout_commit'] == commit and source['measurement_worktree_dirty'] is False
    ids = [receipt['images'][kind]['id'] for kind in ('backend','frontend')]
    assert len(set(ids)) == 2 and all(re.fullmatch(r'sha256:[0-9a-f]{64}', value) for value in ids)
    images = json.loads(subprocess.check_output(['docker','image','inspect',*ids], text=True))
    assert [item['Id'] for item in images] == ids
    assert all(item['Os'] == 'linux' and item['Architecture'] == receipt['architecture'] for item in images)
    args.output.mkdir(parents=True, exist_ok=False)
    archive = args.output / 'images.tar'
    # Save original image IDs, never a running container or its mounted data.
    subprocess.run(['docker','image','save','--output',str(archive),*ids], check=True, timeout=300)
    with tarfile.open(archive) as saved:
        manifest = json.load(saved.extractfile('manifest.json'))
        assert len(manifest) == 2
        assert {Path(item['Config']).name.removesuffix('.json') for item in manifest} == {value[7:] for value in ids}
    digest = hashlib.sha256()
    with archive.open('rb') as handle:
        while data := handle.read(1024*1024):
            digest.update(data)
    (args.output / 'pair.json').write_text(json.dumps(dict(format='qf.s2.image-pair.v1',
        source_commit=commit, architecture=receipt['architecture'],
        images={kind:dict(id=receipt['images'][kind]['id']) for kind in ('backend','frontend')},
        archive='images.tar', archive_sha256=digest.hexdigest()), indent=2)+'\n')


if __name__ == '__main__':
    main()
