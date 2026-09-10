#!/usr/bin/env python3

###########################################################
#
# This python script is used for mysql database backup
# using mysqldump and gzip/pigz, with optional upload of
# the backup to Amazon S3 (or any S3-compatible storage)
# and/or a remote server over SCP, retention cleanup and
# failure alerts (webhook, email, healthcheck ping).
#
# Configuration is read from environment variables, which
# can be kept in a .env file (see .env.example).
#
# Written by : Rahul Kumar
# Website: http://tecadmin.net
# Created date: Dec 03, 2013
# Last modified: Sep 10, 2026
# Tested with : Python 3.12
# Script Revision: 2.1
#
##########################################################

import argparse
import datetime
import email.message
import fnmatch
import json
import logging
import os
import re
import shlex
import shutil
import smtplib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRUE_VALUES = {"1", "true", "yes", "on"}
DEFAULT_DUMP_OPTS = "--single-transaction --routines --triggers"
# Skipped by DB_NAMES=all; they can still be listed explicitly.
SYSTEM_DATABASES = {"information_schema", "performance_schema", "sys", "mysql"}
# Each run is stored in a folder named like "20180817-123433". Retention only
# ever touches folders matching this pattern.
RUN_NAME_FORMAT = "%Y%m%d-%H%M%S"
RUN_NAME_RE = re.compile(r"^\d{8}-\d{6}$")
SMTP_DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}

log = logging.getLogger("dbbackup")


class BackupError(Exception):
    pass


def load_env_file(path):
    """Load KEY=VALUE lines from a .env file into os.environ.

    Blank lines and lines starting with '#' are ignored, values may be wrapped
    in single or double quotes. Variables already set in the environment win,
    so a value can always be overridden from cron or the shell.
    """
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def env(name, default=""):
    return os.environ.get(name, default).strip()


def env_bool(name, default=False):
    value = env(name)
    return value.lower() in TRUE_VALUES if value else default


def env_list(name, default=""):
    return env(name, default).replace(",", " ").split()


class Retention:
    """Delete backups older than `days`, but always keep the newest `count`.
    Either can be used alone; 0 disables that rule."""

    def __init__(self, days, count):
        self.days = days
        self.count = count

    @property
    def enabled(self):
        return bool(self.days or self.count)

    def expired(self, names, current_run):
        if not self.enabled:
            return []
        runs = {}
        for name in names:
            if name == current_run or not RUN_NAME_RE.match(name):
                continue
            try:
                runs[name] = datetime.datetime.strptime(name, RUN_NAME_FORMAT)
            except ValueError:
                continue
        newest_first = sorted(runs, reverse=True)
        keep = set()
        if self.count:
            keep.update(newest_first[:self.count - 1])  # the current run is one of them
        if self.days:
            cutoff = datetime.datetime.now() - datetime.timedelta(days=self.days)
            keep.update(name for name, created in runs.items() if created >= cutoff)
        return [name for name in reversed(newest_first) if name not in keep]


