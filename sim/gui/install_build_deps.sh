#!/usr/bin/env bash
set -euo pipefail

apt-get update
apt-get install -y --no-install-recommends \
  build-essential \
  cmake \
  libgz-gui8-dev \
  libgz-msgs10-dev \
  libgz-transport13-dev \
  qtbase5-dev \
  qtdeclarative5-dev \
  qtquickcontrols2-5-dev
apt-get clean
rm -rf /var/lib/apt/lists/*
