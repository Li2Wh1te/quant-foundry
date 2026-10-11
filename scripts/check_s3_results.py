#!/usr/bin/env python3
"""Measure the D11 Rust module stream and real file-write failure, NOT B1-B4."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import resource
import signal
import subprocess
import tempfile
import time
ROOT = Path(__file__).resolve().parents[1]

def run(events: int, *, file_limit: int | None = None) -> dict:
    affinity=sorted(os.sched_getaffinity(0))[:4]
    def limit():
        os.sched_setaffinity(0,affinity)
        resource.setrlimit(resource.RLIMIT_AS,(128*1024*1024,128*1024*1024))
        if file_limit is not None:
            signal.signal(signal.SIGXFSZ,signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE,(file_limit,file_limit))
    args=[str(ROOT/'engine/target/release/examples/result_stream'),str(events)]
    if file_limit is not None: args.append('expect-write-failure')
    with tempfile.TemporaryFile() as output:
        start=time.monotonic()
        child=subprocess.Popen(args,stdout=output,stderr=subprocess.STDOUT,preexec_fn=limit)
        _,status,usage=os.wait4(child.pid,0)
        child.returncode=os.waitstatus_to_exitcode(status)
        wall=time.monotonic()-start
        output.seek(0);text=output.read().decode()
    if child.returncode != 0: raise RuntimeError(f'{args}: exit {child.returncode}\n{text}')
    packet=json.loads(text)
    if file_limit is None:
        assert packet['events']==events
        assert packet['results']['outcome']['status']=='succeeded'
        assert packet['results']['counts']['trades']==events//1000
        assert packet['results']['counts']['equity']==events//20000+1
        assert packet['peak_retained_market_events']==1
        assert usage.ru_maxrss < 128*1024
    else:
        assert packet['outcome']['status']=='failed'
        assert packet['failure']['code']=='RESULT_BUDGET_EXCEEDED'
    return dict(events=events,file_limit_bytes=file_limit,cpu_affinity=affinity,
        virtual_memory_limit_bytes=128*1024*1024,peak_child_rss_kib=usage.ru_maxrss,
        cpu_seconds=usage.ru_utime+usage.ru_stime,wall_seconds=wall,packet=packet)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events',type=int,default=10_000_000)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    filesystem = subprocess.check_output(['stat','-f','-c','%T',tempfile.gettempdir()],text=True).strip()
    sizes=sorted({min(1_000_000,args.events),args.events})
    report=dict(scope='D11 module only; no gateway, D09 account, Python callback, supervisor, DB, or Docker in this resource probe',
        product_b1_b4_accepted=False,production_executed=False,environment=dict(os=platform.platform(),python=platform.python_version(),
            rust='locked 1.90.0 release',filesystem=f'{filesystem}; private temporary result files',input='generated one event at a time; no market input file or copy',
            page_cache='fresh result files each run; source cache cold/hot not applicable',measurement='wait4 child peak RSS; compiler and launcher excluded'),
        streams=[run(n) for n in sizes],write_failure=run(20_000,file_limit=1024))
    text=json.dumps(report,ensure_ascii=False,indent=2)+'\n'
    if args.output: args.output.write_text(text)
    print(text)
if __name__=='__main__':main()
