# S3 Access — IRSA Credential Bootstrap for DuckDB

The agent stack runs on EKS with IRSA (IAM Roles for Service Accounts). To
query the market-data buckets (`s3://dt-paradigm-data`,
`s3://dt-exchange-venue-data`, and `s3://dt-paradex-data` — all same region
and same role) from DuckDB, exchange
the projected web identity token for temporary STS credentials, then load
them into DuckDB's `httpfs` extension.

## Bootstrap (run once per session)

```bash
TOKEN=$(cat $AWS_WEB_IDENTITY_TOKEN_FILE)
CREDS=$(curl -s "https://sts.ap-northeast-1.amazonaws.com/?Action=AssumeRoleWithWebIdentity&Version=2011-06-15&RoleArn=${AWS_ROLE_ARN}&RoleSessionName=duckdb&WebIdentityToken=${TOKEN}")
AK=$(echo $CREDS | grep -o '<AccessKeyId>[^<]*' | cut -d'>' -f2)
SK=$(echo $CREDS | grep -o '<SecretAccessKey>[^<]*' | cut -d'>' -f2)
ST=$(echo $CREDS | grep -o '<SessionToken>[^<]*' | cut -d'>' -f2)
```

Required env vars:

- `AWS_WEB_IDENTITY_TOKEN_FILE` — projected service account token path.
- `AWS_ROLE_ARN` — IAM role to assume.

## Pass into DuckDB

```sql
INSTALL httpfs;
LOAD httpfs;
SET s3_region='ap-northeast-1';
SET s3_access_key_id='<AK>';
SET s3_secret_access_key='<SK>';
SET s3_session_token='<ST>';
```

## Token lifecycle

- STS tokens expire after **~1 hour**.
- On HTTP 400 `InvalidToken`, re-run the bootstrap to refresh.
- Treat refreshes as idempotent; safe to re-run mid-session.

## Verifying access

After bootstrap, the cheapest reachability check is a read of a known
stable key — the hot surface, which is clobbered every 60 s and always
present:

```sql
SELECT COUNT(*) FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet');
```

A non-zero count confirms credentials and network path are good.

## Coverage probe pattern

The catalog's verified date ranges are point-in-time; the tapes grow forward.
Confirm current coverage by reading the date column directly:

```sql
SELECT min(DATE) AS earliest, max(DATE) AS latest
FROM read_csv_auto('s3://dt-paradigm-data/paradigm_data/paradigm_trade_tape_slim.csv.gz');
```

Use this before concluding "no data" for a recent date.
