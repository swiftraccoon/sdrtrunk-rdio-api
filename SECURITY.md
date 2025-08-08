# Security Policy

## Supported Versions

We release patches for security vulnerabilities. Currently supported versions:

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a Vulnerability

We take the security of RdioCallsAPI seriously. If you believe you have found a security vulnerability, please report it responsibly.

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
- Rate limiting per API key/IP

#### Input Validation

- Comprehensive input sanitization
- SQL injection prevention
- Path traversal protection
- File type validation
- File size limits
- Request size limits

#### Network Security

- Security headers (X-Frame-Options, X-Content-Type-Options, etc.)
- Content Security Policy (CSP)
- CORS configuration
- Rate limiting

#### Data Protection

- No sensitive data in logs
- Secure file storage
- Database query parameterization
- Error message sanitization

### Security Best Practices for Deployment

#### 1. Use HTTPS

Always deploy with HTTPS in production:

```yaml
server:
  host: 0.0.0.0
  port: 443
  ssl_cert: /path/to/cert.pem
  ssl_key: /path/to/key.pem
```

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
    - key: "your-secure-api-key"
      description: "SDRTrunk Instance 1"
      allowed_ips: ["192.168.1.100"]
      allowed_systems: ["1"]
```

#### 4. Database Security

- Use a dedicated database user with minimal privileges
- Enable database encryption at rest
- Regular backups with encryption

#### 5. File Storage Security

- Store files outside the web root
- Implement proper file permissions (644 for files, 755 for directories)
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

### Security Checklist

Before deploying to production:

- [ ] HTTPS configured and enforced
- [ ] Strong API keys generated and configured
- [ ] IP restrictions configured where appropriate
- [ ] Database user has minimal required privileges
- [ ] File upload directory is outside web root
- [ ] Proper file permissions set
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
- **safety**: Dependency vulnerability scanning
- **mypy**: Type checking to prevent type-related vulnerabilities
- **ruff**: Linting with security rules
- **GitHub Dependabot**: Automated dependency updates

## Acknowledgments

We thank the following researchers for responsibly disclosing security issues:

- (Your name could be here!)

## Contact

For security concerns, please use GitHub's private security advisory feature as described above.

For general questions, please use GitHub Issues.
