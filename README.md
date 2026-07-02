# rpm-fetch

Warm an [Artifactory](https://jfrog.com/artifactory/) cache from an RPM
repository's own metadata. `rpm-fetch` reads a yum/dnf repo's `repodata`,
enumerates **every** package, and fires concurrent `HEAD` (or `GET`) requests at
the corresponding Artifactory paths so the cache is populated before clients ask
for the packages.

- **Zero dependencies.** Pure Python standard library.
- **All repodata codecs.** gzip, xz, bzip2, and zstd — the last via the stdlib
  `compression.zstd` module added in **Python 3.14** (hence the 3.14 floor).
- **Streaming metadata parse.** `primary.xml` is parsed incrementally, so a
  full-distro repo with hundreds of thousands of packages stays memory-flat.
- **Metadata warmed too.** The repo's own `repodata/` files (repomd.xml,
  filelists, updateinfo, comps, ...) are warmed ahead of the packages — they are
  the first thing a dnf client asks for.

## Requirements

- Python **3.14+** (for `compression.zstd`).
- No third-party packages.

## Install

```bash
# Run straight from a checkout, no install needed (run from the repo root):
python -m rpm_fetch --help

# Or install the console script into the bundled virtualenv:
source .venv/bin/activate
pip install .          # provides the `rpm-fetch` command
```

## Usage

```bash
# Bearer token is the default auth; keep the secret in the environment.
export ARTIFACTORY_TOKEN=…

rpm-fetch https://artifactory.example.com/artifactory/rpm-remote/

# Equivalently, without installing:
python -m rpm_fetch https://artifactory.example.com/artifactory/rpm-remote/
```

### Authentication

Pass a flag or set the env var (env vars are preferred so secrets stay out of
shell history and `ps` output). Precedence: token → basic → API key.

| Method      | Flag(s)            | Env var(s)                              |
| ----------- | ------------------ | --------------------------------------- |
| Bearer      | `--token`          | `ARTIFACTORY_TOKEN`, `RPM_FETCH_TOKEN`  |
| Basic       | `--user`, `--password` | `ARTIFACTORY_PASSWORD`, `RPM_FETCH_PASSWORD` |
| API key     | `--api-key`        | `ARTIFACTORY_API_KEY`, `RPM_FETCH_API_KEY` |
| Anonymous   | *(none)*           | —                                       |

### Common options

| Option                 | Default | Purpose                                                       |
| ---------------------- | ------- | ------------------------------------------------------------- |
| `--method {HEAD,GET}`  | `HEAD`  | Request method per package (see note below).                  |
| `--concurrency N`      | `16`    | Maximum in-flight requests.                                   |
| `--timeout SECS`       | `30`    | Per-request timeout.                                          |
| `--retries N`          | `2`     | Retries on `429`/`5xx`/network errors (honours `Retry-After`, else jittered exponential backoff). |
| `--insecure`           | off     | Skip TLS verification (internal CAs / self-signed certs).     |
| `--metadata-base-url`  | repo URL| Read repodata from a different base than the warm target.     |
| `--limit N`            | —       | Only process the first N URLs (metadata first; handy for a smoke test). |
| `--dry-run`            | off     | Print the package URLs that *would* be warmed, then exit.     |
| `--fail-fast`          | off     | Stop scheduling new requests after the first failure.         |
| `--verbose` / `--quiet`| —       | Per-request logging / summary-only.                           |

Exit codes: `0` all warmed, `1` some requests failed, `2` metadata could not be
read.

### A note on HEAD vs GET

`HEAD` is the default and the lightest way to nudge Artifactory. Be aware that
for some remote-repository configurations a `HEAD` only resolves/refreshes
metadata, while the artifact bytes are not downloaded and stored until a `GET`.
If you need the **content** cached (not just the path known), use `--method GET`
— `rpm-fetch` drains the body so Artifactory fully fetches and stores each file.

Point `rpm-fetch` at the directory that actually contains `repodata/` — for a
distro mirror that is the arch/os path (e.g.
`…/BaseOS/x86_64/os/`), not the repository root.

## Docker

The image is just CPython 3.14 plus this package (no third-party layers), runs as
an unprivileged user, and accepts the same arguments as the CLI.

```bash
docker build -t rpm-fetch .

# Pass the token from your environment — it is never baked into the image.
export ARTIFACTORY_TOKEN=…
docker run --rm -e ARTIFACTORY_TOKEN rpm-fetch \
  https://artifactory.example.com/artifactory/rpm-remote/BaseOS/x86_64/os/

# No arguments → prints --help.
docker run --rm rpm-fetch
```

Everything after the image name is forwarded straight to `rpm-fetch`, so every
flag above works unchanged (`--method GET`, `--concurrency 32`, `--dry-run`, …).

## How it works

```
<repo>/repodata/repomd.xml          → locate primary metadata
        primary.xml.{gz,xz,bz2,zst}  → stream + decompress
            <package><location href> → urljoin against <repo> → HEAD/GET
```

Artifactory often answers metadata/artifact requests with a 302 to a presigned
S3/CDN URL. `rpm-fetch` follows the redirect but **drops your token on the
cross-host hop** — otherwise the presigned URL's own auth collides with the
header and the backend returns `400 Bad Request` ("only one auth mechanism
allowed"). No configuration needed; this is automatic.

## Development

```bash
# Run the test suite (stdlib unittest — no pytest required):
python -m unittest discover -s tests -v
```

Layout:

| Path                          | Responsibility                                       |
| ----------------------------- | ---------------------------------------------------- |
| `rpm_fetch/cli.py`        | Argument parsing, orchestration, reporting.          |
| `rpm_fetch/repodata.py`   | repomd/primary parsing → list of package URLs.        |
| `rpm_fetch/decompress.py` | Extension-based streaming decompression.             |
| `rpm_fetch/http.py`       | urllib client: auth, retries, HEAD/GET warming.      |
| `rpm_fetch/warmer.py`     | Thread-pool fan-out over the package URLs.            |
