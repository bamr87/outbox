---
title: "$title"
description: "Address already in use, and nothing says by whom. Two commands answer it, and each has one flag that decides whether the answer is true."
date: $date
categories: [Hacks]
tags: [shell, docker]
author: edge
excerpt: "lsof and ss both find the process holding a port. Both also show an empty result for a port they are not allowed to see."
permalink: /hacks/find-the-process-on-port-8080/
---
You start the dev server and get `address already in use`. Something owns port 8080, and the error will not tell you what. Two commands will, and each has one flag that decides whether it tells you the truth.

```bash
lsof -nP -iTCP:8080 -sTCP:LISTEN
```

`-iTCP:8080` filters to that port, `-sTCP:LISTEN` drops clients that are merely connected to it, and `-nP` skips DNS and port-name lookups so the answer does not wait on a resolver. Run it without `sudo` and lsof lists only your own processes. A port held by root or by another user comes back empty, which looks exactly like a free port.

```bash
sudo lsof -nP -iTCP:8080 -sTCP:LISTEN
```

On Linux, `ss` answers the same question from the kernel's socket table:

```bash
sudo ss -ltnp 'sport = :8080'
```

`-l` means listening, `-t` TCP, `-n` numeric and `-p` the owning process. Without root, `-p` leaves the process column blank for sockets you do not own. The flag everyone forgets is not a flag; it is `sudo`.

## Before you kill it

The PID you find may belong to a container runtime that will take the port straight back. If the command name is `docker-proxy` on Linux or `com.docker.backend` on macOS, the port is published by a container: stop the container instead of the process.
