using System;

namespace HorusGta
{
    /// <summary>
    /// Gimbal angles from a servo-driven mount (ArduPilot MNTx_TYPE 1, the physics-mode stand-in in sim/scripts/start_gta_sitl.sh):
    /// SERVO9 roll (function 8), SERVO10 tilt (7), SERVO11 pan (6). The servos sit on the body and
    /// ArduPilot already subtracts the vehicle's lean from the roll and tilt outputs, so these are
    /// angles relative to the body, not to the horizon.
    /// </summary>
    public sealed class GimbalServo
    {
        /// <summary>1-based servo output channels driving mount roll, pitch and yaw.</summary>
        public int RollChannel = 9, PitchChannel = 10, YawChannel = 11;
        /// <summary>MNT1_ROLL_MIN/MAX (ArduPilot defaults), MNT1_PITCH_MIN/MAX and MNT1_YAW_MIN/MAX of start_gta_sitl.sh's physics mode.</summary>
        public double RollMinDeg = -30, RollMaxDeg = 30, PitchMinDeg = -135, PitchMaxDeg = 45, YawMinDeg = -160, YawMaxDeg = 160;
        public int ServoMinUs = 1100, ServoMaxUs = 1900;

        /// <summary>Roll (deg, right side down) for the frame's PWM, or null when the channel is not driven.</summary>
        public double? RollDeg(ServoFrame frame) => Angle(frame, RollChannel, RollMinDeg, RollMaxDeg);
        /// <summary>Pitch (deg, negative down) for the frame's PWM, or null when the channel is not driven.</summary>
        public double? PitchDeg(ServoFrame frame) => Angle(frame, PitchChannel, PitchMinDeg, PitchMaxDeg);
        /// <summary>Yaw (deg, right of the nose) for the frame's PWM, or null when the channel is not driven.</summary>
        public double? YawDeg(ServoFrame frame) => Angle(frame, YawChannel, YawMinDeg, YawMaxDeg);

        double? Angle(ServoFrame frame, int channel, double minDeg, double maxDeg)
        {
            int i = channel - 1;
            if (frame.Pwm == null || i < 0 || i >= frame.Pwm.Length) return null;
            ushort pwm = frame.Pwm[i];
            if (pwm < 500 || ServoMaxUs <= ServoMinUs) return null;
            double f = Math.Max(0, Math.Min(1, (pwm - ServoMinUs) / (double)(ServoMaxUs - ServoMinUs)));
            return minDeg + f * (maxDeg - minDeg);
        }
    }

    /// <summary>Outcome of one <see cref="PhysicsLink.Pump"/>.</summary>
    public struct PumpResult
    {
        public int Steps, Resent, Restarts;
        /// <summary>No frame arrived within the receive timeout (SITL busy, stopped or not started).</summary>
        public bool TimedOut;
        /// <summary>The time budget ran out before the sim reached its target.</summary>
        public bool BudgetHit;
    }

    /// <summary>
    /// Lockstep link to ArduPilot's JSON SITL backend. Each new servo frame advances the quad by
    /// 1 / frame rate and is answered with the new state; a repeated frame (SITL re-sends when an
    /// answer was lost) gets the last answer again; a lower frame count means SITL restarted, so the
    /// quad goes back to its start. SITL waits for every answer, so the caller paces the sim by only
    /// answering until the sim time reaches a target (real time, bounded by a per-call budget); a
    /// frame that arrives at the target is held and answered on the next call.
    /// </summary>
    public sealed class PhysicsLink
    {
        public readonly QuadBody Body;
        public readonly NedFrame Frame;
        public readonly GimbalServo Gimbal;
        readonly Vec3 start;
        readonly double startHeadingRad;
        readonly double[] throttle = new double[4];
        ServoFrame? pending;
        bool hasFrame;
        string lastReply;

        public PhysicsLink(QuadBody body, NedFrame frame, GimbalServo gimbal, Vec3 start, double startHeadingRad)
        {
            Body = body; Frame = frame; Gimbal = gimbal;
            this.start = start; this.startHeadingRad = startHeadingRad;
            body.Place(start, startHeadingRad);
        }

        public double SimTimeS { get; private set; }
        public uint LastFrame { get; private set; }
        public ushort FrameRate { get; private set; }
        /// <summary>Latest mount angles relative to the body from the servo outputs (deg); null until a frame drives them.</summary>
        public double? GimbalRollDeg { get; private set; }
        public double? GimbalPitchDeg { get; private set; }
        public double? GimbalYawDeg { get; private set; }
        public long TotalSteps { get; private set; }
        /// <summary>Called after each step, before its answer goes to SITL (e.g. for <see cref="RangeSensors.Update"/> at <see cref="SimTimeS"/>).</summary>
        public Action Advanced;

