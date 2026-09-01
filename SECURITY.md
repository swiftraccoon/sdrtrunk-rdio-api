# Security Policy

## Supported Versions

We release patches for security vulnerabilities. Currently supported versions:

| Version | Supported          |
| ------- | ------------------ |
| Latest release / `main` | :white_check_mark: |
| Older releases          | Best effort        |

## Reporting a Vulnerability

We take the security of sdrtrunk-rdio-api seriously. If you believe you have found a security vulnerability, please report it responsibly.

### Please do NOT

- Open a public GitHub issue for security vulnerabilities
- Post about the vulnerability on social media or forums
- Exploit the vulnerability for any purpose other than verification

### Please DO

- Open a **private security advisory** on GitHub:
  1. Go to the [Security tab](https://github.com/swiftraccoon/sdrtrunk-rdio-api/security) of this repository
  2. Click "Report a vulnerability"
  3. Provide detailed steps to reproduce
  4. Include the version affected
  5. If possible, provide a proof of concept

### What to expect

1. **Investigation**: We will investigate and validate the report
2. **Resolution**: We will work on a fix and coordinate disclosure
3. **Credit**: We will credit you for the discovery (unless you prefer to remain anonymous)

## Security Measures

### Current Security Features

#### Authentication & Authorization

- API key-based authentication
- IP-based access restrictions
- System-based access control
- Authenticated query, metrics, and audio endpoints
- System- and IP-scoped keys with stable audit identifiers
- Rate limiting by a server-resolved client IP

#### Input Validation

- Comprehensive input sanitization
- SQL injection prevention
- Path traversal protection
- File type validation
- File size limits
- Request size limits
- Bounded streaming multipart parsing
- Strict MP3 extension, MIME, and signature checks

#### Network Security

- Security headers (X-Frame-Options, X-Content-Type-Options, etc.)
- Content Security Policy (CSP)
- CORS configuration
- Rate limiting
- Trusted-proxy chain validation
- Optional built-in TLS and bounded request read timeouts

#### Data Protection

- API key values are excluded from logs; persisted audit metadata is bounded
- Secure file storage
- Mode-0600 configuration, database, log, temporary, and audio files
- Database query parameterization
- Error message sanitization
- Retention based on server receipt time and SQLite deleted-cell scrubbing

### Security Best Practices for Deployment

#### 1. Use HTTPS

Always deploy with HTTPS in production:

```yaml
server:
  host: 127.0.0.1
  port: 8443
  ssl_cert: /path/to/cert.pem
  ssl_key: /path/to/private-key.pem
```

Configure both TLS paths or neither; a partial pair fails validation. Binding
to `0.0.0.0` is appropriate only with a firewall or a hardened reverse proxy.
When TLS terminates at a same-host proxy, keep the application on loopback.

#### 2. Configure Strong API Keys

Generate strong, random API keys:

```python
import secrets
api_key = secrets.token_urlsafe(32)
```

#### 3. Restrict API Access

Configure IP restrictions for API keys:

```yaml
security:
  api_keys:
    - key: "PASTE-A-UNIQUE-32-BYTE-RANDOM-KEY-HERE"
      identifier: "scanner-1"
      description: "SDRTrunk Instance 1"
      allowed_ips: ["192.168.1.100"]
      allowed_systems: ["1"]
```

Keys shorter than 16 characters and blank keys are rejected. Every key also
requires a unique, public `identifier`; credential-derived fingerprints are
not stored. Never pass a key directly on a command line, commit it, or include
it in diagnostic output.
Leave `allow_unauthenticated_uploads` and `allow_unauthenticated_reads` false.

#### 4. Database Security

- Run the process under a dedicated operating-system account
- Keep the SQLite database and backups mode 0600
- Encrypt the host volume and off-host backups when confidentiality matters
- Test restoration regularly and protect backup retention separately

#### 5. File Storage Security

- Store files outside the web root
- Keep files mode 0600 and state directories mode 0700
- Regular cleanup of old files
- Virus scanning for uploaded files (recommended)

#### 6. Monitoring & Logging

- Enable comprehensive logging
- Monitor for suspicious activities
- Set up alerts for:
  - Multiple failed authentication attempts
  - Unusual file upload patterns
  - Large number of requests from single IP

#### 7. Regular Updates

- Keep Python dependencies updated
- Monitor security advisories
- Apply security patches promptly

### Platform and Availability Boundaries

- POSIX deployments provide the strongest local-filesystem guarantees: private
  modes, descriptor-relative traversal, no-follow opens, and directory fsync.
  On macOS, private files and roots also fail closed when an extended ACL grants
  access beyond the Unix mode bits; the standard deny-only home-directory ACL
  remains supported. Remove an unsafe inherited ACL with `chmod -RN PATH`
  before startup.
  Run the service under a dedicated account; another process with the same UID
  is inside the trust boundary and can replace paths used by SQLite, Hypercorn,
  or the standard-library logging handler.
- Python's `chmod(0600/0700)` does not create or validate a restrictive Windows
  DACL and pathname checks cannot fully exclude junction/reparse-point races.
  Ambiguous Win32 names (ADS, DOS devices and 8.3-looking aliases, trailing
  dot/space names, and device/extended namespaces) are rejected, but that does
  not replace a restrictive DACL.
  Multi-user Windows confidentiality is therefore unsupported unless an
  administrator separately grants the service identity (plus SYSTEM/admins)
  exclusive NTFS access to the config, database, log, temp, storage, export,
  certificate, and backup trees. Prefer a dedicated Linux container/host when
  that guarantee is required.
- Application rate limits, eight global upload-parse slots, per-IP admission,
  byte limits, and parse deadlines bound local resource use; they are not a
  distributed denial-of-service perimeter. RdioScanner credentials are carried
  inside the multipart body and cannot be authenticated until bounded parsing
  reaches them. Internet-facing deployments need a firewall or reverse proxy
  with connection limits, header/body timeouts, minimum body-rate enforcement,
  and its own source-aware rate limits.
- Archive quota reconciliation intentionally closes readiness and upload
  admission until accounting is certain. Very large archives can take time to
  scan. Monitor `/health`, keep archives within operationally reasonable file
  counts, and use the supported single-worker server unless quota coordination
  is provided outside this process.
- First-upgrade index creation uses filesystem-backed SQLite sorting and
  conservatively verifies database/WAL/sort scratch headroom before each
  missing required index. A large legacy database can therefore fail startup
  closed until sufficient free bytes and inodes are available; take a backup
  and perform that upgrade during an operator-controlled maintenance window.
  On Unix, preflight follows SQLite's `SQLITE_TMPDIR`, then `TMPDIR`, selection
  rather than Python's different temporary-directory choice. A separate
  scratch filesystem must retain a fixed 32 MiB safety margin after reserving
  `max(main database + WAL size, 32 MiB)`; a shared database/scratch device
  instead keeps the full configured persistent-state byte and inode reserves.
  The supplied 64 MiB tmpfs therefore needs temporary enlargement for a
  missing-index upgrade when the legacy database plus WAL is larger than
  32 MiB.
- Authenticated archive scans have both a 15-second SQLite progress deadline
  and a fixed virtual-machine instruction ceiling. SQLite aggregate/sort
  scratch still belongs on a separately bounded temporary filesystem: the
  supplied container directs it to the 64 MiB `/tmp` tmpfs. Native deployments
  should set `SQLITE_TMPDIR` to a private, quota-limited filesystem; a plain
  directory on an otherwise unbounded volume is not a hard scratch-space cap.
- The server and the destructive/long-snapshot `clean` and `export` commands
  use a kernel advisory lock per database directory. Run those CLI commands only after
  stopping the server; they fail closed when another protected process owns the
  lock. Direct library callers and non-cooperating same-UID processes remain
  inside the local trust boundary.
- The server holds a separate advisory lock for the active rotating-log family;
  a second process cannot append or roll it concurrently. CLI commands log to
  the console only, and configured database sidecars, log rotations/lock, TLS,
  config, key, backup, export, storage, and temp paths are checked for aliases
  before mutation.
- CLI monitoring and cleanup-preview reads open the existing database with
  SQLite `mode=ro` and `query_only`, skip schema/journal initialization, apply
  the same bounded-query deadline, and close their snapshot before terminal
  output. A blocked output pipe therefore cannot pin a live service WAL.
- Retention performs logical deletion: SQLite `secure_delete`, WAL checkpointing,
  and audio-file unlinking reduce ordinary recovery exposure, but cannot promise
  forensic erasure from SSD wear-leveling, copy-on-write filesystems, snapshots,
  replicas, or backups. Use encrypted volumes and apply the same retention policy
  to every snapshot and backup when deletion guarantees matter.

### Security Checklist

Before deploying to production:

- [ ] HTTPS configured and enforced
- [ ] Strong API keys generated and configured
- [ ] Anonymous compatibility flags are false
- [ ] IP restrictions configured where appropriate
- [ ] Database user has minimal required privileges
- [ ] File upload directory is outside web root
- [ ] Proper file permissions set
- [ ] Service binds only to the intended interfaces
- [ ] `trusted_proxies` contains only controlled proxy peers
- [ ] Logging configured and monitored
- [ ] Rate limiting enabled
- [ ] Security headers configured
- [ ] Error messages don't leak sensitive information
- [ ] Regular backup strategy in place
- [ ] Update strategy defined

## Security Updates

Security updates will be released as soon as possible after a vulnerability is confirmed. We will:

1. Release a patch version with the fix
2. Update the CHANGELOG with security notes
3. Send notifications to users (if contact information is available)
4. Wait 30 days before public disclosure (unless immediate disclosure is necessary)

## Compliance

This project implements security measures suitable for:

- Personal use
- Small to medium deployments
- Non-critical systems

For high-security environments or compliance requirements (HIPAA, PCI-DSS, etc.), additional security measures may be required.

## Security Tools

We use the following tools to maintain security:

- **bandit**: Security linting for Python code
- **pip-audit**: Python dependency vulnerability scanning
- **mypy**: Type checking to prevent type-related vulnerabilities
- **ruff**: Python linting
- **TruffleHog**: Credential scanning
- **GitHub Dependabot**: Automated dependency updates

## Acknowledgments

We thank the following researchers for responsibly disclosing security issues:

- (Your name could be here!)

## Contact

For security concerns, please use GitHub's private security advisory feature as described above.

For general questions, please use GitHub Issues.
