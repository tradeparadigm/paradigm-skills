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
CREATE OR REPLACE SECRET s3_irsa (TYPE S3, PROVIDER CREDENTIAL_CHAIN, REGION 'ap-northeast-1');

SELECT COUNT(*)
FROM read_csv_auto('s3://dt-paradigm-data/paradigm_data/paradigm_rfq_tape_slim.csv.gz');
```

A non-zero count confirms credentials and network path are good.

An `HTTP 403` on a bucket that the query names correctly is an IAM problem, not
a credential-plumbing problem — the pod's role is missing the read grant for
that bucket. Report it as such rather than retrying with different credential
mechanics.

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
