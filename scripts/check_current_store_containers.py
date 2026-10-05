#!/usr/bin/env python3
"""Real cross-container C12 acceptance on an isolated supported filesystem.

Use an empty trusted ext4/XFS/Btrfs bind mount, or --docker-volume for a fresh
Docker-managed ext4 volume. Never injects filesystem probes. Uses a unique
project and network with no published ports; removes only its own test volumes.
No production connection.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path)
    p.add_argument('--docker-volume',action='store_true',
                   help='Use a fresh Docker-managed ext4 volume instead of a host bind mount')
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--with-suite',action='store_true',help='Also run native kernel tests and B01–B04 on the same supported mount')
    args=p.parse_args()
    if args.docker_volume:
        if args.root is not None:
            p.error('--docker-volume and --root are mutually exclusive')
    elif (args.root is None or not args.root.is_absolute() or args.root.resolve()!=args.root
          or not args.root.is_dir() or any(args.root.iterdir())):
        p.error('Require an existing EMPTY absolute local test directory')
    if args.output.exists():
        p.error('Require a new output path')
    if shutil.which('docker') is None:
        p.error('Docker CLI unavailable; cross-container acceptance is NOT EXECUTED')
    if args.root is not None:
        os.chmod(args.root,0o700)
    project='lfd01-'+uuid4().hex[:12]
    compose=Path(__file__).resolve().parents[1]/'ops/lf01/compose/compose.yml'
    env=dict(os.environ,LF_TEST_ROOT=str(args.root or '/tmp/lfd01-unused-bind'),
             LF_TEST_UID=str(os.getuid()),LF_TEST_GID=str(os.getgid()))
    base=['docker','compose','--env-file','/dev/null','-f',str(compose)]
    if args.docker_volume:
        base+=['-f',str(compose.with_name('compose.volume.yml'))]
    base+=['-p',project]
    created=[]
    result={'task':'LF-D01','case':'C12-cross-container','synthetic':True,'passed':False,'steps':[]}

    def cmd(parts,timeout=120):
        try:
            return subprocess.run(parts,env=env,check=True,capture_output=True,text=True,timeout=timeout).stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            # These commands run only isolated synthetic tests. Keep a bounded
            # diagnostic tail instead of losing the actual pytest assertion when
            # capture_output=True raises; never print the inherited environment.
            failure={'type':type(error).__name__,'returncode':getattr(error,'returncode',None)}
            for stream in ('stdout','stderr'):
                value=getattr(error,stream,None) or ''
                if isinstance(value,bytes):
                    value=value.decode('utf-8',errors='replace')
                failure[stream+'_tail']=value[-16384:]
            result['command_failure']=failure
            print(json.dumps(failure,ensure_ascii=False),file=sys.stderr,flush=True)
            raise

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

    def wait_ready(container,name):
        end=time.monotonic()+20
        while True:
            if args.docker_volume:
                ready=subprocess.run(['docker','exec',container,'test','-e',
                                      '/work/coord/'+name+'.ready'],
                                     env=env,capture_output=True).returncode==0
            else:
                ready=(args.root/'coord'/(name+'.ready')).exists()
            if ready: break
            if time.monotonic()>end: raise RuntimeError('probe did not become ready')
            time.sleep(.1)

    def completed(name):
        code=cmd(['docker','wait',name],timeout=30)
        if code!='0':
            raise RuntimeError('container probe failed; inspect the isolated probe logs')
        result['steps'].append({'mode':name.split(project+'-')[-1], 'passed':True})

    try:
        cmd(base+['build','kernel'],timeout=900)
        if args.docker_volume:
            # The volume starts root-owned. Prepare it for the ordinary test
            # UID without weakening the application's filesystem checks.
            cmd(base+['run','--rm','--no-deps','--user','0:0','kernel','chown',
                      f'{os.getuid()}:{os.getgid()}','/work'])
        cmd(base+['up','-d','--wait','postgres'],timeout=120)
        run('init')
        writer=start('hold_writer'); wait_ready(writer,'writer'); run('contend')
        cmd(['docker','kill','--signal','KILL',writer]); cmd(['docker','wait',writer])
        run('after_kill')
        reader=start('hold_reader'); wait_ready(reader,'reader')
        correcting=start('correct')
        time.sleep(.5)
        assert running(correcting),'commit incorrectly crossed an active read lock'
        run('inspect')
        cleaning=start('cleanup_contend'); wait_ready(cleaning,'cleanup')
        assert running(cleaning),'cleanup did not coordinate with the active writer'
        if args.docker_volume:
            cmd(['docker','exec',reader,'touch','/work/coord/reader.release'])
        else:
            (args.root/'coord'/'reader.release').touch(exist_ok=False)
        completed(reader); completed(correcting); completed(cleaning); run('verify')
        if args.with_suite:
            output=cmd(base+['run','--rm','--no-deps','kernel','python','-m','pytest',
                            'tests/test_data_store_kernel.py','tests/test_data_store_schema.py',
                            'tests/test_data_store_local_pipeline.py',
                            'tests/test_data_store_runtime_budget.py',
                            '--basetemp=/work/pytest','-q','--tb=short'],timeout=240)
            result['kernel_tests']={'passed':True,'summary':output.splitlines()[-1]}
            # Exercise the actual admission gate and inactive sealed resources
            # on this same supported filesystem. Failure recovery stays finite
            # and synthetic; these tests never reach a deployment host.
            output=cmd(base+['run','--rm','--no-deps','kernel','python','-m','pytest',
                            'tests/test_r01_service_switch.py',
                            'tests/test_data_store_scheduler_handoff.py',
                            '--basetemp=/work/switch-pytest','-q','--tb=short'],timeout=120)
            result['service_switch_tests']={'passed':True,'summary':output.splitlines()[-1]}
            if args.docker_volume:
                cmd(base+['run','--rm','--no-deps','kernel','mkdir','-m','0700','/work/benchmark'])
            else:
                (args.root/'benchmark').mkdir(mode=0o700)
            cmd(base+['run','--rm','--no-deps','kernel','python','/app/benchmark_current_store.py',
                      '--root','/work/benchmark','--output','/work/benchmark-results.json'],timeout=900)
            if args.docker_volume:
                result['benchmarks']=json.loads(cmd(base+['run','--rm','--no-deps','kernel',
                                                    'cat','/work/benchmark-results.json']))
            else:
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
