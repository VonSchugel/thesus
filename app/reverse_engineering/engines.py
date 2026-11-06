from __future__ import annotations

import hashlib
import json
import re
import stat
from collections import defaultdict
from io import BytesIO
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from app.reverse_engineering.contracts import EngineFailure, EngineReport, Inference, Locator, MAX_OBSERVATIONS, Observation


_METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'TRACE', 'CONNECT'})
_JS_LITERAL = re.compile(rb"""(['"])([A-Za-z0-9_./:@+-]{1,96})\1\Z""")
_JS_CALLS = {
    b'ipcMain.handle': 'ipc_handler',
    b'ipcRenderer.invoke': 'ipc_invocation',
    b'contextBridge.exposeInMainWorld': 'bridge_export',
    b'require': 'import_specifier',
}
_PE_ARCH = {0x014c: 'x86', 0x8664: 'x86_64', 0xaa64: 'aarch64'}
_ELF_ARCH = {'EM_386': 'x86', 'EM_X86_64': 'x86_64', 'EM_AARCH64': 'aarch64', 'EM_ARM': 'arm'}
_JS_BYTES = 128 * 1024
_ZIP_MEMBERS = 40


def _observation(key: str, value, **locator) -> Observation:
    return Observation(key=key, value=value, locator=Locator(**locator))


def _decode_entry(report: EngineReport, artifact: bytes, arch: str, entry: int, offset: int, size: int) -> None:
    from capstone import CS_ARCH_ARM64, CS_ARCH_X86, CS_MODE_32, CS_MODE_64, CS_MODE_LITTLE_ENDIAN, Cs

    modes = {'x86': (CS_ARCH_X86, CS_MODE_32), 'x86_64': (CS_ARCH_X86, CS_MODE_64),
             'aarch64': (CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN)}
    if arch not in modes:
        report.limitations.append('Entry disassembly is unavailable for this architecture or byte order.')
        return
    decoder = Cs(*modes[arch])
    first = len(report.observations)
    decoded = []
    for address, length, mnemonic, operand in decoder.disasm_lite(artifact[offset:offset + min(size, 192)], entry, 24):
        decoded.append((mnemonic, operand))
        report.observations.append(_observation(
            'entry_instruction', {'address': address, 'mnemonic': mnemonic, 'operand': operand},
            offset=offset + address - entry, length=length,
        ))
        if mnemonic in {'ret', 'retf', 'jmp', 'b', 'br', 'eret', 'ud2'}:
            break
    report.limitations.append('Entry decoding is linear over file-backed bytes. Function boundaries and runtime behavior were not established.')
    if len(decoded) != 2 or decoded[1] != ('ret', '') or decoded[0][0] != 'mov':
        return
    operand = decoded[0][1]
    register = 'w0' if arch == 'aarch64' else 'eax'
    match = re.fullmatch(rf'{register}, #?(0x[0-9a-f]+|[0-9]+)', operand)
    if match:
        constant = int(match.group(1), 0)
        report.inferences.append(Inference(
            text=f'The decoded entry sequence places {constant} in {register} and returns if reached as shown.',
            observations=[first, first + 1],
        ))


def _pe(artifact: bytes, mode: str) -> EngineReport:
    import pefile

    try:
        image = pefile.PE(data=artifact, fast_load=True)
    except pefile.PEFormatError as exc:
        raise EngineFailure('invalid_artifact') from exc
    try:
        header, optional = image.FILE_HEADER, image.OPTIONAL_HEADER
        arch = _PE_ARCH.get(header.Machine, 'unknown')
        rva = optional.AddressOfEntryPoint
        directories = optional.DATA_DIRECTORY
        managed = len(directories) > 14 and bool(directories[14].VirtualAddress and directories[14].Size)
        report = EngineReport(observations=[
            _observation('pe_header', {
                'architecture': arch, 'machine': header.Machine, 'entry_rva': rva, 'image_base': optional.ImageBase,
                'clr_directory_present': managed, 'section_count': header.NumberOfSections,
                'coff_symbol_count': header.NumberOfSymbols,
            }, offset=header.get_file_offset(), length=header.sizeof() + header.SizeOfOptionalHeader),
        ])
        candidates = []
        for section in image.sections:
            start, raw, available = section.VirtualAddress, section.PointerToRawData, section.SizeOfRawData
            size = min(available, section.Misc_VirtualSize or available)
            executable = bool(section.Characteristics & 0x20000000)
            if len(report.observations) <= 32:
                report.observations.append(_observation(
                    'pe_section', {'rva': start, 'raw_offset': raw, 'raw_size': available, 'executable': executable},
                    offset=section.get_file_offset(), length=section.sizeof(),
                ))
            if executable and start <= rva < start + size and raw > 0 and raw + size <= len(artifact):
                candidates.append((raw + rva - start, size - (rva - start)))
        if len(image.sections) > 32:
            report.limitations.append('Only the first 32 section headers are shown.')
        if managed:
            report.limitations.append('The image declares a CLR directory. Managed IL and native bootstrap code are outside this report.')
        elif mode == 'inspect' and rva:
            if len(candidates) == 1:
                offset, size = candidates[0]
                _decode_entry(report, artifact, arch, optional.ImageBase + rva, offset, size)
            else:
                report.limitations.append('The entry RVA has no unique executable, file-backed section.')
        return report
    finally:
        image.close()


