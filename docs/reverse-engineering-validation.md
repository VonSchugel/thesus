# THESUS reverse-engineering validation

The subsystem accepts explicitly authorized local artifacts through the THESUS API. Its automatic route selects a bounded static investigation, and its review route saves the plan before the parser starts. It returns machine-readable observations, supported inferences and evidence locations. The [architecture and decision record](reverse-engineering.md) explains the integration and its limits.

## Reproduce the fixtures

Use the Python 3.12 environment described in the [workspace setup](../README.md#start-on-windows). Install the optional investigation dependencies into that environment.

```powershell
& $workspacePython -m pip install -r requirements-reverse-engineering.txt
& $workspacePython -m pytest -q tests/test_reverse_engineering.py
& $workspacePython -m scripts.demo_reverse_engineering
```

The demo uses a disposable SQLite database and the fake browser. It builds its own inputs. Add `--node` with an installed Node executable for the independent IPC contract oracle. Add `--zig` with a Zig 0.15.2 executable to compile the two stripped ELF targets. The compiler output is only read as data. Add `--rea-cli` with the entry script of a separately staged `rea-agents@6.8.0` to benchmark standalone REA on the same JavaScript files. The benchmark calls the static CLI directly and requires `--node`.

```powershell
& $workspacePython -m scripts.demo_reverse_engineering --node $nodePath --zig $zigPath --rea-cli $reaCliPath --rounds 12
```

## Demonstrations

All targets were authored for this evaluation. The checks below ran on Windows 11, Python 3.12.14, Node 24.14.0, and Zig 0.15.2 with the pinned optional dependencies. The input digests let another run verify that it used the same fixtures. The benchmark concerns these small controlled targets.

| Investigation and procedure | Evidence and independently specified oracle | Observed result and limit |
| --- | --- | --- |
| **Stripped native program.** Compile C returning 42 with Zig for x86-64 ELF. Strip symbols, use the named entry at link time and request inspect mode. | ELF header, two load segments and two entry instructions with file offsets. Compare the decoded immediate and return instruction with the self-authored C contract. | Five observations and one inference about `eax`. Artifact 1,080 bytes, SHA-256 `c5b2e94db4a5ded0c8589b515543274d1d074dfd61e325b01a0669026b602fed`. The binary was never launched. The inference applies to the shown instructions. |
| **Embedded target.** Compile the same source for freestanding AArch64 ELF, with no standard library. Resolve the entry through a load segment. | Validate AArch64 and the `mov w0, #0x2a` and `ret` sequence against the source oracle. | Five observations and one inference about `w0`. Artifact 1,064 bytes, SHA-256 `15cd69243d3460c19483e3b52d2daa08d1c5b5775b5fc0f3342ea14548caa836`. This is a freestanding program fixture. Flash dumps, extraction and device runtime remain separate. |
| **Electron-style feature tracing.** Inspect a ZIP containing an invoice feature split across main, preload and renderer source. Independently run those self-authored fixture strings in separate Node VM contexts with stubbed IPC and bridge APIs. | Static evidence locates the literal handler and invocation and the bridge export. The oracle exercises the fixture renderer and returns 4,200 cents through the simulated bridge. | Ten observations and one candidate IPC link. ZIP SHA-256 `9dafe0dd9a1dd03be45d9ac4ba5d281626194b6afe3ed5ba43f35f5266504fc9`. The oracle covers the fixture contract. It does not establish real Electron runtime behavior or sandboxing. |
| **API reconstruction.** Send three fixed ASGI HTTP requests to a self-authored reference and a separately authored compatible implementation. Produce sanitized HAR records. Change one candidate response to detect a regression. | The HTTP harness compares responses including the invoice body. The THESUS comparison checks method, exact path digest, status and multiplicity. It marks incomplete captures inconclusive. | Three independent response checks passed. Reference and candidate captures matched. The preview mutation from 204 to 202 produced a difference. The evidence covers the exercised request surface, with bodies checked only in the separate fixture oracle. |

The tests also exercise authorization and same-origin enforcement, bounded upload, session isolation, request-ID retries, stable evidence after restart, plan review, cancellation, recovery from a recorded interrupted claim, version-change rejection, worker deadlines, output limits and child reaping. A real MCP stdio client discovered the single read tool, retrieved retained evidence through the token-protected workspace, and received an error for the wrong session. [Integration tests](../tests/test_reverse_engineering.py)

Two additional offline PE parser spot checks used self-authored sources. Zig compiled the constant-return C fixture for `x86_64-windows-gnu` with the entry symbol selected at link time and symbols stripped. The parser observed a zero COFF symbol count and inferred the expected 42 in `eax`. Windows' .NET Framework C# compiler built a 3,072-byte library with a static method returning 42. The parser reported a declared CLR directory and correctly withheld a native entry inference. Neither file was launched. Managed IL recovery remains a future use case.

The final full suite passed **98 tests in 168.04 seconds**, including the six investigation tests, from a disposable source copy without workspace credentials or data. The run used Chromium 145.0.7632.6 through `BROWSER_EXECUTABLE_PATH` and a short Windows temporary directory. This includes the existing browser controls and artifact tests under that directory configuration. [Earlier Windows path sensitivity](modularity-validation.md#baseline-rerun)

## Measurements

There was one warm-up per mode and 12 measured sequential runs per mode. Direct parser timing uses the exact same synthetic ZIP in the calling process. The integrated timing begins at the upload API and ends when status polling obtains the retained result. It includes SQLite, process startup and polling. The REA trial uses a fresh Node process per measured call and an extracted tree of the same four source files. Host background work varied during this small run. Report the figures as local observations, without extrapolating to arbitrary targets or model workloads.

| Workload | Median | p95 | Result |
| --- | ---: | ---: | --- |
| Direct ZIP inventory | 0.192 ms | 2.172 ms | Member metadata baseline |
| Direct ZIP inspection | 1.104 ms | 12.960 ms | Bounded JavaScript syntax and IPC findings |
| THESUS integrated ZIP inventory | 446.117 ms | 681.617 ms | Retained archive inventory |
| THESUS integrated ZIP inspection | 550.930 ms | 647.280 ms | Retained typed observations and inference |
| Standalone REA 6.8.0 JavaScript application analysis | 5,379.565 ms | 8,386.267 ms | Full application and semantic graph with 170,972 output bytes |

Both static inspection paths found the target channel. REA also produced a source-located candidate from the exposed `invoice.total` bridge to the channel and returned a richer graph. Its evidence ID was stable across the warm-up and measured runs on that fixture. The tested Node 24.14.0 executable was below the npm package's declared Node 24 minimum, although the selected CLI command ran successfully. The timings therefore describe this exact fixture and environment. They do not establish a supported REA deployment or equivalent workload coverage. None of these measurements invoked a model or spent tokens. [REA CLI release](https://www.npmjs.com/package/rea-agents/v/6.8.0)

Use the low-overhead inventory path when the question is only what the archive contains. Use inspection for source-located evidence. At this scale the integrated boundary dominates the parser cost. A persistent pool could reduce startup cost, with cross-run state, authority and crash cleanup as acceptance gates. Measure those gates before adopting pooling or an unscoped result cache.

The ELF engine originally parsed the load segments once for a count and again for evidence. The implementation now counts them during evidence collection. The before and after reports matched exactly on both ELF test artifacts in both modes. In alternating batches of 200 calls across the two artifacts, with two warm-up and 12 measured batches per mode, median parser time fell from 814 to 721 µs for inventory and from 1,521 to 1,270 µs for inspection. This isolated parser comparison does not estimate the change in the integrated route.

A separate resource sample polled the demo process and its children every 20 ms while running the JavaScript and HAR fixtures and two timing rounds per mode. Across 367 samples, peak combined resident working set was 134.9 MiB, and the last observed user plus system CPU totals summed to 13.156 seconds. These are sampled process-tree figures. Working sets may count shared pages more than once, and sampling can miss work between process exit and the final poll.

## Next acceptance gates

| Research gap | Smallest defensible test |
| --- | --- |
| Broader native, managed, firmware and packaged applications | Match Ghidra and an alternative native engine on function boundaries in held-out stripped builds. Validate one compiled managed assembly with ILSpy, an APK with JADX, an ASAR with unpacked sidecars, and one redistributable firmware image in separate controlled workers. |
| External MCP engine and runtime observation | Bind one reviewed read-only tool schema and target digest in an isolated worker. Deny project mutation, arbitrary paths and network egress. Fault-test shutdown and evidence export with the provider absent. Use a separately approved runtime lane for an owned target and specified actions. |
| Agentic investigation | Add a proposal-only planner to a held-out task subset. Score evidence citations, verified conclusions and harmful or unsupported suggestions against the deterministic baseline. Budget calls and wall time and retain human authority for actions with external effects. |
| Long-running runs, parallelism and verification | Run duplicate submissions, kill the process at every durable transition, and compare results and queue fairness at concurrency one and two. Add evidence paging and persistent workers only if representative measurements justify them. |
