using System;

namespace HorusGta
{
    /// <summary>Scripted-camera pose in GTA world coordinates (X east, Y north, Z up, metres).</summary>
    public struct GtaCameraPose
    {
        public double X, Y, Z;
        /// <summary>SET_CAM_ROT angles for rotation order 2 (degrees): x = pitch, y = roll, z = heading.</summary>
        public float RotX, RotY, RotZ;
        /// <summary>Vertical field of view for SET_CAM_FOV, clamped to the range GTA accepts.</summary>
        public float VfovDeg;
    }

    public static class PoseMapping
    {
        /// <summary>GTA rotation order 2 (YXZ), as used by SET_CAM_ROT / CREATE_CAM_WITH_PARAMS.</summary>
        public const int RotationOrder = 2;
        /// <summary>SET_CAM_FOV accepts 1..130 degrees.</summary>
        public const float MinCamFovDeg = 1f, MaxCamFovDeg = 130f;

        /// <summary>
        /// NED (relative to SITL home) to GTA world. <paramref name="originZ"/> is the GTA height of
        /// home (ini z, or the probed ground height). The origin heading is the GTA heading of NED
        /// north; GTA headings turn counter-clockwise from +Y.
        /// </summary>
        public static GtaCameraPose Map(PosePacket p, Origin origin, double originZ, double screenWidth, double screenHeight)
        {
            double h = origin.HeadingDeg * Math.PI / 180.0;
            double sin = Math.Sin(h), cos = Math.Cos(h);
            return new GtaCameraPose
            {
                X = origin.X - p.NorthM * sin + p.EastM * cos,
                Y = origin.Y + p.NorthM * cos + p.EastM * sin,
                Z = originZ - p.DownM,
                RotX = p.PitchDeg,
                RotY = p.RollDeg,
                RotZ = (float)WrapDeg(origin.HeadingDeg - p.YawDeg),
                VfovDeg = (float)Clamp(VerticalFovDeg(p.HfovDeg, screenWidth, screenHeight), MinCamFovDeg, MaxCamFovDeg),
            };
        }

        /// <summary>GTA camera FOV is vertical: vfov = 2 atan(tan(hfov / 2) * h / w). 16:9 if the size is unknown.</summary>
        public static double VerticalFovDeg(double hfovDeg, double screenWidth, double screenHeight)
        {
            double aspect = screenWidth > 0 && screenHeight > 0 ? screenHeight / screenWidth : 9.0 / 16.0;
            double half = hfovDeg * Math.PI / 360.0;
            return 2.0 * Math.Atan(Math.Tan(half) * aspect) * 180.0 / Math.PI;
        }

        /// <summary>Wrap to (-180, 180].</summary>
        public static double WrapDeg(double deg)
        {
            double w = deg % 360.0;
            if (w <= -180.0) w += 360.0;
            else if (w > 180.0) w -= 360.0;
            return w;
        }

        static double Clamp(double v, double lo, double hi) => v < lo ? lo : v > hi ? hi : v;
    }
}
