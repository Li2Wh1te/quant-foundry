"""Assemble the five approved R01 repairs on the pinned diagnostic image.

The image supplies the existing private controls and membership branch. Only
hash-pinned PR 193/194 code is adapted; no dependencies or runtime settings are
installed or copied from whole main. The operator pulls this recipe from Git.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap

BASE_IMAGE = 'sha256:645d83b0f239cbc329e74d46969ed684d2060c77090b3a656650b82bbeb5140e'
SOURCE_REVISION = '4454337feaf3ce4e0f1b62c211bf0035f1e7a1fc'
BASE_HASHES = {
    'app/data_store/pipeline.py': 'ff3bb7727bd987500d5ed2fe6cd2d0bcdbbce421299f95ccae9236e8ac25e8ab',
    'app/data_ingestion/tonghuashun/repository.py': '90f0aac6ab1a57e8abc67d71071902ef271f849bf63d0b4958023612c76c6ac3',
    'app/data_ingestion/tonghuashun/directory_proof.py': '734835b559aaeb5d74fb72ee10e18eca44c3426cfcd804d554574429b949b9db',
    'app/data_ingestion/tonghuashun/service.py': 'd4d3977ba549b307a83bd18050586bd824ba40b6fda127022074d8731cf8295d',
    'app/data_ingestion/tonghuashun/ticker_diagnostics.py': 'f0a90ff91bc66f49b1072c330d397ea749c5db4014f60532ddf9e8f5f6d2cf4b',
}
MERGED_HASHES = {
    'app/data_store/pipeline.py': '3bb1ea72d8fb77e41cb5bab85ec956148f734df5091a8e00abe35bb346479de8',
    'app/data_ingestion/tonghuashun/repository.py': '00ea9cc4b2c0cfcdc8f64b8d729401653559192a69d7888954a31765c914cab0',
    'app/data_ingestion/tonghuashun/directory_proof.py': '8dd200a95b8924fba3beca58b57486db3a0616e9919471c91e2f51332136403c',
    'app/data_ingestion/tonghuashun/service.py': '0039c5a4196c5620ae52cff52c462ccf1890c86a5449d0ffb39aa9e666d869a9',
    'app/data_ingestion/tonghuashun/ticker_diagnostics.py': '0fd407f745177d0cd204cb9508ad59d362db0dc9a324f54b7fcd57a23fcbdefd',
}


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError('R01 compatibility anchor mismatch')
    return source.replace(before, after)


def function(source, name):
    return next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == name)


def replay_node(source, *, retention=False):
    candidates = []
    for node in ast.walk(function(source, '_collect')):
        if not isinstance(node, ast.If):
            continue
        text = ast.get_source_segment(source, node)
        if retention:
            match = ('failure_recorded' in ast.unparse(node.test)
                     and 'TickerAssetTypeConflict' in ast.unparse(node.test))
        else:
            match = (ast.unparse(node.test) == 'directory_replays is not None'
                     and any(isinstance(n, ast.For) and 'directory_replays.ready('
                             in ast.unparse(n.iter) for n in node.body))
        if match:
            candidates.append(node)
    if len(candidates) != 1:
        raise ValueError('R01 replay region mismatch')
    return candidates[0]


def transplant(source, node, donor, donor_node, *, adapt=None):
    block = ast.get_source_segment(donor, donor_node)
    # get_source_segment drops only the first line's indent.
    block = textwrap.dedent(' ' * donor_node.col_offset + block)
    if adapt:
        block = adapt(block)
    lines = source.splitlines(keepends=True)
    lines[node.lineno - 1:node.end_lineno] = [textwrap.indent(block, ' ' * node.col_offset) + '\n']
    result = ''.join(lines)
    ast.parse(result)
    return result


def e15_pipeline(source, merged):
    # These are the reviewed PR 193 changes. Its existing private member-proof
    # condition remains between the same anchors and is never transplanted.
    for before, after in (
        ('logical={};prior={};resolved={};count=0',
         'logical={};prior={};resolved={};physical_by_key={};member_keys={};count=0'),
        ("    after=''\n", "    observed=set(logical)\n    after=''\n"),
        ("            resolved[physical['issue_key']]=physical['evidence_token']\n",
         "            physical_by_key[physical['issue_key']]=physical\n"
         "            member_keys[physical['issue_key']]=set()\n"
         "            resolved[physical['issue_key']]=physical['evidence_token']\n"),
        ('            for old in members(physical):\n',
         "            for old in members(physical):\n"
         "                member_keys[physical['issue_key']].add(old['issue_key'])\n"),
        ('    changes.new_objects=len(set(logical)-set(prior))\n',
         '    changes.new_objects=len(set(logical)-set(prior))\n'
         '    changes.unresolved_objects=len(logical)\n'),
        ('qualified=not issues,expected_generation=generation',
         'qualified=not resolved.unresolved_objects,expected_generation=generation'),
    ):
        source = replace_once(source, before, after)
    start = merged.index('    retained=[]\n')
    end = merged.index('    # Physical regrouping', start)
    source = replace_once(source, '    additions=pack(logical.values())\n',
                          '    additions=pack(logical.values())\n' + merged[start:end])
    ast.parse(source)
    return source


def e40_service(source, merged):
    before = {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(source).body
              if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name != '_collect'}
    source = replace_once(source,
        '        acquisition = Acquisition(client, repair_native_keys=repair_native_keys)\n',
        '        from .directory_proof import DirectoryAcquisition\n'
        '        acquisition = (DirectoryAcquisition if directory_replays is not None else Acquisition)(\n'
        '            client, repair_native_keys=repair_native_keys)\n')
    def adapt(block):
        block = (block.replace('check_publication()', 'check_execution()')
                .replace('previous.revision + 1', 'expected_revision + 1')
                .replace('monitor.emit(directory_reused=',
                    "monitor.emit(current_interface=None, current_request=None, request_stage='publication_validation', directory_reused="))
        if 'tonghuashun_directory_pair_rejected' in block:
            block = replace_once(block,
                "message='ETF 与场外基金目录配对发布失败；完成标记未推进，保留已有成功版本。',",
                "message='同花顺基金目录配对发布失败：范围为 ETF 目录与场外基金目录，日期范围按接口快照采集时间记录；候选 2 个，新增拉取 0 条，发布 0 个，失败 2 个；完成标记未推进，保留已有成功版本。',")
            block = replace_once(block, "subjects=['fund-etf', 'fund-otc'],",
                                 "subjects=['fund-etf', 'fund-otc'], start_date=None, end_date=None,")
            block, count = re.subn(r"^([ \t]*)logger.warning\('tonghuashun_directory_pair_rejected',",
                lambda match: match[1] + 'event_logger = structlog.get_logger(__name__) if execution_deadline.get() is not None else logger\n'
                    + match[1] + "event_logger.warning('tonghuashun_directory_pair_rejected',",
                block, flags=re.M)
            if count != 1:
                raise ValueError('R01 pair log sink mismatch')
        return block
    for retention in (False, True):
        source = transplant(source, replay_node(source, retention=retention),
            merged, replay_node(merged, retention=retention),
            adapt=adapt)
    after = {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(source).body
             if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name != '_collect'}
    if before != after:
        raise ValueError('R01 controls or diagnostic methods changed')
    # The scope fence, repair baseline, final outcome hook and supervisor
    # deadline remain actual base-image code, not main's different controls.
    required = ('repair.validate()', 'repair.baseline(', 'execution_deadline.get()',
                'record_outcome(spec.key, succeeded, kind, error_message, result.get("version_id"))')
    if not all(value in source for value in required):
        raise ValueError('R01 existing controls or outcome hook missing')
    return source


def e40_proof(merged):
    node = function(merged, 'check_publication')
    lines = merged.splitlines(keepends=True)
    # Preserve the public monitor boundary and the base's independent hard
    # deadline/cancellation check, which public main does not provide.
    insert_at = node.body[0].end_lineno if isinstance(node.body[0], ast.Expr) else node.lineno
    lines[insert_at:insert_at] = ['    from .control import check_execution\n',
                                '    check_execution()\n']
    result = ''.join(lines)
    ast.parse(result)
    return result


def prepare(source, output, repository=None):
    repository = repository or Path(__file__).resolve().parents[1]
    if output.exists():
        raise ValueError('Refusing to replace an existing R01 build context')
    base, merged = {}, {}
    # All hash checks precede any output, including a partially assembled image.
    for name, expected in BASE_HASHES.items():
        data = (source / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('R01 base source hash mismatch: ' + name)
        base[name] = data.decode()
        data = (repository / 'backend' / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != MERGED_HASHES[name]:
            raise ValueError('R01 merged source hash mismatch: ' + name)
        merged[name] = data.decode()
    prefix = 'app/data_ingestion/tonghuashun/'
    result = dict(merged)
    result['app/data_store/pipeline.py'] = e15_pipeline(base['app/data_store/pipeline.py'], merged['app/data_store/pipeline.py'])
    result[prefix + 'service.py'] = e40_service(base[prefix + 'service.py'], merged[prefix + 'service.py'])
    result[prefix + 'directory_proof.py'] = e40_proof(merged[prefix + 'directory_proof.py'])
    for text in result.values():
        ast.parse(text)
    output.mkdir(parents=True, mode=0o700)
    hashes = {}
    for name, text in result.items():
        target = output / name; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text); target.chmod(0o644)
        hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    tag = 'qf-r01-repair-base:' + BASE_IMAGE.removeprefix('sha256:')
    (output / 'Dockerfile').write_text('FROM ' + tag + '\n' + ''.join(
        'COPY ' + name + ' /app/' + name + '\n' for name in result))
    receipt = {'base_image': BASE_IMAGE, 'source_revision': SOURCE_REVISION,
        'base_source_hashes': BASE_HASHES, 'merged_source_hashes': MERGED_HASHES,
        'overlay_hashes': hashes, 'local_base_tag': tag, 'whole_main_copied': False,
        'dependencies_or_migrations_changed': False, 'runtime_configuration_changed': False,
        'existing_private_controls_and_diagnostics_preserved': True}
    (output / 'overlay-manifest.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.source_root:
        receipt = prepare(args.source_root, args.output)
    else:
        identity = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', BASE_IMAGE], text=True).strip()
        if identity != BASE_IMAGE:
            raise ValueError('R01 base image identity mismatch')
        container = subprocess.check_output(['docker', 'create', '--network', 'none', BASE_IMAGE], text=True).strip()
        try:
            with tempfile.TemporaryDirectory(prefix='r01-repair-base-') as temp:
                source = Path(temp)
                for name in BASE_HASHES:
                    target = source / name; target.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.run(['docker', 'cp', container + ':/app/' + name, str(target)], check=True)
                receipt = prepare(source, args.output)
        finally:
            subprocess.run(['docker', 'rm', container], check=True, stdout=subprocess.DEVNULL)
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
