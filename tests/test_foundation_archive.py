"""Archive sharing stays within the private owner group used by containers."""
import hashlib
import io
import json
import os
import pwd
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from scripts.archive_foundation_runtime import archive_image, grant_container_group_read


class FoundationArchiveTests(unittest.TestCase):
    def test_archive_read_is_limited_to_private_owner_group(self):
        config=b'{"fixture":true}'
        digest='sha256:'+hashlib.sha256(config).hexdigest()
        def command(args):
            if '{{.Size}}' in args:return '1024'
            if args[:3]==['docker','image','inspect']:return digest
            return json.dumps({'python':'3.12.2','lock_hash':'a'*64})
        def subprocess_run(args,**kwargs):
            if args[:3]!=['docker','image','save']:return
            target=Path(args[args.index('--output')+1])
            with tarfile.open(target,'w') as archive:
                for name,value in [('manifest.json',json.dumps([{'Config':'config.json'}]).encode()),('config.json',config)]:
                    info=tarfile.TarInfo(name);info.size=len(value)
                    archive.addfile(info,io.BytesIO(value))
            target.chmod(0o600)
        owner=pwd.getpwuid(os.getuid())
        with tempfile.TemporaryDirectory() as directory,patch('scripts.archive_foundation_runtime.run',side_effect=command),patch('scripts.archive_foundation_runtime.subprocess.run',side_effect=subprocess_run),patch('scripts.archive_foundation_runtime.grp.getgrgid',return_value=SimpleNamespace(gr_mem=[])),patch('scripts.archive_foundation_runtime.pwd.getpwall',return_value=[owner]):
            result=archive_image('fixture',directory,reader_gid=os.getgid())
            target=Path(directory)/result['archive_key']
            self.assertEqual(target.stat().st_mode & 0o777,0o640)
            self.assertEqual(result['archive_hash'],hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertEqual(result['verification']['restored_image_digest'],digest)

    def test_shared_host_group_is_rejected_without_changing_permissions(self):
        with tempfile.TemporaryDirectory() as directory,patch('scripts.archive_foundation_runtime.grp.getgrgid',return_value=SimpleNamespace(gr_mem=['unrelated-account'])):
            target=Path(directory)/'private.tar';target.write_bytes(b'fixture');target.chmod(0o600)
            with self.assertRaisesRegex(ValueError,'shared'):
                grant_container_group_read(target,os.getgid())
            self.assertEqual(target.stat().st_mode & 0o777,0o600)
