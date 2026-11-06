using System;
using System.Runtime.InteropServices;

namespace HorusGta
{
    /// <summary>
    /// The HORUS drone's seven short-range distance sensors (as on the Gazebo drone), cast against
    /// the world on the physics clock for ArduPilot's proximity avoidance. Beams, in the body frame
    /// (forward, left, up), level in the body so they tilt with it: front (yaw 0), front_right (-45°),
    /// right (-90°), rear (180°), left (+90°), front_left (+45°), and down (-up).
    ///
    /// Each beam is cast from the body centre, which is never inside an obstacle (the collision step
    /// keeps it a radius off every surface), for the beam's hull offset plus <see cref="MaxRangeM"/>,
    /// and reads the hit distance minus the hull offset, clamped at 0, or +∞ when nothing is hit.
    /// Hull offsets come from the body's collision geometry, a sphere of <see cref="Airframe.RadiusM"/>
    /// (0.12 m by default):
    /// <list type="bullet">
    /// <item>horizontal beams: the sphere radius (0.12 m), the body's collision reach sideways, so a
    /// reading of 0 means the collision sphere touches the obstacle (resting against a wall it reads
    /// the 1 mm rest gap). The visible props reach about 0.35 m from the centre but do not collide.</item>
    /// <item>down beam: the resting centre height, radius + <see cref="QuadBody.RestGapM"/> (0.121 m), where
    /// the collision step puts the centre above the landing contact, so landed on flat ground the
    /// down sensor reads 0.</item>
    /// </list>
    /// </summary>
    public sealed class RangeSensors
    {
        public const int Beams = 7;
        /// <summary>Index of the down beam; 0..5 are horizontal.</summary>
        public const int Down = 6;
        public const double MaxRangeM = 4.0;
        public const double DefaultRateHz = 20, MinRateHz = 10, MaxRateHz = 20;
        public static readonly string[] Names = { "front", "front_right", "right", "rear", "left", "front_left", "down" };
        /// <summary>Yaw of each horizontal beam in the body frame, rad, positive left (counter-clockwise from above).</summary>
        static readonly double[] YawRad = { 0, -Math.PI / 4, -Math.PI / 2, Math.PI, Math.PI / 2, Math.PI / 4 };
        // Sweep times are sums of 1/frame rate steps: accept a sweep that is due up to this much early.
        const double DueToleranceS = 1e-9;

        public readonly double HorizontalHullM, DownHullM, RateHz, PeriodS;
        /// <summary>Ranges of the latest sweep (m; +∞ = clear), in <see cref="Names"/> order.</summary>
        public readonly float[] Ranges = new float[Beams];
        /// <summary>The latest sweep's datagram (<see cref="RangeProtocol.Size"/> bytes), valid once <see cref="Sweeps"/> > 0.</summary>
        public readonly byte[] Datagram = new byte[RangeProtocol.Size];
        readonly Func<Vec3, Vec3, double?> ray;
        double nextDueS = double.NaN, lastSweepS = double.NaN;

        /// <summary>
        /// Sensors on <paramref name="frame"/>, sweeping at <paramref name="rateHz"/> (10..20) of sim
        /// time. <paramref name="ray"/> casts from a to b and returns the distance from a to the first
        /// world hit, or null for none.
        /// </summary>
        public RangeSensors(Airframe frame, double rateHz, Func<Vec3, Vec3, double?> ray)
        {
            if (!(rateHz >= MinRateHz && rateHz <= MaxRateHz)) throw new ArgumentOutOfRangeException(nameof(rateHz), rateHz, "range sensor rate must be 10..20 Hz");
            this.ray = ray ?? throw new ArgumentNullException(nameof(ray));
            HorizontalHullM = frame.RadiusM;
            DownHullM = frame.RadiusM + QuadBody.RestGapM;
            RateHz = rateHz;
            PeriodS = 1 / rateHz;
        }

        /// <summary>Sensors per <paramref name="settings"/>, or null when they are disabled (nothing is then cast or sent).</summary>
        public static RangeSensors Create(SensorSettings settings, Airframe frame, Func<Vec3, Vec3, double?> ray) =>
            settings.Enabled ? new RangeSensors(frame, settings.RateHz, ray) : null;

        /// <summary>Sweeps so far; also the seq of the latest datagram.</summary>
        public long Sweeps { get; private set; }
        /// <summary>Rays cast so far.</summary>
        public long Rays { get; private set; }

        /// <summary>Smallest horizontal range of the latest sweep (m); +∞ when clear or before the first sweep.</summary>
        public double MinHorizontalM
        {
            get
            {
                double min = double.PositiveInfinity;
                if (Sweeps == 0) return min;
                for (int i = 0; i < Down; i++) min = Math.Min(min, Ranges[i]);
                return min;
            }
        }