def _elf(artifact: bytes, mode: str) -> EngineReport:
    from elftools.common.exceptions import ELFError
    from elftools.elf.elffile import ELFFile

    try:
        image = ELFFile(BytesIO(artifact))
        header = image.header
        count = image.num_segments()
        if count > 128:
            raise EngineFailure('input_limit')
        arch = _ELF_ARCH.get(header.e_machine, 'unknown')
        entry = header.e_entry
        report = EngineReport(observations=[
            _observation('elf_header', {
                'architecture': arch, 'bits': image.elfclass, 'little_endian': image.little_endian,
                'type': header.e_type, 'entry': entry, 'program_headers': count, 'section_headers': header.e_shnum,
            }, offset=0, length=64 if image.elfclass == 64 else 52),
        ])
        candidates = []
        load_count = 0
        for index in range(count):
            segment = image.get_segment(index)
            if segment['p_type'] != 'PT_LOAD':
                continue
            load_count += 1
            start, raw = segment['p_vaddr'], segment['p_offset']
            size = min(segment['p_filesz'], segment['p_memsz'])
            executable = bool(segment['p_flags'] & 1)
            if len(report.observations) <= 32:
                report.observations.append(_observation(
                    'elf_segment', {'virtual_address': start, 'raw_offset': raw,
                                    'raw_size': segment['p_filesz'], 'executable': executable},
                    offset=header.e_phoff + index * header.e_phentsize, length=header.e_phentsize,
                ))
            if executable and start <= entry < start + size and raw + size <= len(artifact):
                candidates.append((raw + entry - start, size - (entry - start)))
        if load_count > 32:
            report.limitations.append('Only the first 32 load segment headers are shown.')
        if mode == 'inspect' and entry:
            if len(candidates) == 1 and image.little_endian:
                offset, size = candidates[0]
                _decode_entry(report, artifact, arch, entry, offset, size)
            else:
                report.limitations.append('No unique supported executable, file-backed mapping was established for the entry.')
        return report
    except (ELFError, ValueError) as exc:
        raise EngineFailure('invalid_artifact') from exc


def _js_parser():
    import tree_sitter_javascript
    from tree_sitter import Language, Parser

    return Parser(Language(tree_sitter_javascript.language()))


def _js_literal(artifact: bytes, node) -> str | None:
    if node is not None and node.type == 'string' and node.end_byte - node.start_byte <= 98:
        match = _JS_LITERAL.fullmatch(artifact[node.start_byte:node.end_byte])
        if match:
            return match.group(2).decode('ascii')
    return None


def _scan_javascript(parser, report: EngineReport, artifact: bytes, member: int | None = None) -> None:
    tree = parser.parse(artifact)
    if tree.root_node.has_error and 'JavaScript syntax was incomplete or unsupported.' not in report.limitations:
        report.limitations.append('JavaScript syntax was incomplete or unsupported.')
    pending = [tree.root_node]
    visited = 0
    while pending and visited < 10000 and len(report.observations) < MAX_OBSERVATIONS:
        node = pending.pop()
        visited += 1
        pending.extend(reversed(node.named_children))
        key, literal = None, None
        if node.type in ('import_statement', 'export_statement'):
            key, literal = 'import_specifier', node.child_by_field_name('source')
        elif node.type == 'call_expression':
            function = node.child_by_field_name('function')
            if function is not None and function.end_byte - function.start_byte <= 48:
                key = _JS_CALLS.get(artifact[function.start_byte:function.end_byte])
            if key:
                args = node.child_by_field_name('arguments')
                if args is not None:
                    literal = next((child for child in args.named_children if child.type != 'comment'), None)
        value = _js_literal(artifact, literal)
        if key and value is not None:
            report.observations.append(_observation(key, value, offset=literal.start_byte,
                                                    length=literal.end_byte - literal.start_byte, member=member))
    if pending and 'JavaScript scan reached its observation or syntax-node limit.' not in report.limitations:
        report.limitations.append('JavaScript scan reached its observation or syntax-node limit.')


