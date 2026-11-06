# Watch reload memory retention: local reproduction and evidence

This investigation used Zigflow `v0.14.2`. All runs were local. The original
workflow corpus contained 955 files and is intentionally **not** included here.
It may contain private workflow definitions. The measurements and heap profiles
contain no workflow file contents. The corrected build in the comparison used
the same code as `v0.14.2` plus test-only deletion of the stopped worker's
entries from `activityRegistry`; the production change in this branch applies
that deletion to the worker lifecycle, including failed builds, health-check
setup, and starts.

## What was measured

`original-samples.csv` is the raw output of a local Docker run on 4 November
2026. A workflow file was rewritten in place, then `reloader.yaml` was rewritten
two seconds later. Each write triggered a watch reload. There were 11 pairs of
writes, sampled before and after every write and every five seconds for 40
seconds. Each row has a UTC timestamp and phase. The data came from:

- `/proc/1/status` `VmRSS`, multiplied by 1024 to get bytes. This is the
  resident memory of the Zigflow process inside its own container, not a pod
  total or sidecar measurement.
- `http://127.0.0.1:9090/metrics` inside the same container:
  `go_memstats_heap_alloc_bytes`, `go_memstats_heap_objects`,
  `go_memstats_sys_bytes`, `go_memstats_heap_released_bytes`, and
  `go_gc_duration_seconds_count`.

The first row (22:14:47 UTC) had 133.55 MiB RSS, 47.24 MiB live Go heap, and
390 completed GC cycles. The final row (22:24:17 UTC) had 1722.13 MiB RSS,
824.19 MiB live Go heap, and 952 completed GC cycles. RSS rose by 1588.59 MiB
and live Go heap by 776.95 MiB while GC continued to run. RSS is not expected
to equal live heap: it includes other runtime allocations and pages retained by
the allocator. The CSV by itself does not identify an allocation site.

The four `.heap.pb.gz` files are raw Go heap profiles captured from a local
profiling build through `/debug/pprof/heap?gc=1`. That query forces a GC
before profiling, so comparisons focus on objects still live after GC.
The profile type is `inuse_space`, not cumulative allocation (`alloc_space`).
They came from two independent three-reload-pair runs:

| Build | Initial live heap | After three pairs | Net change |
| --- | ---: | ---: | ---: |
| Unmodified `v0.14.2` | 44.66 MiB | 250.16 MiB | +205.50 MiB |
| Test-only registry cleanup | 42.47 MiB | 42.03 MiB | -0.44 MiB |

The profiler's baseline-subtracted **positive** allocations total 211.84 MiB
for the unmodified build and 8.97 MiB for the cleanup build. Positive deltas
can coexist with frees elsewhere, so the net changes above are the direct
comparison of total live heap. In the unmodified profile, the largest positive
allocation sites include `reflect.mapassign_faststr0` (95.53 MiB) and
`encoding/json/v2.makeString` (44.59 MiB). These are allocation sites inside
workflow parsing/building. A heap profile shows where still-live objects were
allocated; it does **not** identify which reference keeps them alive. The
registry-key retention is established by code inspection and by the controlled
cleanup intervention, then covered by the regression tests.

## Reproduce the sampling locally

Use a local Temporal dev server on port 7233 and a local Docker container.
Do not point the worker at any remote Temporal address. Generate a synthetic
corpus if the real one is unavailable. The template below creates valid,
minimal workflow definitions and keeps all files on your machine:

```sh
mkdir -p /tmp/zigflow-watch-memory/workflows
python3 - <<'PY'
from pathlib import Path
directory = Path('/tmp/zigflow-watch-memory/workflows')
for i in range(955):
    (directory / f'workflow-{i:04}.yaml').write_text(f'''document:
  dsl: 1.0.0
  taskQueue: zigflow-memory-repro
  workflowType: memoryRepro{i:04}
  version: 1.0.0
do:
  - request:
      call: http
      with:
        method: get
        endpoint: https://example.invalid/
''')
(directory / 'reloader.yaml').write_text('''document:
  dsl: 1.0.0
  taskQueue: zigflow-memory-repro
  workflowType: memoryReloader
  version: 1.0.0
do:
  - noop:
      set:
        reloaded: true
''')
PY
go run . validate /tmp/zigflow-watch-memory/workflows/workflow-0000.yaml \
  --disable-telemetry
```

Build either the unmodified tag or this branch for Linux. To compare builds,
hold the base revision fixed: compare `v0.14.2` with the tag-based cleanup
experiment, or compare current upstream `main` with this branch. The saved
profiles are from the first comparison. For heap profiles,
add the following temporary local-only instrumentation to `main.go` before
building; it is **not** part of the fix or production image:

