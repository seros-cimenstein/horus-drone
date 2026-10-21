FROM debian:trixie
RUN apt-get update \
 && apt-get install -y --no-install-recommends g++-mingw-w64-x86-64-posix cmake ninja-build git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
