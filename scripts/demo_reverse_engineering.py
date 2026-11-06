"""Run reproducible THESUS investigations against self-authored fixtures."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import statistics
import subprocess
import time
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_STORED, ZipFile, ZipInfo

import httpx
from fastapi import FastAPI, Response

from app.app import create_app
from app.core.config import Settings
from app.reverse_engineering.engines import analyze


JS_FILES = {
    'package.json': '{"name":"thesus-invoice-fixture","version":"1.0.0","main":"main.cjs"}',
    'main.cjs': """const { ipcMain } = require('electron');
const cents = () => 4200;
ipcMain.handle('invoice:total', () => ({ cents: cents() }));
""",
    'preload.cjs': """const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('invoice', { total: () => ipcRenderer.invoke('invoice:total') });
""",
    'renderer.js': """async function showTotal() {
  return (await window.invoice.total()).cents;
}
showTotal();
""",
}

_NODE_ORACLE = """
const fs = require('node:fs');
const vm = require('node:vm');
const files = JSON.parse(fs.readFileSync(0, 'utf8'));
const handlers = new Map();
const bridges = {};
const main = { ipcMain: {handle: (channel, fn) => handlers.set(channel, fn)} };
const preload = {
  contextBridge: {exposeInMainWorld: (name, api) => bridges[name] = api},
  ipcRenderer: {invoke: async (channel) => handlers.get(channel)()},
};
vm.runInNewContext(files['main.cjs'], {require: () => main});
vm.runInNewContext(files['preload.cjs'], {require: () => preload});
vm.runInNewContext(files['renderer.js'], {window: bridges}).then(value => {
  process.stdout.write(JSON.stringify({value, channels: [...handlers.keys()]}));
});
"""
_UPLOAD = {'X-Analysis-Authorization': 'owned', 'Content-Type': 'application/octet-stream'}


def js_archive() -> bytes:
    carrier = BytesIO()
    with ZipFile(carrier, mode='w', compression=ZIP_STORED) as archive:
        for name, content in JS_FILES.items():
            archive.writestr(ZipInfo(name), content)
    return carrier.getvalue()


def compile_fixtures(zig: Path, root: Path) -> dict[str, bytes]:
    source = root / 'entry.c'
    source.write_text('int entry(void) { return 42; }\n', encoding='ascii')
    env = {**os.environ, 'ZIG_GLOBAL_CACHE_DIR': str(root / 'zig-global'), 'ZIG_LOCAL_CACHE_DIR': str(root / 'zig-local')}
    artifacts = {}
    for label, target in [('native', 'x86_64-linux-musl'), ('embedded', 'aarch64-freestanding-none')]:
        binary = root / f'{label}.elf'
        subprocess.run(
            [str(zig), 'cc', '-target', target, '-O2', '-fomit-frame-pointer', '-nostdlib', '-static', '-s',
             '-Wl,-e,entry', str(source), '-o', str(binary)],
            env=env, check=True, timeout=90,
        )
        artifacts[label] = binary.read_bytes()
    return artifacts


async def capture(*, candidate: bool = False, mutated: bool = False) -> tuple[bytes, list]:
    app = FastAPI()

    @app.api_route('/{path}', methods=['GET', 'POST'])
    async def serve(path: str, response: Response):
        if candidate:
            price, tax, status = {'invoice': (4200, 420, 200), 'preview': (0, 0, 204)}.get(path, (0, 0, 404))
            response.status_code = 202 if mutated and path == 'preview' else status
            return {'total': price + tax} if path == 'invoice' else None
        if path == 'invoice':
            return {'total': 4620}
        response.status_code = 204 if path == 'preview' else 404
        return None

    requests = [('GET', '/invoice'), ('POST', '/preview'), ('GET', '/missing')]
    entries, observed = [], []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture.invalid') as client:
        for method, path in requests:
            response = await client.request(method, path)
            entries.append({'request': {'method': method, 'url': str(response.request.url)},
                            'response': {'status': response.status_code}})
            observed.append((response.status_code, response.json() if response.content else None))
    return json.dumps({'log': {'version': '1.2', 'entries': entries}}).encode('utf-8'), observed


async def investigate(client: httpx.AsyncClient, path: str, artifact: bytes, mode: str = 'inspect') -> dict:
    result = (await client.post(path, params={'mode': mode}, content=artifact, headers=_UPLOAD)).raise_for_status().json()
    async with asyncio.timeout(20):
        while result['status'] in ('queued', 'running'):
            await asyncio.sleep(.01)
            result = (await client.get(f'{path}/{result["id"]}')).raise_for_status().json()
    if result['status'] != 'succeeded':
        raise RuntimeError(f'Fixture investigation failed with {result["error"]}')
    return result


def timing(samples: list[float]) -> dict[str, float]:
    return {'median_ms': round(statistics.median(samples) * 1000, 3),
            'p95_ms': round(sorted(samples)[math.ceil(len(samples) * .95) - 1] * 1000, 3)}


async def measure(client, base: str, artifact: bytes, rounds: int) -> dict:
    direct, integrated = {}, {}
    for mode in ('inventory', 'inspect'):
        warm = (await investigate(client, base, artifact, mode))
        (await client.delete(f'{base}/{warm["id"]}')).raise_for_status()
        direct_times, integrated_times = [], []
        for _ in range(rounds):
            start = time.perf_counter()
            analyze(artifact, 'zip', mode)
            direct_times.append(time.perf_counter() - start)
            start = time.perf_counter()
            result = await investigate(client, base, artifact, mode)
            integrated_times.append(time.perf_counter() - start)
            (await client.delete(f'{base}/{result["id"]}')).raise_for_status()
        direct[mode], integrated[mode] = timing(direct_times), timing(integrated_times)
    return {'rounds_per_mode': rounds, 'direct_parser': direct, 'integrated_api': integrated, 'model_tokens': 0}


def measure_rea(node: Path, cli: Path, fixture: Path, rounds: int) -> dict:
    samples, sizes, ids = [], [], set()
    for _ in range(rounds + 1):
        start = time.perf_counter()
        child = subprocess.run([str(node), str(cli), 'analyze-javascript-application', str(fixture), '--json'],
                               capture_output=True, check=True, timeout=20)
        samples.append(time.perf_counter() - start)
        result = json.loads(child.stdout)
        graph = result['normalized_result']['graph']
        assert 'invoice:total' in json.dumps(graph)
        ids.add(result['evidence_id'])
        sizes.append(len(child.stdout))
    return {**timing(samples[1:]), 'rounds': rounds, 'evidence_bytes': sizes[-1], 'distinct_evidence_ids': len(ids)}


def write_js_fixture(directory: Path) -> None:
    directory.mkdir()
    for name, source in JS_FILES.items():
        (directory / name).write_bytes(source.encode('utf-8'))


async def run(root: Path, args: argparse.Namespace) -> dict:
    settings = Settings(
        app_db_url=f'sqlite+aiosqlite:///{(root / "workspace.db").as_posix()}',
        data_dir=root / 'data', screenshot_dir=root / 'screenshots', profile_dir=root / 'profiles',
        browser_backend='fake', local_llm_enrichment_enabled=False, reverse_engineering_enabled=True,
        workspace_access_token=None, log_level='WARNING',
    )
    app = create_app(settings=settings)
    reference, expected = await capture()
    candidate, actual = await capture(candidate=True)
    mutant, mutated = await capture(candidate=True, mutated=True)
    assert expected == actual == [(200, {'total': 4620}), (204, None), (404, None)]
    assert mutated[1][0] == 202
    artifacts = {'javascript': js_archive(), 'protocol': reference, 'candidate': candidate, 'mutated': mutant}
    if args.zig:
        artifacts.update(compile_fixtures(args.zig.resolve(), root))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            session = (await client.post('/sessions', json={'title': 'Authorized synthetic investigations'})).raise_for_status().json()
            base = f'/sessions/{session["session_id"]}/investigations'
            results = {name: await investigate(client, base, artifact) for name, artifact in artifacts.items()}
            observations = results['javascript']['report']['observations']
            assert {row['key'] for row in observations if row['value'] == 'invoice:total'} == {'ipc_handler', 'ipc_invocation'}
            assert results['javascript']['report']['inferences']
            if args.node:
                oracle = subprocess.run(
                    [str(args.node), '-e', _NODE_ORACLE], input=json.dumps(JS_FILES), text=True,
                    capture_output=True, check=True, timeout=10,
                )
                assert json.loads(oracle.stdout) == {'value': 4200, 'channels': ['invoice:total']}
            for name in ('native', 'embedded'):
                if name in results:
                    report = results[name]['report']
                    assert len([row for row in report['observations'] if row['key'] == 'entry_instruction']) == 2
                    assert any('42' in inference['text'] for inference in report['inferences'])
            compare_url = f'{base}/{results["protocol"]["id"]}/compare'
            for name, expected_result in [('candidate', 'match'), ('mutated', 'difference')]:
                compared = (await client.post(compare_url, json={'other_id': results[name]['id']})).raise_for_status().json()
                assert compared['result'] == expected_result
            summary = {
                name: {
                    'bytes': len(artifacts[name]), 'sha256': hashlib.sha256(artifacts[name]).hexdigest(),
                    'observations': len(run['report']['observations']), 'inferences': len(run['report']['inferences']),
                    'evidence_id': run['evidence_id'],
                } for name, run in results.items()
            }
            summary['oracles'] = {'protocol_responses': len(actual), 'protocol_mutation_detected': True,
                                  'node_ipc_contract_fixture': bool(args.node)}
            if args.rounds:
                summary['benchmark'] = await measure(client, base, artifacts['javascript'], args.rounds)
            if args.rea_cli:
                fixture = root / 'invoice-js'
                write_js_fixture(fixture)
                summary['standalone_rea'] = measure_rea(args.node.resolve(), args.rea_cli.resolve(), fixture, args.rounds or 5)
            return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zig', type=Path, help='Optional path to Zig for the two stripped ELF fixtures.')
    parser.add_argument('--node', type=Path, help='Optional Node executable for the self-authored IPC contract oracle.')
    parser.add_argument('--rea-cli', type=Path, help='Optional path to a separately installed REA CLI entry script.')
    parser.add_argument('--export-js', type=Path, help='Create a new directory with the JavaScript fixture files.')
    parser.add_argument('--rounds', type=int, default=0, choices=range(0, 51), metavar='0..50')
    args = parser.parse_args()
    if args.rea_cli and not args.node:
        parser.error('--rea-cli requires --node')
    if args.export_js:
        write_js_fixture(args.export_js)
    with TemporaryDirectory(prefix='th-re-demo-') as path:
        print(json.dumps(asyncio.run(run(Path(path), args)), indent=2))


if __name__ == '__main__':
    main()
