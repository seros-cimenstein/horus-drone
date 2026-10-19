FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG ARDUPILOT_REF=Copter-4.7.1
ARG ARDUPILOT_GAZEBO_COMMIT=082a0fe231f6e63bc8d1598f1cba461d9e2ea7f5
ARG ARDUCOPTER_SHA256=011627d41dd95640c3aca02d45283474be48721c8b1bb7a4b234d38b69063482

ENV DEBIAN_FRONTEND=noninteractive \
    GZ_VERSION=harmonic \
    GZ_SIM_SYSTEM_PLUGIN_PATH=/opt/ardupilot_gazebo/build \
    GZ_GUI_PLUGIN_PATH=/opt/horus-gui/lib \
    HORUS_GUI_CONFIG=/opt/horus-gui/horus_demo.config \
    GZ_SIM_RESOURCE_PATH=/opt/ardupilot_gazebo/models:/opt/ardupilot_gazebo/worlds:/horus/sim/models:/horus/sim/worlds \
    PATH=/opt/horus-venv/bin:/opt/ardupilot/Tools/autotest:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/horus

RUN printf 'Acquire::ForceIPv4 "true";\n' > /etc/apt/apt.conf.d/99force-ipv4 \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -4 -fsSL https://packages.osrfoundation.org/gazebo.gpg \
        -o /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg \
    && printf 'deb [arch=%s signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] https://packages.osrfoundation.org/gazebo/ubuntu-stable %s main\n' \
        "$(dpkg --print-architecture)" "$(. /etc/os-release && printf '%s' "$VERSION_CODENAME")" \
        > /etc/apt/sources.list.d/gazebo-stable.list \
    && apt-get update \
    && apt-get install -y \
        gstreamer1.0-gl \
        gstreamer1.0-libav \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-tools \
        gz-harmonic \
        libdebuginfod1 \
        python3-defusedxml \
        python3-future \
        python3-gz-msgs10 \
        python3-gz-transport13 \
        python3-lxml \
        python3-numpy \
        python3-opencv \
        python3-pexpect \
        python3-pip \
        python3-psutil \
        python3-pyparsing \
        python3-serial \
        python3-venv \
        python3-yaml \
    && python3 -m venv --system-site-packages /opt/horus-venv \
    && /opt/horus-venv/bin/pip install --no-cache-dir pynmeagps \
    && mkdir -p /opt/ardupilot/build/sitl/bin \
    && curl -4 -fsSL https://firmware.ardupilot.org/Copter/stable-4.7.1/SITL_x86_64_linux_gnu/arducopter \
        -o /opt/ardupilot/build/sitl/bin/arducopter \
    && printf '%s  %s\n' "${ARDUCOPTER_SHA256}" /opt/ardupilot/build/sitl/bin/arducopter | sha256sum --check - \
    && chmod 0755 /opt/ardupilot/build/sitl/bin/arducopter \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

# Compile and install the Gazebo plugin; keep its toolchain packages installed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        libgstreamer-plugins-base1.0-dev \
        libgstreamer1.0-dev \
        libgz-sim8-dev \
        libopencv-dev \
        pkg-config \
        rapidjson-dev \
    && git clone --filter=blob:none --no-checkout --depth 1 --branch "${ARDUPILOT_REF}" \
        https://github.com/ArduPilot/ardupilot.git /tmp/ardupilot-source \
    && git -C /tmp/ardupilot-source sparse-checkout init --cone \
    && git -C /tmp/ardupilot-source sparse-checkout set Tools/autotest Tools/scripts ArduCopter \
    && git -C /tmp/ardupilot-source checkout --detach "${ARDUPILOT_REF}" \
    && cp -a /tmp/ardupilot-source/. /opt/ardupilot/ \
    && git clone --filter=blob:none --no-checkout --depth 1 \
        https://github.com/ArduPilot/ardupilot_gazebo.git /tmp/ardupilot_gazebo \
    && git -C /tmp/ardupilot_gazebo sparse-checkout init --cone \
    && git -C /tmp/ardupilot_gazebo sparse-checkout set config include models src worlds \
    && git -C /tmp/ardupilot_gazebo fetch --depth 1 origin "${ARDUPILOT_GAZEBO_COMMIT}" \
    && git -C /tmp/ardupilot_gazebo checkout --detach FETCH_HEAD \
    && cmake -S /tmp/ardupilot_gazebo -B /tmp/ardupilot_gazebo/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/tmp/ardupilot_gazebo-install \
    && cmake --build /tmp/ardupilot_gazebo/build --parallel 4 \
    && cmake --install /tmp/ardupilot_gazebo/build \
    && mkdir -p /opt/ardupilot_gazebo/build /opt/ardupilot_gazebo/config \
        /opt/ardupilot_gazebo/models /opt/ardupilot_gazebo/worlds \
    && cp -a /tmp/ardupilot_gazebo-install/lib/ardupilot_gazebo/. /opt/ardupilot_gazebo/build/ \
    && cp -a /tmp/ardupilot_gazebo-install/share/ardupilot_gazebo/config/. /opt/ardupilot_gazebo/config/ \
    && cp -a /tmp/ardupilot_gazebo-install/share/ardupilot_gazebo/models/. /opt/ardupilot_gazebo/models/ \
    && cp -a /tmp/ardupilot_gazebo-install/share/ardupilot_gazebo/worlds/. /opt/ardupilot_gazebo/worlds/ \
    && rm -rf /opt/ardupilot/.git /tmp/ardupilot-source /tmp/ardupilot_gazebo /tmp/ardupilot_gazebo-install \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 sim \
    && useradd --uid 1000 --gid sim --create-home --shell /bin/bash sim
RUN /opt/horus-venv/bin/pip install --no-cache-dir 'setuptools>=68' wheel
RUN gz sim --version && command -v sim_vehicle.py
RUN output="$(gz sim -s -r --headless-rendering -v4 --iterations 50 shapes.sdf 2>&1)" \
    && printf '%s\n' "$output" \
    && ! printf '%s\n' "$output" | grep -E 'Failed to (load|find plugin)'
# The Qt GUI plugin depends only on sim/gui; build it before the HORUS source so README or
# package edits rebuild only the pip layer below.
COPY sim/gui/install_build_deps.sh /tmp/install_horus_gui_build_deps.sh
RUN bash /tmp/install_horus_gui_build_deps.sh \
    && rm /tmp/install_horus_gui_build_deps.sh
COPY sim/gui /tmp/horus-gui-source
RUN bash /tmp/horus-gui-source/build_plugin.sh /tmp/horus-gui-source \
    && rm -rf /tmp/horus-gui-source
COPY pyproject.toml README.md /tmp/horus-source/
COPY horus /tmp/horus-source/horus
RUN /opt/horus-venv/bin/pip install --no-cache-dir --no-build-isolation \
        '/tmp/horus-source[server,perception,test]' \
    && rm -rf /tmp/horus-source


USER sim
WORKDIR /opt/ardupilot

EXPOSE 5760/tcp 5762/tcp 5763/tcp 5600/udp 9002/udp
