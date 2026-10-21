using System;

namespace HorusGta
{
    /// <summary>One camera pose from horus-gta-bridge (horus/gta_bridge.py).</summary>
    public struct PosePacket
    {
        public ulong Seq;
        /// <summary>Camera position, NED metres relative to SITL home.</summary>
        public double NorthM, EastM, DownM;
        /// <summary>Camera attitude in the NED world frame: yaw clockwise from north, pitch positive up.</summary>
        public float RollDeg, PitchDeg, YawDeg;
        public float HfovDeg;
    }

    public enum PacketError
    {
        None,
        Size,
        Magic,
        Version,
        NonFinite,
        FovRange,
    }

    /// <summary>
    /// UDP pose packet, little-endian, 56 bytes:
    /// magic "HPOS" | u32 version | u64 seq | f64 north, east, down | f32 roll, pitch, yaw | f32 hfov.
    /// </summary>
    public static class PoseProtocol
    {
        public const int Size = 56;
        public const uint Version = 1;
        static readonly byte[] Magic = { (byte)'H', (byte)'P', (byte)'O', (byte)'S' };

        public static PacketError TryParse(byte[] data, int length, out PosePacket packet)
        {
            packet = default;
            if (data == null || length != Size || data.Length < Size) return PacketError.Size;
            for (int i = 0; i < Magic.Length; i++)
                if (data[i] != Magic[i]) return PacketError.Magic;
            if (ReadU32(data, 4) != Version) return PacketError.Version;
            var p = new PosePacket
            {
                Seq = ReadU64(data, 8),
                NorthM = BitConverter.Int64BitsToDouble((long)ReadU64(data, 16)),
                EastM = BitConverter.Int64BitsToDouble((long)ReadU64(data, 24)),
                DownM = BitConverter.Int64BitsToDouble((long)ReadU64(data, 32)),
                RollDeg = ReadF32(data, 40),
                PitchDeg = ReadF32(data, 44),
                YawDeg = ReadF32(data, 48),
                HfovDeg = ReadF32(data, 52),
            };
            if (!Finite(p.NorthM) || !Finite(p.EastM) || !Finite(p.DownM) || !Finite(p.RollDeg)
                || !Finite(p.PitchDeg) || !Finite(p.YawDeg) || !Finite(p.HfovDeg))
                return PacketError.NonFinite;
            if (!(p.HfovDeg > 0f && p.HfovDeg < 180f)) return PacketError.FovRange;
            packet = p;
            return PacketError.None;
        }

        public static byte[] Encode(PosePacket p)
        {
            var b = new byte[Size];
            Array.Copy(Magic, b, Magic.Length);
            WriteU64(b, 4, Version, 4);
            WriteU64(b, 8, p.Seq, 8);
            WriteU64(b, 16, (ulong)BitConverter.DoubleToInt64Bits(p.NorthM), 8);
            WriteU64(b, 24, (ulong)BitConverter.DoubleToInt64Bits(p.EastM), 8);
            WriteU64(b, 32, (ulong)BitConverter.DoubleToInt64Bits(p.DownM), 8);
            WriteU64(b, 40, F32Bits(p.RollDeg), 4);
            WriteU64(b, 44, F32Bits(p.PitchDeg), 4);
            WriteU64(b, 48, F32Bits(p.YawDeg), 4);
            WriteU64(b, 52, F32Bits(p.HfovDeg), 4);
            return b;
        }

        static bool Finite(double v) => !double.IsNaN(v) && !double.IsInfinity(v);

        static uint ReadU32(byte[] b, int o) => (uint)(b[o] | b[o + 1] << 8 | b[o + 2] << 16 | b[o + 3] << 24);

        static ulong ReadU64(byte[] b, int o) => ReadU32(b, o) | (ulong)ReadU32(b, o + 4) << 32;

        static float ReadF32(byte[] b, int o)
        {
            var tmp = BitConverter.GetBytes(ReadU32(b, o));   // native order round trip of the LE value
            return BitConverter.ToSingle(tmp, 0);
        }

        static ulong F32Bits(float v) => BitConverter.ToUInt32(BitConverter.GetBytes(v), 0);

        static void WriteU64(byte[] b, int o, ulong v, int count)
        {
            for (int i = 0; i < count; i++) b[o + i] = (byte)(v >> (8 * i));
        }
    }

    /// <summary>
    /// Keeps the newest packet by sequence number. A sequence far below the newest one means the
    /// bridge restarted; it is accepted and becomes the new reference.
    /// </summary>
    public sealed class SeqTracker
    {
        public const ulong RestartGap = 1000;
        bool any;
        public ulong Newest { get; private set; }

        public bool Accept(ulong seq)
        {
            if (any && seq <= Newest && Newest - seq < RestartGap) return false;
            any = true;
            Newest = seq;
            return true;
        }

        public void Reset() { any = false; Newest = 0; }
    }
}