def _ipc_inferences(report: EngineReport) -> None:
    endpoints = defaultdict(lambda: defaultdict(list))
    for index, item in enumerate(report.observations):
        if item.key in ('ipc_handler', 'ipc_invocation'):
            endpoints[item.value][item.key].append(index)
    for value, channel in endpoints.items():
        handlers, invocations = channel['ipc_handler'], channel['ipc_invocation']
        if len(handlers) == len(invocations) == 1 and len(report.inferences) < 32:
            report.inferences.append(Inference(
                text=f'Literal Electron-style IPC channel {value} has both a registration and an invocation in the scanned source.',
                observations=[handlers[0], invocations[0]],
            ))


def _javascript(artifact: bytes, mode: str) -> EngineReport:
    if len(artifact) > _JS_BYTES:
        raise EngineFailure('input_limit')
    report = EngineReport(limitations=['The syntax scan does not resolve aliases, dynamic channels, or runtime control flow.'])
    if mode == 'inspect':
        _scan_javascript(_js_parser(), report, artifact)
        _ipc_inferences(report)
    else:
        report.observations.append(_observation('source_size', len(artifact), offset=0, length=len(artifact)))
    return report


def _zip(artifact: bytes, mode: str) -> EngineReport:
    try:
        with ZipFile(BytesIO(artifact)) as archive:
            members = archive.infolist()
            report = EngineReport(observations=[_observation('archive_members', len(members))],
                                  limitations=['Members were inspected in memory. Nested archives and unpacked sidecars were not inspected.'])
            parser = _js_parser() if mode == 'inspect' else None
            parsed = 0
            for index, info in enumerate(members[:_ZIP_MEMBERS]):
                if len(report.observations) >= MAX_OBSERVATIONS:
                    report.limitations.append('The archive observation limit was reached.')
                    break
                name = ''.join(character if character.isprintable() else '?' for character in info.filename[:180])
                report.observations.append(_observation(
                    'archive_member', {'name': name, 'size': info.file_size, 'compressed_size': info.compress_size},
                    member=index,
                ))
                if parser is None or not info.filename.lower().endswith(('.js', '.mjs', '.cjs')):
                    continue
                if (parsed >= 4 or info.file_size > _JS_BYTES or info.flag_bits & 1
                        or stat.S_ISLNK(info.external_attr >> 16) or info.file_size > 128 * max(1, info.compress_size)):
                    warning = 'Some JavaScript members were skipped by the size, compression, type or member-count limits.'
                    if warning not in report.limitations:
                        report.limitations.append(warning)
                    continue
                parsed += 1
                with archive.open(info) as member:
                    content = member.read(_JS_BYTES + 1)
                if len(content) > _JS_BYTES:
                    raise EngineFailure('input_limit')
                _scan_javascript(parser, report, content, member=index)
            if len(members) > _ZIP_MEMBERS:
                report.limitations.append('Only the first 40 members were considered.')
            if mode == 'inspect':
                report.limitations.append('IPC findings are lexical matches. Dynamic bindings and runtime execution were not established.')
                _ipc_inferences(report)
            return report
    except (BadZipFile, NotImplementedError, RuntimeError, ValueError) as exc:
        raise EngineFailure('invalid_artifact') from exc


def _har(artifact: bytes, _mode: str) -> EngineReport:
    try:
        document = json.loads(artifact)
        entries = document['log']['entries']
        if not isinstance(entries, list):
            raise TypeError
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise EngineFailure('invalid_artifact') from exc

    observed = []
    for index, entry in enumerate(entries[:MAX_OBSERVATIONS - 1]):
        try:
            method, status = entry['request']['method'], entry['response']['status']
            url = urlsplit(entry['request']['url'])
            if method not in _METHODS or type(status) is not int or not 100 <= status <= 599:
                continue
            if url.scheme not in ('http', 'https') or not url.netloc:
                continue
            digest = hashlib.sha256((url.path or '/').encode('utf-8')).hexdigest()
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        observed.append(_observation('http_exchange', {'method': method, 'path_sha256': digest, 'status': status}, record=index))
    return EngineReport(
        observations=[_observation('har_coverage', {'total': len(entries), 'observed': len(observed)}), *observed],
        limitations=['Only valid method, exact URL path digest and HTTP status are retained. Hosts, queries, headers, bodies and timing are outside the evidence.'],
    )


_ENGINES = {'pe': _pe, 'elf': _elf, 'zip': _zip, 'javascript': _javascript, 'har': _har}


def analyze(artifact: bytes, target: str, mode: str) -> EngineReport:
    engine = _ENGINES.get(target)
    if engine is None or mode not in ('inventory', 'inspect'):
        raise EngineFailure('unsupported_artifact')
    return engine(artifact, mode)