class Config:
    def __init__(self):
        self.errors = []

        self.db_names = env("DB_NAMES")
        self.db_exclude = env_list("DB_EXCLUDE")
        self.backup_path = env("BACKUP_PATH", "/backup/dbbackup")
        try:
            self.dump_opts = shlex.split(env("MYSQLDUMP_OPTS", DEFAULT_DUMP_OPTS))
        except ValueError as exc:
            self.errors.append("MYSQLDUMP_OPTS: %s" % exc)
        self.compress_level = self._int("COMPRESS_LEVEL", 6)
        self.local_retention = self._retention("LOCAL")

        self.mysql_host = env("MYSQL_HOST", "localhost")
        self.mysql_port = env("MYSQL_PORT")
        self.mysql_socket = env("MYSQL_SOCKET")
        self.mysql_auth = env_bool("MYSQL_AUTH_ENABLED", True)
        self.mysql_user = env("MYSQL_USER")
        # Not stripped: whitespace may be part of the password.
        self.mysql_password = os.environ.get("MYSQL_PASSWORD", "")

        self.s3_enabled = env_bool("S3_ENABLED")
        self.s3_bucket = env("S3_BUCKET")
        self.s3_prefix = env("S3_PREFIX").strip("/")
        self.s3_endpoint_url = env("S3_ENDPOINT_URL")
        self.s3_storage_class = env("S3_STORAGE_CLASS")
        self.s3_retention = self._retention("S3")

        self.scp_enabled = env_bool("SCP_ENABLED")
        self.scp_host = env("SCP_HOST")
        self.scp_port = env("SCP_PORT", "22")
        self.scp_user = env("SCP_USER")
        self.scp_key_file = os.path.expanduser(env("SCP_KEY_FILE"))
        self.scp_remote_path = env("SCP_REMOTE_PATH").rstrip("/")
        self.scp_retention = self._retention("SCP")

        self.alert_on_success = env_bool("ALERT_ON_SUCCESS")
        self.alert_webhook_url = env("ALERT_WEBHOOK_URL")
        self.healthcheck_url = env("HEALTHCHECK_URL").rstrip("/")
        self.alert_email_to = env_list("ALERT_EMAIL_TO")
        self.alert_email_from = env("ALERT_EMAIL_FROM", "dbbackup@" + socket.getfqdn())
        self.smtp_host = env("SMTP_HOST")
        self.smtp_security = env("SMTP_SECURITY", "starttls").lower()
        self.smtp_port = self._int("SMTP_PORT", SMTP_DEFAULT_PORTS.get(self.smtp_security, 25))
        self.smtp_user = env("SMTP_USER")
        self.smtp_password = os.environ.get("SMTP_PASSWORD", "")

    def _int(self, name, default):
        value = env(name)
        if not value:
            return default
        if not value.isdigit():
            self.errors.append("%s must be a whole number, got %r" % (name, value))
            return default
        return int(value)

    def _retention(self, target):
        return Retention(self._int(target + "_RETENTION_DAYS", 0),
                         self._int(target + "_RETENTION_COUNT", 0))

    def validate(self):
        errors = list(self.errors)
        if not self.db_names:
            errors.append("DB_NAMES is not set")
        if not 1 <= self.compress_level <= 9:
            errors.append("COMPRESS_LEVEL must be between 1 and 9")
        if self.mysql_auth and not self.mysql_user:
            errors.append("MYSQL_USER is required when MYSQL_AUTH_ENABLED=true")
        if self.s3_enabled and not self.s3_bucket:
            errors.append("S3_BUCKET is required when S3_ENABLED=true")
        if self.scp_enabled:
            if not self.scp_host:
                errors.append("SCP_HOST is required when SCP_ENABLED=true")
            if not self.scp_remote_path:
                errors.append("SCP_REMOTE_PATH is required when SCP_ENABLED=true")
            if self.scp_key_file and not os.path.isfile(self.scp_key_file):
                errors.append("SCP_KEY_FILE not found: " + self.scp_key_file)
        if self.alert_email_to and not self.smtp_host:
            errors.append("SMTP_HOST is required when ALERT_EMAIL_TO is set")
        if self.smtp_security not in SMTP_DEFAULT_PORTS:
            errors.append("SMTP_SECURITY must be one of: " + ", ".join(SMTP_DEFAULT_PORTS))
        if not shutil.which("mysqldump"):
            errors.append("mysqldump not found in PATH")
        if self.db_names.lower() == "all" and not shutil.which("mysql"):
            errors.append("DB_NAMES=all needs the mysql client in PATH")
        return errors


def run(cmd):
    """Run a command and return its stdout, raising BackupError on failure."""
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        output = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise BackupError("%s failed (exit %d): %s" % (cmd[0], result.returncode, output))
    return result.stdout.decode(errors="replace")


