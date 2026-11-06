using System;

namespace HorusGta
{
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
        public readonly GimbalMount Gimbal;
        readonly Vec3 start;
        readonly double startHeadingRad;
        readonly double[] throttle = new double[4];
        ServoFrame? pending;
        bool hasFrame;
        string lastReply;

        public PhysicsLink(QuadBody body, NedFrame frame, GimbalMount gimbal, Vec3 start, double startHeadingRad)
        {
            Body = body; Frame = frame; Gimbal = gimbal;
            this.start = start; this.startHeadingRad = startHeadingRad;
            body.Place(start, startHeadingRad);
            gimbal.Reset(body, frame);
        }

        public double SimTimeS { get; private set; }
        public uint LastFrame { get; private set; }
        public ushort FrameRate { get; private set; }
        public long TotalSteps { get; private set; }
        /// <summary>Called after each step, before its answer goes to SITL (e.g. for <see cref="RangeSensors.Update"/> and <see cref="GimbalMount.Report"/> at <see cref="SimTimeS"/>).</summary>
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
            Gimbal.Step(h, Body, Frame);
            lastReply = SitlJson.Encode(SimTimeS, Body, Frame);
            Advanced?.Invoke();
        }

        /// <summary>
        /// The HORUS camera on the quad, as a pose for <see cref="PoseMapping.Map"/> relative to the
        /// ground at home, height <paramref name="groundZ"/>: at the airframe's camera mount,
        /// oriented as the body turned by the gimbal's motors (<see cref="CameraAxes"/>). ArduPilot's
        /// NED origin is the resting body centre, so this pose's down differs from ArduPilot's by the
        /// rest height plus the mount height.
        /// </summary>
        public NedPose CameraPose(double groundZ, double hfovDeg)
        {
            Vec3 mount = Body.ToWorld(Body.Frame.CameraMountM);
            Vec3 p = Frame.ToNed(mount - new Vec3(Frame.Origin.X, Frame.Origin.Y, groundZ));
            CameraAxes(Body, Gimbal.RollDeg, Gimbal.TiltDeg, Gimbal.PanDeg, out Vec3 r, out Vec3 f, out Vec3 u);
            Frame.Euler(r, f, u, out double roll, out double pitch, out double yaw);
            const double Deg = 180 / Math.PI;
            return new NedPose
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
        /// World axes (right, forward, up) of a camera on a three-axis gimbal fixed to the body,
        /// chained from the body out: yaw (pan) motor, then roll, then pitch (tilt), angles relative
        /// to the body. <see cref="GimbalMount"/> turns the motors against the body's lean to hold
        /// the camera's horizon pitch, level roll and nose-relative yaw.
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

        /// <summary>SITL restarted: the quad goes back to its start, the clock to zero, the gimbal to rest.</summary>
        public void Restart()
        {
            Body.Place(start, startHeadingRad);
            SimTimeS = 0;
            hasFrame = false;
            pending = null;
            Gimbal.Reset(Body, Frame);
        }
    }
}
