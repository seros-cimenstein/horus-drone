using System;
using System.Net;
using System.Net.Sockets;

namespace HorusGta
{
    /// <summary>
    /// The HORUS camera's three-axis gimbal, pointed by HORUS (README "Gimbal"): a yaw (pan) motor on
    /// the body, then roll, then pitch (tilt), the chain <see cref="PhysicsLink.CameraAxes"/> turns.
    /// Like a brushless gimbal with its own IMU it holds the setpoint against the vehicle's lean: the
    /// camera's pitch is relative to the horizon, its roll level, its yaw relative to the nose. A new
    /// setpoint is approached at <see cref="SlewDegS"/> per axis; holding it against the lean is
    /// immediate, within the motors' travel (Gazebo's gimbal_small_3d limits, horus/gimbal/gazebo.py).
    /// Until HORUS sends a setpoint the camera rests at <see cref="RestPitchDeg"/>, facing the nose.
    /// </summary>
    public sealed class GimbalMount
    {
        public const double PanLimitDeg = 160, RollLimitDeg = 30, TiltMinDeg = -135, TiltMaxDeg = 45;
        /// <summary>Setpoint range: horizon pitch (HORUS's gimbal.pitch_min/max_deg defaults) and nose-relative yaw.</summary>
        public const double PitchMinDeg = -90, PitchMaxDeg = 45, YawLimitDeg = PanLimitDeg;
        public const double RestPitchDeg = -45;
        public const double DefaultSlewDegS = 90, MinSlewDegS = 1, MaxSlewDegS = 1000;
        /// <summary>Heartbeat: a report at least this often (per second of sim time) while the attitude holds still.</summary>
        public const double DefaultRateHz = 50, MinRateHz = 20, MaxRateHz = 100;
        // Report times are sums of 1/frame rate steps: accept a heartbeat that is due up to this much early.
        const double DueToleranceS = 1e-9;
        const double Deg = 180 / Math.PI;

        public readonly double SlewDegS, RateHz, PeriodS;
        /// <summary>The latest report's datagram (<see cref="GimbalProtocol.StatusSize"/> bytes), valid once <see cref="Reports"/> > 0.</summary>
        public readonly byte[] Datagram = new byte[GimbalProtocol.StatusSize];
        double nextDueS = double.NaN, lastReportS = double.NaN;
        // The float32 angles of the latest report, as sent: camera roll, pitch, yaw, vehicle roll, pitch (deg).
        readonly float[] reported = new float[5];
        readonly float[] measured = new float[5];

        public GimbalMount(double slewDegS = DefaultSlewDegS, double rateHz = DefaultRateHz)
        {
            if (!(slewDegS >= MinSlewDegS && slewDegS <= MaxSlewDegS)) throw new ArgumentOutOfRangeException(nameof(slewDegS), slewDegS, "gimbal slew must be 1..1000 deg/s");
            if (!(rateHz >= MinRateHz && rateHz <= MaxRateHz)) throw new ArgumentOutOfRangeException(nameof(rateHz), rateHz, "gimbal report rate must be 20..100 Hz");
            SlewDegS = slewDegS;
            RateHz = rateHz;
            PeriodS = 1 / rateHz;
        }

        /// <summary>A mount per <paramref name="settings"/> (slew and report rate).</summary>
        public static GimbalMount Create(GimbalSettings settings) => new GimbalMount(settings.SlewDegS, settings.RateHz);

        /// <summary>HORUS's latest setpoint, clamped (deg): horizon pitch, nose-relative yaw.</summary>
        public double TargetPitchDeg { get; private set; } = RestPitchDeg;
        public double TargetYawDeg { get; private set; }
        /// <summary>True once HORUS has sent a setpoint since the last <see cref="Reset"/>.</summary>
        public bool HasSetpoint { get; private set; }
        /// <summary>The setpoint the mount holds now, moving toward the target at <see cref="SlewDegS"/> (deg).</summary>
        public double PitchDeg { get; private set; } = RestPitchDeg;
        public double YawDeg { get; private set; }
        /// <summary>Motor angles relative to the body (deg): +pan right, +roll right side down, +tilt up.</summary>
        public double PanDeg { get; private set; }
        public double RollDeg { get; private set; }
        public double TiltDeg { get; private set; } = RestPitchDeg;
        /// <summary>Reports so far; also the seq of the latest datagram.</summary>
        public long Reports { get; private set; }

