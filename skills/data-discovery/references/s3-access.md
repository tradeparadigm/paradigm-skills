# S3 Access — credentials for DuckDB

The agent stack runs on EKS with IRSA (IAM Roles for Service Accounts), and
**DuckDB resolves those credentials itself**. The `aws` extension's
`CREDENTIAL_CHAIN` provider reads the pod's projected web identity token
in-process, so there is nothing to bootstrap: no shell script, no STS call, no
token handling, no keys to pass around.

To query the market-data buckets (`s3://dt-paradigm-data`,
`s3://dt-exchange-venue-data`, and `s3://dt-paradex-data` — all same region and
same role), put the preamble below at the top of the query.

## Preamble — include it in the same `duckdb -c` call as the query

```sql
INSTALL httpfs; LOAD httpfs;
INSTALL aws;    LOAD aws;
SET ca_cert_file='/etc/ssl/certs/ca-certificates.crt';
SET enable_server_cert_verification=true;
CREATE OR REPLACE SECRET s3_irsa (
  TYPE S3,
  PROVIDER CREDENTIAL_CHAIN,
  REGION 'ap-northeast-1',
  ENDPOINT 's3.ap-northeast-1.amazonaws.com'
);
```

## Why the two `SET` lines

The agent's egress is TLS-intercepted: every 443 connection terminates at a
certificate the credential proxy minted for the host asked for, because it has
to read and rewrite the request to swap credential placeholders for real
secrets. Every program in the pod therefore has to trust the proxy's CA.

duckdb is the one that cannot be told this any other way. It reads no CA
environment variable and not the OS trust store either — its httpfs extension
carries a compiled-in CA list, and only `ca_cert_file` displaces it. Without
those two lines every read fails as:

```
IO Error: SSL peer certificate or SSH remote key was not OK
```

which names neither a certificate nor the proxy, and reads like a network
fault. `/etc/ssl/certs/ca-certificates.crt` is the pod's own trust store with
the proxy's CA folded into it, so it still verifies anything the proxy did not
mint; do not point these at a single-certificate file, and never turn
verification off — the interception is the product.

The `duckdb` CLI also picks these up from a mounted `~/.duckdbrc`, so a query
that forgets them still works there. Python does not: `import duckdb` reads no
rc file, so a script has to issue them on the connection itself (see
`options-recap/scripts/collect_recap.py`, `ca_statements`).

Both extensions are pre-installed in the terminal image, so `INSTALL` is a
no-op after the first use and never hits the extension repository.

`ENDPOINT` is pinned to the regional host so the S3 authority is deterministic:
the global `s3.amazonaws.com` answers a cross-region request with a 307 redirect,
which an exact-match egress allowlist inside the OC enclave cannot follow.

## Do not hand-roll the credentials

Do **not** read `$AWS_WEB_IDENTITY_TOKEN_FILE`, call STS with `curl`, scrape
`<AccessKeyId>` out of the XML response, or pass keys in with
`SET s3_access_key_id=…`. Failure modes observed in real sessions:

- **"Bootstrap once per session" is not possible.** Every `exec` call gets a
  fresh shell, so credentials exported in one call are gone by the next. The
  bootstrap and the query that needs it have to be the same call — at which
  point the bootstrap buys nothing over `CREDENTIAL_CHAIN`.
- **The shell is `sh`, not bash.** Bash-isms fail outright: `${AK:0:4}` returns
  `Bad substitution`.
- **Expansion order fails silently, and the error points at the wrong thing.**
  A heredoc that expands `$AK` before the credentials are sourced writes empty
  strings into the `SET` statements, and the query returns
  `HTTP 403 AccessDenied` — indistinguishable from a missing IAM grant. This is
  the expensive one: it sends you debugging bucket permissions when the bug is
  shell ordering.
- **Keys land in argv**, readable by anything that can see the process list.

None of these exist when the credential step is a SQL statement inside the same
`duckdb -c "…"` call as the query.

(The same STS logic can be valid inside a committed `.sh` file because the
script is one process with normal shell state and a known interpreter. The rule
here is about inline shell an agent assembles across `exec` calls.)

## The concurrent object reader

`scripts/s3_async.py` lives here, beside `execution_tape.py`, because it is a
shared reader rather than any one skill's: `read_objects(paths, columns)` signs
and fetches every object through obstore outside the GIL and returns one Arrow
table, which DuckDB then reads by replacement scan. Import it the way every
cross-skill import in this repo works — insert `data-discovery/scripts` on
`sys.path`, never reach sideways into another skill's `scripts/`.

Two properties bind its callers:

- **It pins the S3 endpoint.** The enclave's egress allowlist is exact-match and
  cannot follow a 307, so `S3Store(...)` must carry `endpoint=`.
  `options-recap/tests/test_run_recap.py` is the only thing that checks this,
  which is why that workflow also watches this file.
- **Peak memory is the whole window, and the caller usually holds it twice.**
  `_concat` is zero-copy where the schemas match, so the concatenation itself
  costs nothing; what costs is that every object's rows are resident at once,
  and `CONCURRENCY` and `BATCH` bound only the raw bodies and the single batch
  being parsed. The second copy is the caller's: `/recap` keeps the returned
  table alive as a replacement scan while DuckDB materialises a result that, in
  render mode, is every row. Cost scales with objects read, not with wall time —
  a 30-day window peaks near 6 GiB against a 4 GiB production container.
  Reducing per batch, rather than returning one table, is what would bound it.

## Token lifecycle

Each `duckdb -c` invocation is its own process and resolves fresh credentials
through the chain, so there is no expiry to manage for one-shot queries. Inside
a long-lived DuckDB session, re-run the `CREATE OR REPLACE SECRET` statement if
a read starts returning HTTP 400 `InvalidToken`.

## Verifying access

Use a small non-hot source object to verify the credential and network path:

```sql
INSTALL httpfs; LOAD httpfs;
INSTALL aws;    LOAD aws;
SET ca_cert_file='/etc/ssl/certs/ca-certificates.crt';
SET enable_server_cert_verification=true;
CREATE OR REPLACE SECRET s3_irsa (TYPE S3, PROVIDER CREDENTIAL_CHAIN, REGION 'ap-northeast-1', ENDPOINT 's3.ap-northeast-1.amazonaws.com');

SELECT COUNT(*)
FROM read_csv_auto('s3://dt-paradigm-data/paradigm_data/paradigm_rfq_tape_slim.csv.gz');
```

A non-zero count confirms credentials and network path are good.

An `HTTP 403` on a bucket that the query names correctly is an IAM problem, not
a credential-plumbing problem — the pod's role is missing the read grant for
that bucket. Report it as such rather than retrying with different credential
mechanics. The grant is per bucket and the three are granted together, so a 403
on one of them while another reads fine means that customer's role was given a
narrower list, not that the credential chain broke.

## Coverage probe pattern

The catalog's verified date ranges are point-in-time. Confirm coverage by
reading the source's event-time column directly:

```sql
SELECT min(DATE) AS earliest, max(DATE) AS latest
FROM read_csv_auto('s3://dt-paradigm-data/paradigm_data/paradigm_rfq_tape_slim.csv.gz');
```

For exchange landing data, inspect `max(CAST(timestamp AS TIMESTAMP))` in the
narrow raw partition selected for the request. Use this before concluding
"no data" for a recent date.
