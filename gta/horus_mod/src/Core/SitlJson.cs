using System;
using System.Globalization;
using System.Text;

namespace HorusGta
{
    /// <summary>One servo-output frame from ArduPilot's JSON SITL backend.</summary>
    public struct ServoFrame
    {
        public ushort FrameRate;
        public uint FrameCount;
        /// <summary>PWM per output channel (µs); channel n is index n-1. 16 or 32 entries.</summary>
        public ushort[] Pwm;
    }

    /// <summary>
    /// ArduPilot SITL JSON backend wire format (libraries/SITL/examples/JSON/readme.md). SITL sends a
    /// binary servo frame to the physics backend's UDP port (9002); the backend answers each new
    /// frame with one JSON state line to the sender's address. SITL steps only when it gets an
    /// answer (lockstep), so its clock is the backend's <c>timestamp</c>.
    /// </summary>
    public static class SitlJson
    {
        public const ushort Magic16 = 18458, Magic32 = 29569;

        /// <summary>Decode a servo frame; false for any other size or magic.</summary>
        public static bool TryDecode(byte[] data, int length, out ServoFrame frame)
        {
            frame = default;
            if (length < 8) return false;
            ushort magic = BitConverter.ToUInt16(data, 0);
            int channels = magic == Magic16 ? 16 : magic == Magic32 ? 32 : 0;
            if (channels == 0 || length != 8 + 2 * channels) return false;
            frame.FrameRate = BitConverter.ToUInt16(data, 2);
            frame.FrameCount = BitConverter.ToUInt32(data, 4);
            frame.Pwm = new ushort[channels];
            for (int i = 0; i < channels; i++) frame.Pwm[i] = BitConverter.ToUInt16(data, 8 + 2 * i);
            return frame.FrameRate > 0;
        }

        /// <summary>Throttle 0..1 of a motor PWM (1000..2000 µs).</summary>
        public static double Throttle(ushort pwm) => Math.Max(0, Math.Min(1, (pwm - 1000) / 1000.0));

        /// <summary>Accelerometer range (±16 g, as common flight-controller IMUs): an impact reads as clipping.</summary>
        public const double AccelRangeMs2 = 16 * QuadBody.G;
        /// <summary>Gyro range (±2000 deg/s).</summary>
        public const double GyroRangeRadS = 2000 * Math.PI / 180;

        /// <summary>
        /// The state line for <paramref name="body"/> at physics time <paramref name="timestampS"/>:
        /// position and velocity NED relative to <paramref name="frame"/>'s origin, body (FRD) gyro and
        /// specific force saturated at the sensor ranges, Euler attitude. Preceded and terminated by a
        /// newline as SITL expects.
        /// </summary>
        public static string Encode(double timestampS, QuadBody body, NedFrame frame)
        {
            Vec3 pos = frame.ToNed(body.P - frame.Origin), vel = frame.ToNed(body.V);
            // Body rates FRD: p about the nose (roll right), q about the right axis (nose up), r about down (yaw right).
            double p = Sat(body.W.Y, GyroRangeRadS), q = Sat(body.W.X, GyroRangeRadS), r = Sat(-body.W.Z, GyroRangeRadS);
            Vec3 s = body.SpecificForce;
            double ax = Sat(Vec3.Dot(s, body.F), AccelRangeMs2), ay = Sat(Vec3.Dot(s, body.R), AccelRangeMs2), az = Sat(-Vec3.Dot(s, body.U), AccelRangeMs2);
            frame.Euler(body, out double roll, out double pitch, out double yaw);
            var b = new StringBuilder(256);
            b.Append("\n{\"timestamp\":").Append(N(timestampS))
             .Append(",\"imu\":{\"gyro\":[").Append(N(p)).Append(',').Append(N(q)).Append(',').Append(N(r))
             .Append("],\"accel_body\":[").Append(N(ax)).Append(',').Append(N(ay)).Append(',').Append(N(az))
             .Append("]},\"position\":[").Append(N(pos.X)).Append(',').Append(N(pos.Y)).Append(',').Append(N(pos.Z))
             .Append("],\"velocity\":[").Append(N(vel.X)).Append(',').Append(N(vel.Y)).Append(',').Append(N(vel.Z))
             .Append("],\"attitude\":[").Append(N(roll)).Append(',').Append(N(pitch)).Append(',').Append(N(yaw))
             .Append("]}\n");
            return b.ToString();
        }

        static string N(double v) => v.ToString("0.#########", CultureInfo.InvariantCulture);
        static double Sat(double v, double range) => Math.Max(-range, Math.Min(range, v));
    }

    /// <summary>
    /// GTA world ↔ NED for the physics link. <see cref="Origin"/> is the world point that is NED
    /// (0, 0, 0) (the body's resting centre at SITL home); <see cref="HeadingDeg"/> is the GTA heading
    /// of NED north (HorusGta.ini origin.heading_deg, counter-clockwise from GTA +Y), as in
    /// <see cref="PoseMapping"/>.
    /// </summary>
    public sealed class NedFrame
    {
        public Vec3 Origin;
        public double HeadingDeg;

        double Sin => Math.Sin(HeadingDeg * Math.PI / 180);
        double Cos => Math.Cos(HeadingDeg * Math.PI / 180);

        /// <summary>World vector (east-ish X, north-ish Y, up Z) as (north, east, down).</summary>
        public Vec3 ToNed(Vec3 w) => new Vec3(-w.X * Sin + w.Y * Cos, w.X * Cos + w.Y * Sin, -w.Z);

        /// <summary>Roll, pitch, yaw (rad, NED, yaw clockwise from north) of the body.</summary>
        public void Euler(QuadBody body, out double roll, out double pitch, out double yaw) =>
            Euler(body.R, body.F, body.U, out roll, out pitch, out yaw);

        /// <summary>
        /// Roll, pitch, yaw (rad, NED, ZYX) of a frame with world axes right, forward, up. Looking
        /// straight up or down only yaw minus roll is defined; roll is then 0.
        /// </summary>
        public void Euler(Vec3 right, Vec3 forward, Vec3 up, out double roll, out double pitch, out double yaw)
        {
            Vec3 f = ToNed(forward), r = ToNed(right), d = ToNed(-up);
            pitch = Math.Asin(Math.Max(-1, Math.Min(1, -f.Z)));
            if (f.X * f.X + f.Y * f.Y > 1e-12)
            {
                yaw = Math.Atan2(f.Y, f.X);
                roll = Math.Atan2(r.Z, d.Z);
            }
            else
            {
                yaw = Math.Atan2(-r.X, r.Y);
                roll = 0;
            }
        }

        /// <summary>GTA heading (rad, counter-clockwise from +Y) of NED yaw <paramref name="yawRad"/>.</summary>
        public double GtaHeadingRad(double yawRad) => HeadingDeg * Math.PI / 180 - yawRad;
    }
}
