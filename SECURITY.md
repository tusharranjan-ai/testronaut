# Security policy

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through GitHub:
[Report a vulnerability](https://github.com/tusharranjan-ai/testronaut/security/advisories/new).

Include what you found, how to reproduce it, and the impact you expect. You can
expect an acknowledgement within a few days. Testronaut is maintained by one
person, so fixes are best-effort, but anything that lets a remote party run code
or read files will be treated as urgent.

## Scope and trust model

Testronaut is a **single-user tool for your own machine**. It has no
authentication by design, so the following are *expected* behaviour when the
service is exposed to a network, not vulnerabilities:

- anyone who can reach port 8000 can use the whole API;
- the Docker Compose setup mounts `/var/run/docker.sock` into the backend.

Both are why the Compose file binds to `127.0.0.1` only. See "Security and trust
model" in the [README](README.md).

In scope: the sandbox escaping its limits, path traversal in the generated-file
endpoints, SSRF past the spec-URL guard, secrets written somewhere they should
not be, and anything that makes a malicious OpenAPI spec execute code outside the
container.

## Supported versions

Only the latest commit on `main`.