def option_file_value(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_credentials_file(user, password):
    """Write the MySQL credentials to a private (0600) option file, so the
    password never shows up in the process list like `-pSECRET` does."""
    fd, path = tempfile.mkstemp(prefix="dbbackup-", suffix=".cnf")
    with os.fdopen(fd, "w") as fh:
        fh.write("[client]\n")
        fh.write("user=%s\n" % option_file_value(user))
        fh.write("password=%s\n" % option_file_value(password))
    return path


def mysql_client_args(cfg, credentials_file):
    # --defaults-extra-file must be the first option passed to mysql/mysqldump.
    # With auth disabled no credentials are passed and the client falls back to
    # ~/.my.cnf or socket (auth_socket / unix_socket) authentication.
    args = ["--defaults-extra-file=" + credentials_file] if credentials_file else []
    if cfg.mysql_socket:
        args.append("--socket=" + cfg.mysql_socket)
    if cfg.mysql_host:
        args.append("--host=" + cfg.mysql_host)
    if cfg.mysql_port:
        args.append("--port=" + cfg.mysql_port)
    return args


def resolve_databases(cfg, client_args):
    """DB_NAMES is "all", a file with one database name per line, or a
    comma/space separated list of names. DB_EXCLUDE patterns are removed."""
    if cfg.db_names.lower() == "all":
        output = run(["mysql"] + client_args + ["--batch", "--skip-column-names",
                                                "-e", "SHOW DATABASES"])
        names = [n for n in output.splitlines() if n not in SYSTEM_DATABASES]
    elif os.path.isfile(cfg.db_names):
        log.info("Reading database names from %s", cfg.db_names)
        with open(cfg.db_names) as fh:
            names = [line.strip() for line in fh]
    else:
        names = cfg.db_names.replace(",", " ").split()
    names = [n for n in names if n and not n.startswith("#")]
    excluded = [n for n in names if any(fnmatch.fnmatchcase(n, p) for p in cfg.db_exclude)]
    if excluded:
        log.info("Excluded by DB_EXCLUDE: %s", ", ".join(excluded))
    return [n for n in dict.fromkeys(names) if n not in excluded]  # de-duplicate, keep order


def compressor_cmd(level):
    # pigz is a drop-in, multi-threaded gzip; its output is a regular .gz file.
    binary = shutil.which("pigz") or shutil.which("gzip")
    if not binary:
        raise BackupError("neither pigz nor gzip found in PATH")
    return [binary, "-c", "-%d" % level]


def dump_database(db, dest, dump_cmd, compress_cmd):
    """Stream `mysqldump db | gzip > dest` without going through a shell and
    without writing an uncompressed .sql file to disk first."""
    partial = dest + ".part"
    with open(partial, "wb") as out, tempfile.TemporaryFile() as errors:
        dump = subprocess.Popen(dump_cmd + [db], stdout=subprocess.PIPE, stderr=errors)
        compress = subprocess.Popen(compress_cmd, stdin=dump.stdout, stdout=out, stderr=errors)
        dump.stdout.close()  # so mysqldump gets SIGPIPE if the compressor dies
        compress_rc = compress.wait()
        dump_rc = dump.wait()
        errors.seek(0)
        messages = errors.read().decode(errors="replace").strip()

    if dump_rc or compress_rc:
        os.remove(partial)
        raise BackupError("mysqldump exit %d, compressor exit %d: %s"
                          % (dump_rc, compress_rc, messages or "no error output"))
    if messages:
        log.warning("%s: %s", db, messages)
    os.replace(partial, dest)


def human_size(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return "%.1f %s" % (num, unit)
        num /= 1024.0
    return "%.1f TB" % num


def prune_local(cfg, run_name):
    names = [n for n in os.listdir(cfg.backup_path)
             if os.path.isdir(os.path.join(cfg.backup_path, n))]
    for name in cfg.local_retention.expired(names, run_name):
        shutil.rmtree(os.path.join(cfg.backup_path, name))
        log.info("Retention: removed local backup %s", name)


# --- S3 -------------------------------------------------------------------

def s3_client(cfg):
    """Return a boto3 S3 client, or None to fall back to the aws CLI."""
    try:
        import boto3
    except ImportError:
        if not shutil.which("aws"):
            raise BackupError("S3 needs boto3 (pip install boto3) or the aws CLI")
        return None
    return boto3.client("s3", endpoint_url=cfg.s3_endpoint_url or None)


def aws_s3(cfg, *args):
    cmd = ["aws", "s3"] + list(args)
    if cfg.s3_endpoint_url:
        cmd += ["--endpoint-url", cfg.s3_endpoint_url]
    return run(cmd)


def s3_base(cfg):
    return cfg.s3_prefix + "/" if cfg.s3_prefix else ""


def upload_s3(cfg, backup_dir, run_name):
    prefix = s3_base(cfg) + run_name + "/"
    client = s3_client(cfg)
    if client:
        extra_args = {"StorageClass": cfg.s3_storage_class} if cfg.s3_storage_class else None
        for name in sorted(os.listdir(backup_dir)):
            # upload_file switches to multipart uploads for large files.
            client.upload_file(os.path.join(backup_dir, name), cfg.s3_bucket, prefix + name,
                               ExtraArgs=extra_args)
            log.info("Uploaded s3://%s/%s%s", cfg.s3_bucket, prefix, name)
    else:
        args = ["cp", backup_dir, "s3://%s/%s" % (cfg.s3_bucket, prefix),
                "--recursive", "--only-show-errors"]
        if cfg.s3_storage_class:
            args += ["--storage-class", cfg.s3_storage_class]
        aws_s3(cfg, *args)
        log.info("Uploaded %s to s3://%s/%s", backup_dir, cfg.s3_bucket, prefix)


def prune_s3(cfg, run_name):
    base = s3_base(cfg)
    client = s3_client(cfg)
    if client:
        paginator = client.get_paginator("list_objects_v2")
        names = [p["Prefix"][len(base):].rstrip("/")
                 for page in paginator.paginate(Bucket=cfg.s3_bucket, Prefix=base, Delimiter="/")
                 for p in page.get("CommonPrefixes", [])]
    else:
        output = aws_s3(cfg, "ls", "s3://%s/%s" % (cfg.s3_bucket, base))
        names = [line.split()[-1].rstrip("/") for line in output.splitlines()
                 if line.strip().startswith("PRE ")]

    for name in cfg.s3_retention.expired(names, run_name):
        prefix = base + name + "/"
        if client:
            keys = [obj["Key"]
                    for page in paginator.paginate(Bucket=cfg.s3_bucket, Prefix=prefix)
                    for obj in page.get("Contents", [])]
            for i in range(0, len(keys), 1000):  # delete_objects takes max 1000 keys
                response = client.delete_objects(
                    Bucket=cfg.s3_bucket,
                    Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True})
                if response.get("Errors"):
                    raise BackupError("could not delete %s: %s"
                                      % (prefix, response["Errors"][0].get("Message")))
        else:
            aws_s3(cfg, "rm", "s3://%s/%s" % (cfg.s3_bucket, prefix),
                   "--recursive", "--only-show-errors")
        log.info("Retention: removed s3://%s/%s", cfg.s3_bucket, prefix)


# --- SCP ------------------------------------------------------------------

def ssh_target(cfg):
    return "%s@%s" % (cfg.scp_user, cfg.scp_host) if cfg.scp_user else cfg.scp_host


def ssh_opts(cfg):
    # BatchMode makes ssh fail instead of hanging on a password prompt under cron.
    opts = ["-o", "BatchMode=yes"]
    if cfg.scp_key_file:
        opts += ["-i", cfg.scp_key_file]
    return opts


def ssh(cfg, command):
    return run(["ssh"] + ssh_opts(cfg) + ["-p", cfg.scp_port, ssh_target(cfg), command])


def upload_scp(cfg, backup_dir):
    ssh(cfg, "mkdir -p " + shlex.quote(cfg.scp_remote_path))
    run(["scp"] + ssh_opts(cfg) + ["-P", cfg.scp_port, "-q", "-r", backup_dir,
                                   "%s:%s/" % (ssh_target(cfg), cfg.scp_remote_path)])
    log.info("Copied %s to %s:%s/", backup_dir, ssh_target(cfg), cfg.scp_remote_path)


def prune_scp(cfg, run_name):
    names = ssh(cfg, "ls -1 " + shlex.quote(cfg.scp_remote_path)).split()
    expired = cfg.scp_retention.expired(names, run_name)
    if expired:
        paths = [shlex.quote(cfg.scp_remote_path + "/" + name) for name in expired]
        ssh(cfg, "rm -rf -- " + " ".join(paths))
        for name in expired:
            log.info("Retention: removed %s:%s/%s", ssh_target(cfg), cfg.scp_remote_path, name)


# --- Alerts ---------------------------------------------------------------

def send_webhook(url, subject, body):
    # {"text": ...} is understood by Slack, Mattermost, Rocket.Chat, Google Chat
    # and Discord (append /slack to a Discord webhook URL).
    data = json.dumps({"text": subject + "\n```\n" + body + "\n```"}).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=15).close()


