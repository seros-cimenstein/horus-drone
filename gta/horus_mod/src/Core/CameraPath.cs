using System;

namespace HorusGta
{
    /// <summary>GTA world point (metres).</summary>
    public struct Point3
    {
        public double X, Y, Z;

        public Point3(double x, double y, double z) { X = x; Y = y; Z = z; }

        public static double Distance(Point3 a, Point3 b)
        {
            double dx = a.X - b.X, dy = a.Y - b.Y, dz = a.Z - b.Z;
            return Math.Sqrt(dx * dx + dy * dy + dz * dz);
        }
    }

    /// <summary>
    /// The drone camera's path through GTA's world, which SITL does not know. Each applied pose
    /// gives a segment from the previous camera position (<see cref="NextSegment"/>) that the
    /// script shape-tests against the world; hits go to <see cref="RecordHit"/>.
    /// <para>
    /// A hit is a new intersection unless it lies within <see cref="RepeatRadiusM"/> of the
    /// previous hit and less than <see cref="RepeatWindowS"/> after it: a camera hovering at a
    /// surface jitters back and forth through it every tick, which is one intersection, not 30 a
    /// second. Repeats are counted separately and extend the window.
    /// </para>
    /// </summary>
    public sealed class CameraPath
    {
        public const double RepeatRadiusM = 1.0;
        public const double RepeatWindowS = 1.0;

        Point3 previous;
        bool hasPrevious;
        Point3 lastHit;
        double lastHitS = double.NegativeInfinity;

        /// <summary>New intersections since the script started.</summary>
        public long Intersections { get; private set; }
        /// <summary>Hits folded into an earlier intersection.</summary>
        public long Repeats { get; private set; }

        /// <summary>
        /// Advance to <paramref name="current"/>. True with the previous position in
        /// <paramref name="from"/> when there is a segment to test; false for the first position
        /// after <see cref="Reset"/> (the camera was placed, not moved) and for a camera that did
        /// not move (a zero-length probe tests nothing).
        /// </summary>
        public bool NextSegment(Point3 current, out Point3 from)
        {
            from = previous;
            bool moved = hasPrevious && (current.X != previous.X || current.Y != previous.Y || current.Z != previous.Z);
            previous = current;
            hasPrevious = true;
            return moved;
        }

        /// <summary>Count a hit at <paramref name="at"/>; true when it is a new intersection (log it).</summary>
        public bool RecordHit(Point3 at, double nowS)
        {
            bool repeat = nowS - lastHitS < RepeatWindowS && Point3.Distance(at, lastHit) <= RepeatRadiusM;
            lastHit = at;
            lastHitS = nowS;
            if (repeat) Repeats++;
            else Intersections++;
            return !repeat;
        }

        /// <summary>
        /// Forget the position (activation or a stream restart places the camera anew; that jump
        /// is not a flight). The counts and the repeat window stay.
        /// </summary>
        public void Reset() => hasPrevious = false;
    }
}
