#!/bin/bash
# One-time root setup for Koko's two-screen layout. Run it on Koko from the user's account with
#
#   sudo bash ~/horus-display/install.sh
#
# (from the laptop: ssh -t koko sudo bash ~/horus-display/install.sh). sudo asks for the password
# in the terminal; nothing else ever needs it afterwards. It:
#   1. installs horus-display root-owned as /usr/local/sbin/horus-display;
#   2. lets the calling user run exactly that command through sudo without a password
#      (/etc/sudoers.d/horus-display), so the layout can be switched or reverted remotely;
#   3. turns on LightDM autologin for the calling user, so Koko comes back to the desktop by
#      itself after a display-manager restart or a reboot;
#   4. switches to two screens (horus-display two-screens), which restarts the display manager:
#      every window on Koko's screens closes (GTA, the Gazebo window); SSH and Docker keep running.
# Undo: sudo horus-display one-screen; sudo rm /etc/sudoers.d/horus-display
#       /etc/lightdm/lightdm.conf.d/70-horus-autologin.conf /usr/local/sbin/horus-display
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
user="${SUDO_USER:-}"
if [[ "${EUID}" -ne 0 || -z "${user}" || "${user}" == root ]]; then
  echo "run it from your own account through sudo: sudo bash ${here}/install.sh" >&2
  exit 1
fi
id "${user}" >/dev/null

install -o root -g root -m 0755 "${here}/horus-display" /usr/local/sbin/horus-display
echo "installed /usr/local/sbin/horus-display"

rule="$(mktemp)"
trap 'rm -f "${rule}"' EXIT
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/horus-display\n' "${user}" >"${rule}"
visudo -cqf "${rule}"
install -o root -g root -m 0440 "${rule}" /etc/sudoers.d/horus-display
echo "sudo rule: ${user} may run /usr/local/sbin/horus-display without a password"

mkdir -p /etc/lightdm/lightdm.conf.d
cat >/etc/lightdm/lightdm.conf.d/70-horus-autologin.conf <<EOF
# Written by HORUS scripts/koko/display/install.sh: log ${user} in without the greeter.
[Seat:*]
autologin-user=${user}
autologin-user-timeout=0
autologin-session=cinnamon
EOF
echo "LightDM autologin: ${user}, Cinnamon"

/usr/local/sbin/horus-display two-screens