def send_email(cfg, subject, body):
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.alert_email_from
    msg["To"] = ", ".join(cfg.alert_email_to)
    msg.set_content(body)
    smtp_class = smtplib.SMTP_SSL if cfg.smtp_security == "ssl" else smtplib.SMTP
    with smtp_class(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
        if cfg.smtp_security == "starttls":
            smtp.starttls()
        if cfg.smtp_user:
            smtp.login(cfg.smtp_user, cfg.smtp_password)
        smtp.send_message(msg)


def ping_healthcheck(url, ok, body):
    # Healthchecks.io convention: <url> for success, <url>/fail for failure.
    # The body shows up in the check's event log.
    request = urllib.request.Request(url if ok else url + "/fail", data=body.encode()[:100000])
    urllib.request.urlopen(request, timeout=15).close()


def send_alerts(cfg, ok, subject, body):
    channels = []
    if cfg.healthcheck_url:
        channels.append(("healthcheck", lambda: ping_healthcheck(cfg.healthcheck_url, ok, body)))
    if not ok or cfg.alert_on_success:
        if cfg.alert_webhook_url:
            channels.append(("webhook", lambda: send_webhook(cfg.alert_webhook_url, subject, body)))
        if cfg.alert_email_to:
            channels.append(("email", lambda: send_email(cfg, subject, body)))
    for name, send in channels:
        try:
            send()
            log.info("Sent %s notification", name)
        except Exception as exc:
            log.error("Could not send %s notification: %s", name, exc)


# --- Main -----------------------------------------------------------------

def attempt(problems, label, func, *args):
    """Run func, recording a failure in problems instead of raising."""
    try:
        func(*args)
        return True
    except Exception as exc:  # boto3 raises its own exception types
        log.error("%s failed: %s", label, exc)
        problems.append("%s failed: %s" % (label, exc))
        return False


def backup(cfg, run_name, problems):
    """Dump, upload and prune. Returns summary lines for the notification;
    anything that went wrong is appended to problems."""
    backup_dir = os.path.join(cfg.backup_path, run_name)
    summary = ["Backup dir: " + backup_dir]

    credentials_file = None
    if cfg.mysql_auth:
        credentials_file = write_credentials_file(cfg.mysql_user, cfg.mysql_password)
    else:
        log.info("MySQL auth disabled, using ~/.my.cnf or socket authentication")

    done = []
    try:
        client_args = mysql_client_args(cfg, credentials_file)
        databases = resolve_databases(cfg, client_args)
        if not databases:
            problems.append("No databases to back up (check DB_NAMES / DB_EXCLUDE)")
            return summary

        os.makedirs(backup_dir, exist_ok=True)
        dump_cmd = ["mysqldump"] + client_args + cfg.dump_opts
        compress_cmd = compressor_cmd(cfg.compress_level)
        log.info("Backing up %d database(s) to %s", len(databases), backup_dir)
        for db in databases:
            dest = os.path.join(backup_dir, db + ".sql.gz")
            started = time.monotonic()
            try:
                dump_database(db, dest, dump_cmd, compress_cmd)
            except BackupError as exc:
                log.error("Backup of %s failed: %s", db, exc)
                problems.append("Database %s: %s" % (db, exc))
                continue
            done.append(db)
            log.info("Backed up %s (%s, %.1fs)", db, human_size(os.path.getsize(dest)),
                     time.monotonic() - started)
    finally:
        if credentials_file:
            os.remove(credentials_file)

    summary.append("Databases: %d of %d backed up" % (len(done), len(databases)))
    if not done:
        os.rmdir(backup_dir)
        return summary

    # Never prune after a failed dump: a string of failing runs would
    # otherwise slowly delete every good backup.
    prune = len(done) == len(databases)
    if not prune and (cfg.local_retention.enabled or cfg.s3_retention.enabled
                      or cfg.scp_retention.enabled):
        log.warning("Skipping retention cleanup because some databases failed")

    if prune and cfg.local_retention.enabled:
        attempt(problems, "Local retention", prune_local, cfg, run_name)
    if cfg.s3_enabled:
        uploaded = attempt(problems, "S3 upload", upload_s3, cfg, backup_dir, run_name)
        summary.append("S3: " + ("uploaded" if uploaded else "FAILED"))
        if uploaded and prune and cfg.s3_retention.enabled:
            attempt(problems, "S3 retention", prune_s3, cfg, run_name)
    if cfg.scp_enabled:
        uploaded = attempt(problems, "SCP upload", upload_scp, cfg, backup_dir)
        summary.append("SCP: " + ("uploaded" if uploaded else "FAILED"))
        if uploaded and prune and cfg.scp_retention.enabled:
            attempt(problems, "SCP retention", prune_scp, cfg, run_name)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Back up MySQL databases with mysqldump.")
    parser.add_argument("--env-file",
                        help="path to the .env file (default: .env next to this script)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("boto3", "botocore", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    os.umask(0o077)  # backups contain all the data; keep them readable by owner only

    env_file = args.env_file or os.path.join(SCRIPT_DIR, ".env")
    if os.path.isfile(env_file):
        load_env_file(env_file)
    elif args.env_file:
        log.error("Env file not found: %s", env_file)
        return 2

    cfg = Config()
    host = socket.gethostname()
    run_name = time.strftime(RUN_NAME_FORMAT)

    errors = cfg.validate()
    if errors:
        for error in errors:
            log.error("Config: %s", error)
        send_alerts(cfg, False, "[dbbackup] FAILED on %s: configuration error" % host,
                    "Host: %s\n\nProblems:\n" % host + "\n".join("- " + e for e in errors))
        return 2

    problems = []
    try:
        summary = backup(cfg, run_name, problems)
    except Exception as exc:
        if isinstance(exc, BackupError):
            log.error("Backup aborted: %s", exc)
        else:
            log.exception("Backup aborted")  # unexpected, so include the traceback
        problems.append("Backup aborted: %s" % exc)
        summary = []

    body = "\n".join(["Host: " + host, "Run: " + run_name] + summary)
    if problems:
        body += "\n\nProblems:\n" + "\n".join("- " + p for p in problems)
        subject = "[dbbackup] FAILED on %s: %d problem(s)" % (host, len(problems))
        log.error("Backup finished with %d problem(s)", len(problems))
    else:
        subject = "[dbbackup] OK on %s" % host
        log.info("Backup completed successfully")
    send_alerts(cfg, not problems, subject, body)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