        /// <summary>
        /// Answer frames until the sim time reaches <paramref name="simTargetS"/>, a frame does not
        /// arrive within the receive timeout, or <paramref name="outOfBudget"/> says stop.
        /// <paramref name="receive"/> returns the next decoded frame or null on timeout;
        /// <paramref name="reply"/> sends a state line to the frame's sender.
        /// </summary>
        public PumpResult Pump(double simTargetS, Func<ServoFrame?> receive, Action<string> reply, Func<bool> outOfBudget)
        {
            var result = new PumpResult();
            for (;;)
            {
                if (outOfBudget()) { result.BudgetHit = SimTimeS < simTargetS; break; }
                ServoFrame? next = pending ?? receive();
                pending = null;
                if (!next.HasValue) { result.TimedOut = true; break; }
                var f = next.Value;
                if (hasFrame && f.FrameCount == LastFrame)
                {
                    reply(lastReply);
                    result.Resent++;
                    continue;
                }
                if (hasFrame && f.FrameCount < LastFrame)
                {
                    Restart();
                    result.Restarts++;
                }
                if (hasFrame && SimTimeS >= simTargetS) { pending = f; break; }
                Advance(f);
                reply(lastReply);
                result.Steps++;
            }
            return result;
        }

        void Advance(ServoFrame f)
        {
            double h = 1.0 / f.FrameRate;
            for (int i = 0; i < 4; i++) throttle[i] = SitlJson.Throttle(f.Pwm[i]);
            Body.Step(h, throttle);
            SimTimeS += h;
            TotalSteps++;
            LastFrame = f.FrameCount;
            FrameRate = f.FrameRate;
            hasFrame = true;
            GimbalRollDeg = Gimbal.RollDeg(f) ?? GimbalRollDeg;
            GimbalPitchDeg = Gimbal.PitchDeg(f) ?? GimbalPitchDeg;
            GimbalYawDeg = Gimbal.YawDeg(f) ?? GimbalYawDeg;
            lastReply = SitlJson.Encode(SimTimeS, Body, Frame);
            Advanced?.Invoke();
        }

        /// <summary>
        /// The HORUS camera on the quad, as a pose packet for <see cref="PoseMapping.Map"/> with
        /// ground height <paramref name="groundZ"/> (pose mode's home): at the airframe's camera
        /// mount, oriented as the body turned by the gimbal servos (<see cref="CameraAxes"/>).
        /// ArduPilot's NED origin is the resting body centre, so this packet's down differs from
        /// ArduPilot's by the rest height plus the mount height.
        /// </summary>
        public PosePacket CameraPose(double groundZ, double hfovDeg)
        {
            Vec3 mount = Body.ToWorld(Body.Frame.CameraMountM);
            Vec3 p = Frame.ToNed(mount - new Vec3(Frame.Origin.X, Frame.Origin.Y, groundZ));
            CameraAxes(Body, GimbalRollDeg ?? 0, GimbalPitchDeg ?? 0, GimbalYawDeg ?? 0, out Vec3 r, out Vec3 f, out Vec3 u);
            Frame.Euler(r, f, u, out double roll, out double pitch, out double yaw);
            const double Deg = 180 / Math.PI;
            return new PosePacket
            {
                Seq = LastFrame,
                NorthM = p.X, EastM = p.Y, DownM = p.Z,
                RollDeg = (float)(roll * Deg),
                PitchDeg = (float)(pitch * Deg),
                YawDeg = (float)(yaw * Deg),
                HfovDeg = (float)hfovDeg,
            };
        }

        /// <summary>
        /// World axes (right, forward, up) of a camera on a servo gimbal fixed to the body, chained
        /// like a three-axis gimbal from the body out: yaw (pan) motor, then roll, then pitch (tilt).
        /// ArduPilot's servo mount subtracts the vehicle's roll and pitch from those outputs, which
        /// on this chain levels the camera exactly: body Rz(yaw) Ry(pitch) Rx(roll) times the
        /// mount's Rx(-roll) Ry(tilt - pitch) is Rz(yaw) Ry(tilt).
        /// </summary>
        public static void CameraAxes(QuadBody body, double rollDeg, double pitchDeg, double yawDeg, out Vec3 right, out Vec3 forward, out Vec3 up)
        {
            double y = yawDeg * Math.PI / 180, p = pitchDeg * Math.PI / 180, r = rollDeg * Math.PI / 180;
            // Yaw right turns about down (= -up); roll right about forward; pitch up about right.
            forward = Vec3.Rotate(body.F, body.U, -y);
            right = Vec3.Rotate(body.R, body.U, -y);
            up = body.U;
            right = Vec3.Rotate(right, forward, r);
            up = Vec3.Rotate(up, forward, r);
            forward = Vec3.Rotate(forward, right, p);
            up = Vec3.Rotate(up, right, p);
        }

        /// <summary>SITL restarted: the quad goes back to its start, the clock to zero.</summary>
        public void Restart()
        {
            Body.Place(start, startHeadingRad);
            SimTimeS = 0;
            hasFrame = false;
            pending = null;
            GimbalPitchDeg = null;
        }
    }
}
