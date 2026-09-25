#!/usr/bin/env python3
"""Real cross-container C12 acceptance on an EMPTY trusted local test directory.

Requires Docker Compose and supported ext4/XFS/Btrfs bind mount. Never injects
filesystem probes. Uses a unique project and network with no published ports;
removes only the containers/test PG volume it created. No production connection.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--with-suite',action='store_true',help='Also run native kernel tests and B01–B04 on the same supported mount')
    args=p.parse_args()
    if (not args.root.is_absolute() or args.root.resolve()!=args.root or not args.root.is_dir()
            or any(args.root.iterdir()) or args.output.exists()):
        p.error('Require an existing EMPTY absolute local test directory and a new output path')
    if shutil.which('docker') is None:
        p.error('Docker CLI unavailable; cross-container acceptance is NOT EXECUTED')
    os.chmod(args.root,0o700)
    project='lfd01-'+uuid4().hex[:12]
    compose=Path(__file__).resolve().parents[1]/'ops/lf01/compose/compose.yml'
    env=dict(os.environ,LF_TEST_ROOT=str(args.root),LF_TEST_UID=str(os.getuid()),LF_TEST_GID=str(os.getgid()))
    base=['docker','compose','--env-file','/dev/null','-f',str(compose),'-p',project]
    created=[]
    result={'task':'LF-D01','case':'C12-cross-container','synthetic':True,'passed':False,'steps':[]}

    def cmd(parts,timeout=120):
        return subprocess.run(parts,env=env,check=True,capture_output=True,text=True,timeout=timeout).stdout.strip()

    def run(mode):
        output=cmd(base+['run','--rm','--no-deps','kernel','python','/app/container_probe.py',mode])
        result['steps'].append(json.loads(output.splitlines()[-1]))

    def start(mode):
        name=project+'-'+mode
        created.append(name)
        cmd(base+['run','-d','--no-deps','--name',name,'kernel','python','/app/container_probe.py',mode])
        return name

    def running(name):
        return cmd(['docker','inspect','--format','{{.State.Running}}',name])=='true'

    def wait_ready(name):
        end=time.monotonic()+20
        while not (args.root/'coord'/(name+'.ready')).exists():
            if time.monotonic()>end: raise RuntimeError('probe did not become ready')
            time.sleep(.1)

    def completed(name):
        code=cmd(['docker','wait',name],timeout=30)
        if code!='0':
            raise RuntimeError('container probe failed; inspect the isolated probe logs')
        result['steps'].append({'mode':name.split(project+'-')[-1], 'passed':True})

    try:
        cmd(base+['build','kernel'],timeout=900)
        cmd(base+['up','-d','--wait','postgres'],timeout=120)
        run('init')
        writer=start('hold_writer'); wait_ready('writer'); run('contend')
        cmd(['docker','kill','--signal','KILL',writer]); cmd(['docker','wait',writer])
        run('after_kill')
        reader=start('hold_reader'); wait_ready('reader')
        correcting=start('correct')
        time.sleep(.5)
        assert running(correcting),'commit incorrectly crossed an active read lock'
        run('inspect')
        cleaning=start('cleanup_contend'); wait_ready('cleanup')
        assert running(cleaning),'cleanup did not coordinate with the active writer'
        (args.root/'coord'/'reader.release').touch(exist_ok=False)
        completed(reader); completed(correcting); completed(cleaning); run('verify')
        if args.with_suite:
            output=cmd(base+['run','--rm','--no-deps','kernel','python','-m','pytest',
                            'tests/test_data_store_kernel.py','tests/test_data_store_schema.py',
                            '--basetemp=/work/pytest','-q','--tb=short'],timeout=240)
            result['kernel_tests']={'passed':True,'summary':output.splitlines()[-1]}
            (args.root/'benchmark').mkdir(mode=0o700)
            cmd(base+['run','--rm','--no-deps','kernel','python','/app/benchmark_current_store.py',
                      '--root','/work/benchmark','--output','/work/benchmark-results.json'],timeout=900)
            result['benchmarks']=json.loads((args.root/'benchmark-results.json').read_text())
            assert result['benchmarks']['complete']
        result['passed']=True
    except Exception as error:
        result['error_type']=type(error).__name__
        raise
    finally:
        # Unique exact names/project created above, never docker system prune.
        for name in created:
            subprocess.run(['docker','rm','-f',name],env=env,capture_output=True)
        subprocess.run(base+['down','--volumes'],env=env,capture_output=True)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf8') as f:
            json.dump(result,f,ensure_ascii=False,indent=2)
        print(json.dumps({'case':'C12-cross-container','passed':result['passed']}))

if __name__=='__main__': main()
