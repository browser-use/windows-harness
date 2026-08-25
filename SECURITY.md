# Security

Please report vulnerabilities privately through GitHub Security Advisories for
`browser-use/windows-harness`.

Windows Harness can control applications and read visible UI content with the
current user's privileges. Treat agent instructions and third-party UI as
untrusted input, review irreversible actions, and do not run the harness elevated
unless the target application genuinely requires it.