        /// <summary>Take HORUS's setpoint: horizon pitch and nose-relative yaw (deg), clamped to the setpoint range.</summary>
        public void SetTarget(double pitchDeg, double yawDeg)
        {
            if (double.IsNaN(pitchDeg) || double.IsInfinity(pitchDeg) || double.IsNaN(yawDeg) || double.IsInfinity(yawDeg)) return;
            TargetPitchDeg = Clamp(pitchDeg, PitchMinDeg, PitchMaxDeg);
            TargetYawDeg = Clamp(yawDeg, -YawLimitDeg, YawLimitDeg);
            HasSetpoint = true;
        }

        /// <summary>Advance by <paramref name="h"/> seconds: slew the held setpoint toward the target, then hold it against the body's lean.</summary>
        public void Step(double h, QuadBody body, NedFrame frame)
        {
            double maxMove = SlewDegS * h;
            PitchDeg += Clamp(TargetPitchDeg - PitchDeg, -maxMove, maxMove);
            YawDeg += Clamp(TargetYawDeg - YawDeg, -maxMove, maxMove);
            Hold(body, frame);
        }

        /// <summary>Back to rest with no setpoint, at once (SITL restarted: the quad is back at its start).</summary>
        public void Reset(QuadBody body, NedFrame frame)
        {
            TargetPitchDeg = PitchDeg = RestPitchDeg;
            TargetYawDeg = YawDeg = 0;
            HasSetpoint = false;
            nextDueS = lastReportS = double.NaN;
            Hold(body, frame);
        }

        void Hold(QuadBody body, NedFrame frame)
        {
            Solve(body, frame, PitchDeg, YawDeg, out double pan, out double roll, out double tilt);
            PanDeg = Clamp(pan, -PanLimitDeg, PanLimitDeg);
            RollDeg = Clamp(roll, -RollLimitDeg, RollLimitDeg);
            TiltDeg = Clamp(tilt, TiltMinDeg, TiltMaxDeg);
        }

        /// <summary>
        /// Unclamped motor angles (deg) that put the camera at horizon <paramref name="pitchDeg"/>,
        /// nose-relative <paramref name="yawDeg"/> and level roll on <paramref name="body"/>: the
        /// camera-in-body rotation R_body^T R_camera, decomposed Z-X-Y as Rz(pan) Rx(roll) Ry(tilt) in
        /// the NED body frame (as solve_mount in horus/gimbal/gazebo.py).
        /// </summary>
        public static void Solve(QuadBody body, NedFrame frame, double pitchDeg, double yawDeg, out double panDeg, out double rollDeg, out double tiltDeg)
        {
            frame.Euler(body, out _, out _, out double bodyYaw);
            double p = pitchDeg / Deg, heading = bodyYaw + yawDeg / Deg;
            // Camera axes in NED (north, east, down): forward, right (level), down = forward x right.
            var cf = new Vec3(Math.Cos(p) * Math.Cos(heading), Math.Cos(p) * Math.Sin(heading), -Math.Sin(p));
            var cr = new Vec3(-Math.Sin(heading), Math.Cos(heading), 0);
            var cd = Vec3.Cross(cf, cr);
            Vec3 bf = frame.ToNed(body.F), br = frame.ToNed(body.R), bd = frame.ToNed(-body.U);
            rollDeg = Math.Asin(Clamp(Vec3.Dot(bd, cr), -1, 1)) * Deg;
            panDeg = Math.Atan2(-Vec3.Dot(bf, cr), Vec3.Dot(br, cr)) * Deg;
            tiltDeg = Math.Atan2(-Vec3.Dot(bd, cf), Vec3.Dot(bd, cd)) * Deg;
        }

