# python-mysql-backup
Python scripts for MySQL database backup.

`dbbackup.py` dumps one or more databases (or all of them) with `mysqldump`,
compresses them with `pigz` (or `gzip` if pigz isn't installed), and can optionally:

- upload each backup to Amazon S3 (or S3-compatible storage) and/or a remote server over SCP
- delete old backups locally, on S3 and on the SCP server (retention)
- send alerts on failure via webhook (Slack, Discord, ...), email, or a Healthchecks.io ping

## Requirements

- Python 3.6+
- `mysqldump` and `gzip` (install `pigz` for faster multi-threaded compression)
- S3 upload: `boto3` (`pip install boto3`) or the `aws` CLI
- SCP upload: `ssh`/`scp` with key-based login to the remote host

## Setup

```sh
cp .env.example .env
chmod 600 .env
# edit .env
./dbbackup.py
```

Every option is documented in [.env.example](.env.example). Variables set in the
environment override the `.env` file, and `--env-file` loads a different file:

```sh
./dbbackup.py --env-file /etc/dbbackup/production.env
DB_NAMES=shop ./dbbackup.py
```

Each run writes `BACKUP_PATH/<YYYYmmdd-HHMMSS>/<database>.sql.gz`. S3 uploads go to
`s3://S3_BUCKET/S3_PREFIX/<YYYYmmdd-HHMMSS>/` and SCP uploads to
`SCP_REMOTE_PATH/<YYYYmmdd-HHMMSS>/`.

### MySQL authentication

With `MYSQL_AUTH_ENABLED=true`, `MYSQL_USER` and `MYSQL_PASSWORD` are passed to
mysqldump through a temporary option file readable only by the owner, so the password
never shows up in `ps`. Set it to `false` to use `~/.my.cnf` or socket authentication
instead (for example, root with `auth_socket` on Ubuntu/Debian).

### Retention

Local, S3 and SCP backups each have `*_RETENTION_DAYS` and `*_RETENTION_COUNT`
settings. A backup is deleted only when it is older than the given number of days
*and* not one of the newest N backups, so the count also protects you if backups stop
running for a while. Cleanup is skipped in any run where a database failed, and only
folders named like `20260910-023000` are ever deleted.

### Alerts

A notification is sent when any dump, upload or cleanup fails, when MySQL can't be
reached, or when the configuration is invalid (set `ALERT_ON_SUCCESS=true` to get one
every run). Channels: `ALERT_WEBHOOK_URL`, `ALERT_EMAIL_TO` with SMTP settings, and
`HEALTHCHECK_URL`. The healthcheck ping also catches backups that never ran, e.g. a
broken crontab or a server that was down.

## Cron

The script exits with `0` on success, `1` if any database dump or upload failed, and
`2` for configuration errors. That makes it easy to alert on failures:

```cron
30 2 * * * /opt/scripts/python-mysql-backup/dbbackup.py >> /var/log/dbbackup.log 2>&1
```

## Restore

```sh
gunzip < /backup/dbbackup/20260910-023000/shop.sql.gz | mysql shop
```
