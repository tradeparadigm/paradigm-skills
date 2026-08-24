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
CREATE OR REPLACE SECRET s3_irsa (
  TYPE S3,
  PROVIDER CREDENTIAL_CHAIN,
  REGION 'ap-northeast-1'
);
```

Both extensions are pre-installed in the terminal image, so `INSTALL` is a
no-op after the first use and never hits the extension repository.

## Do not hand-roll the credentials

Do **not** read `$AWS_WEB_IDENTITY_TOKEN_FILE`, call STS with `curl`, scrape
`<AccessKeyId>` out of the XML response, or pass keys in with
`SET s3_access_key_id=…`. That approach fails in this runtime, and it fails
silently:

- The agent's `exec` tool is not a shell. It splits a multi-line script into
  separate steps and runs each one on its own, so a variable assigned on one
  line (`TOKEN=…`, `CREDS=…`) is already gone by the next line.
- A leading `#` comment becomes a step that tries to execute a program named
  `#`, which aborts the whole chain.
- Quoted shell metacharacters get misread — `cut -d'>'` is parsed as a file
  redirect.

The result the user sees is a bare `Exec failed` with no output to diagnose.
Keep everything inside one `duckdb -c "…"` call and none of this applies.

(The same STS logic *is* fine inside a committed `.sh` file — e.g.
`options-recap/scripts/run_recap.sh` — because `bash script.sh` is a single
process with normal shell state. The rule here is about inline shell that an
agent pastes into `exec`.)

## Token lifecycle

Each `duckdb -c` invocation is its own process and resolves fresh credentials
through the chain, so there is no expiry to manage for one-shot queries. Inside
a long-lived DuckDB session, re-run the `CREATE OR REPLACE SECRET` statement if
a read starts returning HTTP 400 `InvalidToken`.

## Verifying access

The cheapest reachability check is a read of a known stable key — the hot
surface, which is clobbered every 60 s and always present:

```sql
INSTALL httpfs; LOAD httpfs;
INSTALL aws;    LOAD aws;
CREATE OR REPLACE SECRET s3_irsa (TYPE S3, PROVIDER CREDENTIAL_CHAIN, REGION 'ap-northeast-1');

SELECT COUNT(*) FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet');
```

A non-zero count confirms credentials and network path are good.

An `HTTP 403` on a bucket that the query names correctly is an IAM problem, not
a credential-plumbing problem — the pod's role is missing the read grant for
that bucket. Report it as such rather than retrying with different credential
mechanics.

## Coverage probe pattern

The catalog's verified date ranges are point-in-time; the tapes grow forward.
Confirm current coverage by reading the date column directly:

```sql
SELECT min(DATE) AS earliest, max(DATE) AS latest
FROM read_csv_auto('s3://dt-paradigm-data/paradigm_data/paradigm_trade_tape_slim.csv.gz');
```

Use this before concluding "no data" for a recent date.