        /// <summary>
        /// The camera's attitude as the motors hold it (deg): roll and pitch relative to the horizon,
        /// yaw relative to the nose; and the vehicle's roll and pitch.
        /// </summary>
        public void Measure(QuadBody body, NedFrame frame, out double cameraRollDeg, out double cameraPitchDeg, out double cameraYawDeg, out double vehicleRollDeg, out double vehiclePitchDeg)
        {
            PhysicsLink.CameraAxes(body, RollDeg, TiltDeg, PanDeg, out Vec3 r, out Vec3 f, out Vec3 u);
            frame.Euler(r, f, u, out double roll, out double pitch, out double yaw);
            frame.Euler(body, out double bodyRoll, out double bodyPitch, out double bodyYaw);
            cameraRollDeg = roll * Deg;
            cameraPitchDeg = pitch * Deg;
            cameraYawDeg = Math.IEEERemainder((yaw - bodyYaw) * Deg, 360);
            vehicleRollDeg = bodyRoll * Deg;
            vehiclePitchDeg = bodyPitch * Deg;
        }

        /// <summary>
        /// Encode a report into <see cref="Datagram"/> if one is due at sim time
        /// <paramref name="simTimeS"/>: on the first call, on every call (physics step) where any
        /// reported angle, as the float32 the datagram carries, differs from the last report's, at
        /// once when the sim clock goes back, and otherwise as a heartbeat one period after the last
        /// report. True when it encoded one.
        /// </summary>
        public bool Report(double simTimeS, QuadBody body, NedFrame frame)
        {
            Measure(body, frame, out double cr, out double cp, out double cy, out double vr, out double vp);
            measured[0] = (float)cr; measured[1] = (float)cp; measured[2] = (float)cy; measured[3] = (float)vr; measured[4] = (float)vp;
            bool restart = double.IsNaN(nextDueS) || simTimeS < lastReportS;
            bool changed = false;
            for (int i = 0; i < measured.Length; i++) changed |= measured[i] != reported[i];
            if (!restart && !changed && simTimeS < nextDueS - DueToleranceS) return false;
            nextDueS = simTimeS + PeriodS;
            lastReportS = simTimeS;
            Array.Copy(measured, reported, measured.Length);
            Reports++;
            GimbalProtocol.EncodeStatus(Datagram, (uint)Reports, RangeProtocol.TimeMs(simTimeS), measured[0], measured[1], measured[2], measured[3], measured[4]);
            return true;
        }

        static double Clamp(double v, double lo, double hi) => Math.Max(lo, Math.Min(hi, v));
    }

    /// <summary>
    /// Gimbal datagrams between HORUS's GTA gimbal device (horus/gimbal/gta.py) and the mod,
    /// little-endian.
    /// Setpoint, 20 bytes, to the mod: magic "HGIM" | u32 version 1 | u32 seq | f32 pitch (deg,
    /// relative to the horizon, negative down) | f32 yaw (deg, relative to the nose, positive right).
    /// Status, 36 bytes, from the mod: magic "HGAT" | u32 version 1 | u32 seq | u32 time_ms (physics
    /// sim time) | f32 camera roll | f32 camera pitch (both relative to the horizon) | f32 camera yaw
    /// (relative to the nose) | f32 vehicle roll | f32 vehicle pitch (deg).
    /// </summary>
    public static class GimbalProtocol
    {
        public const int SetpointSize = 20, StatusSize = 36;
        public const uint Version = 1;
        static readonly byte[] SetpointMagic = { (byte)'H', (byte)'G', (byte)'I', (byte)'M' };
        static readonly byte[] StatusMagic = { (byte)'H', (byte)'G', (byte)'A', (byte)'T' };

        /// <summary>Write one setpoint datagram into <paramref name="into"/> (at least <see cref="SetpointSize"/> bytes).</summary>
        public static void EncodeSetpoint(byte[] into, uint seq, float pitchDeg, float yawDeg)
        {
            if (into.Length < SetpointSize) throw new ArgumentException("datagram buffer shorter than 20 bytes", nameof(into));
            Array.Copy(SetpointMagic, into, 4);
            RangeProtocol.WriteU32(into, 4, Version);
            RangeProtocol.WriteU32(into, 8, seq);
            RangeProtocol.WriteU32(into, 12, RangeProtocol.F32Bits(pitchDeg));
            RangeProtocol.WriteU32(into, 16, RangeProtocol.F32Bits(yawDeg));
        }

