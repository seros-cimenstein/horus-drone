#!/usr/bin/env bash

wait_for_gui_follow() {
  local gui_pid="$1" log_dir="$2" model_name="${3:-horus_iris}"
  local deadline=$((SECONDS + 60)) ready=0 services topics
  while (( SECONDS < deadline )); do
    if ! kill -0 "${gui_pid}" 2>/dev/null; then
      printf 'Gazebo GUI exited before CameraTracking initialized; inspect %s/gui.log.\n' \
        "${log_dir}" >&2
      return 1
    fi
    services="$(gz service -l 2>/dev/null || true)"
    topics="$(gz topic -l 2>/dev/null || true)"
    if [[ "${services}" == *"/gui/follow"* && "${topics}" == *"${model_name}"* ]]; then
      ready=1
      break
    fi
    sleep 0.5
  done
  if (( ready == 0 )); then
    printf 'Timed out waiting for %s and /gui/follow; inspect %s/gui.log.\n' \
      "${model_name}" "${log_dir}" >&2
    return 1
  fi

  if ! gz service -s /gui/follow --reqtype gz.msgs.StringMsg \
    --reptype gz.msgs.Boolean --timeout 2000 --req "data: \"${model_name}\"" >/dev/null; then
    printf 'Gazebo rejected the request to follow %s; inspect %s/gui.log.\n' \
      "${model_name}" "${log_dir}" >&2
    return 1
  fi
  if [[ "${services}" == *"/gui/follow/offset"* ]]; then
    if ! gz service -s /gui/follow/offset --reqtype gz.msgs.Vector3d \
      --reptype gz.msgs.Boolean --timeout 2000 --req 'x: -3, y: 0, z: 2' >/dev/null; then
      printf 'Gazebo rejected the follow camera offset; inspect %s/gui.log.\n' \
        "${log_dir}" >&2
      return 1
    fi
  else
    printf 'Gazebo follow offset service unavailable; keeping CameraTracking default.\n'
  fi
  printf 'Gazebo GUI is following %s.\n' "${model_name}"
}
