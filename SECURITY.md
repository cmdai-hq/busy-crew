# Security

Do not put API tokens, conversation contents, runtime JSON files or unredacted
logs in public issues. To report a security vulnerability, use GitHub's **Report
a vulnerability** action in this repository's Security tab when it is available.
If it is unavailable, contact the repository owner through their GitHub profile
to arrange a private reporting channel before sending sensitive details. A
public issue may request a private contact without disclosing the vulnerability.

Include the version/commit, macOS and Python versions, the affected operation,
expected and actual behavior, and a reproduction with synthetic data. Please
distinguish a result reproduced on fixtures from a result observed on hardware.
This small project currently supports the latest development revision; it does
not promise a response deadline or maintain backports for older revisions.

Busy Crew reads local agent metadata and sends a bitmap to the configured
BUSY Bar; [PRIVACY](docs/privacy.md) lists what it reads, keeps and sends. Use a device on a trusted network. With HTTP, the API key and bitmap
are not protected in transit; use HTTPS when supported by the device. Runtime
files and installation backups are private local files, not an encrypted vault.
