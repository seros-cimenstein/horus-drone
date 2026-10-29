using System;

namespace HorusGta
{
    /// <summary>
    /// Shared view ([view] mode=shared, README "Shared view"): the player keeps the gameplay camera
    /// while the drone camera renders one tick in <c>drone_every</c>. Every tick draws a corner
    /// marker whose colour tells horus_capture.asi which camera the frame used: drone frames go to
    /// HORUS and are kept off the screen (gta/capture/README.md "Shared view").
    /// </summary>
    public static class SharedView
    {
        public const int MinDroneEvery = 2, MaxDroneEvery = 4, DefaultDroneEvery = 2;

        /// <summary>
        /// Marker rectangle in normalized screen coordinates: x in [0, Width], y in [0, Height], 8x8
        /// px at 1280x720. DRAW_RECT takes its centre and size. horus_capture.asi reads texel (2, 2).
        /// </summary>
        public const float MarkerWidth = 0.00625f, MarkerHeight = 0.0111f;
        public const float MarkerCenterX = MarkerWidth / 2, MarkerCenterY = MarkerHeight / 2;
        /// <summary>Marker grey level (R = G = B): black on drone ticks, white on player ticks; opaque.</summary>
        public const int DroneMarkerLevel = 0, PlayerMarkerLevel = 255, MarkerAlpha = 255;

        /// <summary>Drone tick: the <paramref name="tick"/>-th (from 0) tick of an activation renders the drone camera.</summary>
        public static bool IsDroneTick(long tick, int droneEvery) => tick % droneEvery == 0;
    }

    /// <summary>Which ticks of an activation render the drone camera, with counts for the log.</summary>
    public sealed class ViewSchedule
    {
        readonly int droneEvery;
        long tick;

        public ViewSchedule(int droneEvery)
        {
            if (droneEvery < SharedView.MinDroneEvery || droneEvery > SharedView.MaxDroneEvery)
                throw new ArgumentOutOfRangeException(nameof(droneEvery));
            this.droneEvery = droneEvery;
        }

        public long DroneTicks { get; private set; }
        public long PlayerTicks { get; private set; }

        /// <summary>Start a new activation: its first tick is a drone tick; the counts restart.</summary>
        public void Reset()
        {
            tick = 0;
            DroneTicks = PlayerTicks = 0;
        }

        /// <summary>The next tick: true when it renders the drone camera.</summary>
        public bool Next()
        {
            bool drone = SharedView.IsDroneTick(tick++, droneEvery);
            if (drone) DroneTicks++;
            else PlayerTicks++;
            return drone;
        }
    }

    /// <summary>
    /// Centre of the streaming volume kept around the drone: it moves to the drone when the drone is
    /// more than <see cref="RecentreM"/> from it.
    /// </summary>
    public sealed class StreamAnchor
    {
        public const double RecentreM = 30;
        Point3? centre;

        public Point3? Centre => centre;

        /// <summary>True when the volume must be (re)created at <paramref name="drone"/>, which becomes the centre.</summary>
        public bool Update(Point3 drone)
        {
            if (centre.HasValue && Point3.Distance(centre.Value, drone) <= RecentreM) return false;
            centre = drone;
            return true;
        }

        public void Clear() => centre = null;
    }
}