        /// <summary>World direction (unit) of <paramref name="beam"/> for a body with axes right, forward, up.</summary>
        public static Vec3 Direction(int beam, Vec3 right, Vec3 forward, Vec3 up)
        {
            if (beam == Down) return -up;
            double a = YawRad[beam];
            // Body left is -right.
            return forward * Math.Cos(a) - right * Math.Sin(a);
        }

        public double HullM(int beam) => beam == Down ? DownHullM : HorizontalHullM;

        /// <summary>
        /// Sweep all beams from the body centre if one is due at sim time <paramref name="simTimeS"/>:
        /// the first call, then once per period, and again at once when the sim clock goes back (SITL
        /// restarted). True when it swept; <see cref="Ranges"/> and <see cref="Datagram"/> then hold it.
        /// </summary>
        public bool Update(double simTimeS, QuadBody body)
        {
            bool restart = double.IsNaN(nextDueS) || simTimeS < lastSweepS;
            if (!restart && simTimeS < nextDueS - DueToleranceS) return false;
            // On schedule: the next sweep one period after this one was due; after a restart or a
            // gap longer than a period, one period from now.
            nextDueS = restart || simTimeS - nextDueS >= PeriodS ? simTimeS + PeriodS : nextDueS + PeriodS;
            lastSweepS = simTimeS;
            for (int i = 0; i < Beams; i++) Ranges[i] = (float)Cast(body.P, Direction(i, body.R, body.F, body.U), HullM(i));
            Sweeps++;
            RangeProtocol.Encode(Datagram, (uint)Sweeps, RangeProtocol.TimeMs(simTimeS), Ranges);
            return true;
        }

        /// <summary>Range along unit <paramref name="dir"/> from <paramref name="from"/> past <paramref name="hullM"/>: hit − hull clamped at 0, or +∞.</summary>
        double Cast(Vec3 from, Vec3 dir, double hullM)
        {
            double reach = hullM + MaxRangeM;
            Rays++;
            double? d = ray(from, from + dir * reach);
            if (!d.HasValue || double.IsNaN(d.Value) || d.Value > reach) return double.PositiveInfinity;
            return Math.Max(0, d.Value - hullM);
        }
    }

    /// <summary>
    /// Range sensor datagram, little-endian, 44 bytes: magic "HRNG" | u32 version 1 | u32 seq |
    /// u32 time_ms (physics sim time, the JSON reply's clock) | 7 × f32 ranges (m; +∞ = nothing within
    /// 4 m) in the order front, front_right, right, rear, left, front_left, down.
    /// </summary>
    public static class RangeProtocol
    {
        public const int Size = 44;
        public const uint Version = 1;
        static readonly byte[] Magic = { (byte)'H', (byte)'R', (byte)'N', (byte)'G' };

        /// <summary>Sim time in whole milliseconds, rounded to the nearest (so sums of 1/frame rate steps land on the tick).</summary>
        public static uint TimeMs(double simTimeS) => (uint)Math.Max(0, Math.Min(uint.MaxValue, Math.Round(simTimeS * 1000, MidpointRounding.AwayFromZero)));

        /// <summary>Write one datagram into <paramref name="into"/> (at least <see cref="Size"/> bytes).</summary>
        public static void Encode(byte[] into, uint seq, uint timeMs, float[] ranges)
        {
            if (into.Length < Size) throw new ArgumentException("datagram buffer shorter than 44 bytes", nameof(into));
            if (ranges.Length != RangeSensors.Beams) throw new ArgumentException("need 7 ranges", nameof(ranges));
            Array.Copy(Magic, into, Magic.Length);
            WriteU32(into, 4, Version);
            WriteU32(into, 8, seq);
            WriteU32(into, 12, timeMs);
            for (int i = 0; i < RangeSensors.Beams; i++) WriteU32(into, 16 + 4 * i, F32Bits(ranges[i]));
        }

        [StructLayout(LayoutKind.Explicit)]
        struct F32Union { [FieldOffset(0)] public float Value; [FieldOffset(0)] public uint Bits; }

        /// <summary>IEEE 754 bits of <paramref name="v"/>, and back.</summary>
        public static uint F32Bits(float v) => new F32Union { Value = v }.Bits;
        public static float F32(uint bits) => new F32Union { Bits = bits }.Value;

        /// <summary>Little-endian u32 at byte offset <paramref name="o"/>.</summary>
        public static void WriteU32(byte[] b, int o, uint v)
        {
            for (int i = 0; i < 4; i++) b[o + i] = (byte)(v >> (8 * i));
        }

        public static uint ReadU32(byte[] b, int o) => (uint)(b[o] | b[o + 1] << 8 | b[o + 2] << 16 | b[o + 3] << 24);
    }
}
