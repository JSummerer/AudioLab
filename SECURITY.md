# Security

AudioLab is a public beta intended primarily for local use. Security fixes target the current
development branch; older snapshots do not have a separate support commitment.

## Report a vulnerability

Use GitHub's **Security → Report a vulnerability** for a private report:
[report privately](https://github.com/JSummerer/AudioLab/security/advisories/new).
Include the affected commit/version, operating system, reproduction steps and expected impact.
Use a minimal generated example where possible. Do not send passwords, browser cookies, access tokens
or personal recordings.

Please keep exploit details out of public issues until a fix or coordinated disclosure is ready.
Ordinary installation and audio-processing bugs belong in
[Issues](https://github.com/JSummerer/AudioLab/issues).

## Running safely

Keep the default loopback address for desktop use. If you deliberately serve AudioLab on a network,
keep its access token enabled and use it only on a network you trust. Do not expose it directly to the
public internet. See the [server guide](docs/USAGE.md#docker--home-server).

Update your source copy and dependencies using the [installation guide](docs/INSTALLATION.md#update-audiolab).
Only process files and links you trust; media handling also depends on external tools and libraries.