```diff
 import (
+    "net/http"
+    _ "net/http/pprof"
     "github.com/zigflow/zigflow/cmd"
 )
 func main() {
+    go func() { _ = http.ListenAndServe("127.0.0.1:6060", nil) }()
     cmd.Execute()
 }
```

```sh
GOOS=linux GOARCH=amd64 go build -o /tmp/zigflow-watch-memory/zigflow .
cat > /tmp/zigflow-watch-memory/Dockerfile <<'EOF'
FROM alpine:latest
COPY zigflow /usr/local/bin/zigflow
ENTRYPOINT ["/usr/local/bin/zigflow"]
EOF
docker build -t zigflow-watch-memory:local /tmp/zigflow-watch-memory
docker run --rm --name zigflow-watch-memory \
  --add-host=host.docker.internal:host-gateway \
  -v /tmp/zigflow-watch-memory/workflows:/workflows \
  zigflow-watch-memory:local run --dir=/workflows --watch \
  --temporal-address=host.docker.internal:7233 \
  --temporal-namespace=default --disable-telemetry
```

In another terminal, check that the container is running and that reloads
actually complete before interpreting memory measurements:

```sh
docker logs -f zigflow-watch-memory 2>&1 | \
  grep --line-buffered -E 'Watch: (reloading workers|workers reloaded successfully)'
```

Capture the baseline profile and sample the same pair of file rewrites used
in the CSV. The sampling script calls only `docker exec` on the named local
container and writes only the two bind-mounted local files supplied to it:

```sh
docker exec zigflow-watch-memory wget -qO- \
  'http://127.0.0.1:6060/debug/pprof/heap?gc=1' > baseline.heap.pb.gz
python3 investigations/watch-reload-memory/sample_local.py \
  --container zigflow-watch-memory \
  --workflow /tmp/zigflow-watch-memory/workflows/workflow-0000.yaml \
  --reloader /tmp/zigflow-watch-memory/workflows/reloader.yaml \
  --cycles 3 --output /tmp/zigflow-watch-memory/samples.csv
docker exec zigflow-watch-memory wget -qO- \
  'http://127.0.0.1:6060/debug/pprof/heap?gc=1' > after-three.heap.pb.gz
go tool pprof -top -inuse_space -base baseline.heap.pb.gz after-three.heap.pb.gz
```

The local Go installation used for this investigation did not bundle
`go tool pprof`, so the standalone `github.com/google/pprof` executable was
used with equivalent flags. Synthetic one-step workflows are expected to
produce a smaller absolute memory change than the original 955-file corpus;
they test the same watch reload and registration path. Run the same corpus,
cycle count, and environment on both builds for a fair comparison. The
profiles here used the private corpus; its absence limits exact reproduction
of their numeric values by outside maintainers.

## Inspect the saved profiles

From this directory, with `pprof` installed:

```sh
pprof -top -inuse_space -base launch-stop-baseline.heap.pb.gz \
  launch-stop-after-three.heap.pb.gz
pprof -top -inuse_space -base clear-registry-baseline.heap.pb.gz \
  clear-registry-after-three.heap.pb.gz
```

The checksums of the saved profiles, in the same order, are:

```text
cae902a4373d31a759a02790e668596a42392ab08adc954ed44d9241f543058f
88445da98aabf9ebe98c55e7639ae31febbfed648d8882789113b381d1670f49
0243de52f516b14ef79445bad1ad04b71e93af052b4bbfc3d1dcf429194145a6
ead9f19e32e7e21bf2b9db97503bba91d953b82a3c07fa2dd4ef2470486ff66e
```

## Proposed fix and limits

`registerActivityOnce` stores `(worker, activity name)` as a key in a global
`sync.Map`. That key holds a strong reference to the worker. Before the fix,
watch reloads stopped old workers but left their keys in the map. Repeated
reloads could therefore retain the old Temporal worker and its registered
workflow data even after GC. The branch deletes keys for stopped workers and
for workers abandoned during build, health-check, or start errors. It leaves
keys belonging to the active worker intact, so duplicate activity registration
is still avoided.

The local intervention is strong evidence for this code-path cause. It does
not prove that every production byte was retained by this registry, or that
the private corpus and synthetic corpus have identical memory behaviour.
No production pod was profiled: the production binary was stripped and had no
accessible Go heap profiling endpoint. Production observations were Go metrics
and `/proc` snapshots; allocation-site profiles came from the local build.
