# Security policy

## Supported versions

Security fixes go into the latest release. Version 1.0.0 is the current release.

## Reporting a vulnerability

Please report vulnerabilities privately. Do not open a public issue.

1. Open the [Security tab](https://github.com/VukoP/rubato-waveform-generator/security) of this repository.
2. Choose Report a vulnerability, and describe the problem, the affected version, your operating system, and steps to reproduce it.

This uses GitHub private vulnerability reporting, so only the maintainer sees the report.

## What is in scope

The app opens a native window around a web interface. That interface talks to a small HTTP server that runs inside the app. The local model works like this:

- The server listens on 127.0.0.1 only, on a random port chosen at startup.
- Each run creates a random session token. API requests must carry it.
- Every request is checked for an exact `Host` header, which stops DNS rebinding, and for an exact `Origin` header when one is sent.
- No CORS headers are sent, and only GET, HEAD and POST are accepted.
- The interface is served with a strict Content Security Policy that only allows the app's own origin.
- Static files are served from a fixed list built at startup. The audio file served to the preview is the one chosen in the session, never a path taken from a request.
- Every FFmpeg and ffprobe call on user input restricts protocols to `file` and `pipe`, and restricts input formats to an allow-list of plain audio and video containers. Only the file extensions the app lists as supported are accepted.

In scope are problems that break these properties or that let a web page, another local user or a crafted audio file read files, run code, or reach the network through the app. Examples: a way to bypass the Host, Origin or token checks, a path traversal, a way to make FFmpeg read other local files or open network resources, or an injection into the command lines the app builds.

## Out of scope

- Vulnerabilities in FFmpeg itself. Report them to the [FFmpeg project](https://ffmpeg.org/security.html).
- Vulnerabilities in Python, pywebview, Pillow, numpy or other dependencies. Report them upstream. If this project's use of a dependency makes it exploitable, that is in scope.
- Attacks that need an attacker who already runs code as the same user on the machine.
- The macOS build being ad-hoc signed and not notarized, and the Windows build being unsigned. This is documented behavior.

## Response

This is a small open-source project maintained by one person, so there is no guaranteed response time. Reports are read, and confirmed problems are fixed in a new release.