        /// <summary>Decode a setpoint datagram of <paramref name="length"/> bytes; false for a wrong size, magic or version, or a non-finite angle.</summary>
        public static bool TryDecodeSetpoint(byte[] data, int length, out uint seq, out float pitchDeg, out float yawDeg)
        {
            seq = 0; pitchDeg = yawDeg = 0;
            if (data == null || length != SetpointSize || data.Length < SetpointSize) return false;
            for (int i = 0; i < 4; i++) if (data[i] != SetpointMagic[i]) return false;
            if (RangeProtocol.ReadU32(data, 4) != Version) return false;
            seq = RangeProtocol.ReadU32(data, 8);
            pitchDeg = RangeProtocol.F32(RangeProtocol.ReadU32(data, 12));
            yawDeg = RangeProtocol.F32(RangeProtocol.ReadU32(data, 16));
            return !float.IsNaN(pitchDeg) && !float.IsInfinity(pitchDeg) && !float.IsNaN(yawDeg) && !float.IsInfinity(yawDeg);
        }

        /// <summary>Write one status datagram into <paramref name="into"/> (at least <see cref="StatusSize"/> bytes).</summary>
        public static void EncodeStatus(byte[] into, uint seq, uint timeMs, float cameraRollDeg, float cameraPitchDeg, float cameraYawDeg, float vehicleRollDeg, float vehiclePitchDeg)
        {
            if (into.Length < StatusSize) throw new ArgumentException("datagram buffer shorter than 36 bytes", nameof(into));
            Array.Copy(StatusMagic, into, 4);
            RangeProtocol.WriteU32(into, 4, Version);
            RangeProtocol.WriteU32(into, 8, seq);
            RangeProtocol.WriteU32(into, 12, timeMs);
            float[] values = { cameraRollDeg, cameraPitchDeg, cameraYawDeg, vehicleRollDeg, vehiclePitchDeg };
            for (int i = 0; i < values.Length; i++) RangeProtocol.WriteU32(into, 16 + 4 * i, RangeProtocol.F32Bits(values[i]));
        }
    }

    /// <summary>
    /// The gimbal's UDP link ([gimbal]): setpoints arrive on bind:port, status datagrams go to target.
    /// Non-blocking; the caller drains it once per script tick.
    /// </summary>
    public sealed class GimbalChannel : IDisposable
    {
        readonly Socket socket;
        readonly IPEndPoint target;
        readonly byte[] buffer = new byte[64];
        EndPoint from = new IPEndPoint(IPAddress.Any, 0);

        public GimbalChannel(GimbalSettings settings)
        {
            target = new IPEndPoint(IPAddress.Parse(settings.TargetHost), settings.TargetPort);
            socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp) { Blocking = false };
            try { socket.Bind(new IPEndPoint(IPAddress.Parse(settings.Bind), settings.Port)); }
            catch { socket.Close(); throw; }
        }

        /// <summary>Setpoints taken and datagrams rejected (wrong size, magic, version or a non-finite angle) so far.</summary>
        public long Received { get; private set; }
        public long Rejected { get; private set; }
        /// <summary>The UDP port setpoints arrive on (the bound port; [gimbal] port 0 picks a free one).</summary>
        public int LocalPort => ((IPEndPoint)socket.LocalEndPoint).Port;

        /// <summary>Hand every waiting setpoint to <paramref name="mount"/> (the last one wins); returns how many were taken.</summary>
        public int Drain(GimbalMount mount)
        {
            int taken = 0;
            while (socket.Available > 0)
            {
                int n;
                try { n = socket.ReceiveFrom(buffer, ref from); }
                catch (SocketException e) when (e.SocketErrorCode == SocketError.MessageSize) { Rejected++; continue; }
                catch (SocketException) { break; }
                if (GimbalProtocol.TryDecodeSetpoint(buffer, n, out _, out float pitch, out float yaw))
                {
                    mount.SetTarget(pitch, yaw);
                    Received++;
                    taken++;
                }
                else Rejected++;
            }
            return taken;
        }

        /// <summary>Send <paramref name="mount"/>'s latest status datagram; throws on a socket error.</summary>
        public void Send(GimbalMount mount) => socket.SendTo(mount.Datagram, 0, GimbalProtocol.StatusSize, SocketFlags.None, target);

        public void Dispose() => socket.Close();
    }
}
