using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.MemoryMappedFiles;
using System.Net;
using System.Net.Sockets;
using System.Text;
using GTA;
using GTA.Math;
using GTA.Native;
using GTA.UI;

namespace HorusGta
{
    /// <summary>
    /// Flies the HORUS quad in GTA from ArduPilot SITL's JSON backend (README "Physics") and renders
    /// the drone camera on it. Active while SITL frames arrive; idle (normal game) otherwise. Every
    /// tick it publishes its state to the shared camera block read by horus_capture.asi.
    /// Configured by scripts\HorusGta.ini; logs to scripts\HorusGta.log.
    /// </summary>
    public class HorusGtaScript : Script
    {
        const string CameraName = "DEFAULT_SCRIPTED_CAMERA";
        // GET_GROUND_Z_FOR_3D_COORD returns the first surface below this height.
        const float GroundProbeTopZ = 1000f;
        const double ProgressLogIntervalS = 5.0;

        readonly ModConfig cfg;
        readonly StreamWriter log;
        readonly CameraBlock camera;
        readonly Stopwatch clock = Stopwatch.StartNew();
        string pendingNotice;

        double? originZ;
        bool groundNoticeShown;
        int groundAttempts;
        double nextGroundLogS;

        bool active;
        bool focusHeld;
        bool weatherSet, clockPaused;
        int[] prevClock;   // h, m, s before [scene] hour was applied
        ulong appliedSeq;
        int cam = -1;
        GtaCameraPose camSet;   // what this tick gave SET_CAM_COORD/ROT (GET_CAM_* read back the previous frame's)
        Ped frozenPed;
        bool prevFrozen, prevInvincible;
        int prevMaxWanted = 5;

        bool appliedThisTick;
        // The camera's path is shape-tested too (README "Mod", Intersections).
        readonly CameraPath path = new CameraPath();

        // ArduPilot's JSON SITL backend; while its frames arrive they drive the camera.
        readonly Socket physicsSocket;
        readonly byte[] physicsBuffer = new byte[256];
        EndPoint physicsPeer = new IPEndPoint(IPAddress.Any, 0);
        PhysicsLink physics;
        double physicsTargetS, physicsLastRealS, physicsLastFrameS = double.NegativeInfinity;
        bool physicsDriving;
        double nextPhysicsLogS, physicsLogRealS, physicsLogSimS;
        long physicsLogSteps;
        int physicsBudgetHits, physicsTimeouts;
        // What the quad collides with (the player's own ped is ignored; water does not stop it).
        const IntersectFlags Solid = IntersectFlags.Map | IntersectFlags.Vehicles | IntersectFlags.Peds | IntersectFlags.Objects
            | IntersectFlags.Glass | IntersectFlags.Foliage;
        readonly CameraPath contacts = new CameraPath();
        Ped rayIgnore;
        WorldProbe probe;
        long probeRaysLogged;
        // The airframe GTA draws at the physics body (Los Santos FPV's prop), an outside view of
        // it for checking, and the pose trace.
        const string DroneModelName = "ch_prop_casino_drone_02a";
        const System.Windows.Forms.Keys OutsideViewKey = System.Windows.Forms.Keys.F10;
        const float OutsideViewBackM = 2.5f, OutsideViewUpM = 0.8f, OutsideViewFovDeg = 50f;
        Prop drone;
        bool droneUnavailableLogged;
        long droneSelfHits;
        bool outsideView;
        int outsideCam = -1;
        StreamWriter trace;
        double nextTraceS;
        // Range sensors (README "Range sensors"): swept on the physics clock, one datagram each to [sensors] target.
        readonly Socket sensorSocket;
        readonly IPEndPoint sensorTarget;
        RangeSensors sensors;
        bool sensorSendFailed;
        long sensorRaysLogged;
        // The camera gimbal (README "Gimbal"): HORUS's setpoints arrive on [gimbal] bind:port, the
        // attitude the motors hold goes to [gimbal] target on the physics clock.
        readonly GimbalChannel gimbalChannel;
        bool gimbalSendFailed;
        long gimbalRejectedLogged;
        // Shared view ([view] mode=shared, README "Shared view"): the player keeps his gameplay camera
        // and control; the drone camera renders one tick in drone_every, and a corner marker drawn
        // every tick tells horus_capture.asi which camera each frame used.
        readonly bool shared;
        readonly ViewSchedule schedule;
        bool scriptCamsOn;   // shared view: RENDER_SCRIPT_CAMS(true) was called and not undone yet
        long viewDroneLogged, viewPlayerLogged;
        bool outsideViewRefusedLogged;
        // The drone's surroundings while the streaming focus stays on the player: collision is
        // requested every tick (REQUEST_COLLISION_AT_COORD), map models are kept by a sphere stream
        // volume. STREAMVOL_CREATE_SPHERE(x, y, z, radius, flags, lodFlags) returns a handle; the
        // natives db names the last two only "Any p4, Any p5". The values follow the community reading
        // of Rockstar's script enums, not checked in game: flags 1 = FLAG_MAPDATA (2 would add mover
        // collision, 4 weapon collision), lodFlags 127 = every LOD level (HD, orphan HD, LOD,
        // SLOD1-4). StreamVolumeRadiusM 0 turns the volume off (collision is still requested).
        const float StreamVolumeRadiusM = 150f;
        const int StreamVolumeFlags = 1, StreamVolumeLodFlags = 127;
        readonly StreamAnchor streamAnchor = new StreamAnchor();
        int streamVolume;
        bool streamVolumeHeld, streamVolumeLogged, streamVolumeFailedLogged;

        // The height survey (README "Survey"): runs while scripts\HorusGtaSurvey.request.json exists
        // and SITL is not connected; progress in scripts\HorusGtaSurvey.progress.json.
        const string SurveyRequestFile = "HorusGtaSurvey.request.json", SurveyProgressFile = "HorusGtaSurvey.progress.json";
        const double SurveyPollS = 1.0, SurveyLogIntervalS = 30.0;
        // Top surfaces only: the map, objects, foliage and glass; no vehicles, peds or water.
        const IntersectFlags SurveyFlags = IntersectFlags.Map | IntersectFlags.Objects | IntersectFlags.Foliage | IntersectFlags.Glass;
        // Extra collision requests at the tile's quadrant centres (a quarter tile from the centre).
        const float SurveyQuadrantM = (float)(SurveyGrid.TileM / 4);
        SurveyRequest surveyRequest;   // read, waiting for the home ground height
        SurveyRun survey;
        double nextSurveyPollS, nextSurveyLogS;
        bool surveyHolding;   // the survey holds the player and the streaming focus

        public HorusGtaScript()
        {
            Interval = 0;
            string dir = BaseDirectory ?? "scripts";
            try { log = new StreamWriter(Path.Combine(dir, "HorusGta.log"), false) { AutoFlush = true }; } catch { }
            Log("HorusGta " + typeof(HorusGtaScript).Assembly.GetName().Version);

            string iniPath = Path.Combine(dir, "HorusGta.ini");
            if (File.Exists(iniPath))
            {
                cfg = ModConfig.Parse(File.ReadAllText(iniPath));
                foreach (string warning in cfg.Warnings) Log(warning);
            }
            else
            {
                cfg = new ModConfig();
                Log($"{iniPath} not found; using defaults");
            }
            originZ = cfg.Origin.Z;
            shared = cfg.View.Mode == ViewMode.Shared;
            schedule = shared ? new ViewSchedule(cfg.View.DroneEvery) : null;
            Log(string.Format(CultureInfo.InvariantCulture,
                "origin x={0:F2} y={1:F2} heading_deg={2:F1} z={3}; release after {4:g} s without SITL frames; scene weather={5} hour={6} freeze_time={7}",
                cfg.Origin.X, cfg.Origin.Y, cfg.Origin.HeadingDeg,
                originZ.HasValue ? originZ.Value.ToString("F2", CultureInfo.InvariantCulture) : "probe ground", cfg.TimeoutS,
                cfg.Scene.Weather ?? "(game)", cfg.Scene.Hour?.ToString(CultureInfo.InvariantCulture) ?? "(game)",
                cfg.Scene.FreezeTime ? "true" : "false"));

            try
            {
                physicsSocket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
                physicsSocket.Bind(new IPEndPoint(IPAddress.Parse(cfg.Physics.Bind), cfg.Physics.Port));
                Log(string.Format(CultureInfo.InvariantCulture, "physics: listening for ArduPilot JSON SITL on udp {0}:{1} (budget {2:g} ms per tick, hfov {3:g} deg)",
                    cfg.Physics.Bind, cfg.Physics.Port, cfg.Physics.BudgetMs, cfg.Physics.HfovDeg));
            }
            catch (Exception e)
            {
                physicsSocket?.Close();
                physicsSocket = null;
                Log($"physics: cannot listen on udp {cfg.Physics.Bind}:{cfg.Physics.Port}: {e.Message}");
                pendingNotice = $"HORUS GTA: cannot listen on udp {cfg.Physics.Bind}:{cfg.Physics.Port} (see HorusGta.log)";
            }

            if (cfg.Sensors.Enabled)
            {
                try
                {
                    sensorTarget = new IPEndPoint(IPAddress.Parse(cfg.Sensors.Host), cfg.Sensors.Port);
                    sensorSocket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp) { Blocking = false };
                    Log(string.Format(CultureInfo.InvariantCulture, "range sensors: {0} beams -> udp {1} at {2:g} Hz",
                        RangeSensors.Beams, cfg.Sensors.Target, cfg.Sensors.RateHz));
                }
                catch (Exception e)
                {
                    sensorSocket?.Close();
                    sensorSocket = null;
                    Log($"range sensors: off (cannot open a udp socket to {cfg.Sensors.Target}: {e.Message})");
                }
            }
            else Log("range sensors: off");

            try
            {
                gimbalChannel = new GimbalChannel(cfg.Gimbal);
                Log(string.Format(CultureInfo.InvariantCulture, "gimbal: setpoints on udp {0}:{1}, status -> udp {2} at {3:g} Hz, slew {4:g} deg/s; resting at pitch {5:g} until HORUS points it",
                    cfg.Gimbal.Bind, cfg.Gimbal.Port, cfg.Gimbal.Target, cfg.Gimbal.RateHz, cfg.Gimbal.SlewDegS, GimbalMount.RestPitchDeg));
            }
            catch (Exception e)
            {
                gimbalChannel = null;
                Log($"gimbal: cannot listen on udp {cfg.Gimbal.Bind}:{cfg.Gimbal.Port} ({e.Message}); the camera stays at rest and HORUS sees no gimbal");
            }

            try
            {
                camera = new CameraBlock(MemoryMappedFile.CreateOrOpen(CameraBlock.Name, CameraBlock.Size));
                Log($"camera block {CameraBlock.Name} open");
            }
            catch (Exception e)
            {
                Log($"camera block {CameraBlock.Name} not available ({e.Message}); captured frames will not report the drone camera");
            }

            Tick += OnTick;
            KeyDown += OnKeyDown;
            Aborted += OnAborted;
        }

        void Log(string message)
        {
            try { log?.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] {message}"); } catch { }
        }

        void OnTick(object sender, EventArgs e)
        {
            appliedThisTick = false;
            rayIgnore = Game.Player.Character;
            double now = clock.Elapsed.TotalSeconds;
            // Before the physics: on the tick SITL's first frame arrives the survey gives the player and the focus back.
            SurveyTick(now);
            Step(now);
            PublishCamera(shared ? CameraBlock.ViewShared : CameraBlock.ViewDrone);
            if (shared) DrawMarker();
        }

        /// <summary>
        /// Shared view: the corner marker (8x8 px at 1280x720), issued last in the tick so nothing
        /// draws over it. Black when this tick's frame renders the drone camera, white otherwise;
        /// horus_capture.asi reads it back (gta/capture/README.md).
        /// </summary>
        void DrawMarker()
        {
            int level = appliedThisTick ? SharedView.DroneMarkerLevel : SharedView.PlayerMarkerLevel;
            Function.Call(Hash.DRAW_RECT, SharedView.MarkerCenterX, SharedView.MarkerCenterY, SharedView.MarkerWidth, SharedView.MarkerHeight,
                level, level, level, SharedView.MarkerAlpha, false);
        }

        /// <summary>F10 while the physics link drives the camera: switch between the HORUS camera and an outside view of the drone (not in the shared view).</summary>
        void OnKeyDown(object sender, System.Windows.Forms.KeyEventArgs e)
        {
            if (e.KeyCode != OutsideViewKey || !physicsDriving) return;
            if (shared)
            {
                if (!outsideViewRefusedLogged) Log("physics: outside view (F10) is not available in the shared view");
                outsideViewRefusedLogged = true;
                return;
            }
            outsideView = !outsideView;
            Log($"physics: outside view {(outsideView ? "on (captured frames are marked as not the drone camera)" : "off")}");
        }

        void Step(double now)
        {
            if (pendingNotice != null)
            {
                Notification.PostTicker(pendingNotice, true);
                pendingNotice = null;
            }
            PhysicsStep(now);
        }

        /// <summary>
        /// Answer SITL's servo frames for up to the tick budget, pacing the sim to real time, and put
        /// the camera on the quad while frames arrive.
        /// </summary>
        void PhysicsStep(double now)
        {
            if (physicsSocket == null) return;
            if (physics == null)
            {
                if (physicsSocket.Available <= 0) return;
                if (!originZ.HasValue && !ResolveGround(now)) return;
                var frame = new Airframe();
                var body = new QuadBody(frame)
                {
                    FloorZ = originZ.Value,
                    Sweep = (probe = new WorldProbe(WorldRay)).Sweep,
                    Contacted = OnContact,
                };
                var ned = new NedFrame
                {
                    Origin = new Vec3(cfg.Origin.X, cfg.Origin.Y, originZ.Value + frame.RestHeightM),
                    HeadingDeg = cfg.Origin.HeadingDeg,
                };
                physics = new PhysicsLink(body, ned, GimbalMount.Create(cfg.Gimbal), ned.Origin, ned.GtaHeadingRad(0));
                // After each step, inside the tick budget: the sensor sweep (7 rays at 20 Hz of sim
                // time) and the gimbal report ([gimbal] rate_hz of sim time).
                sensors = sensorSocket != null ? RangeSensors.Create(cfg.Sensors, frame, SensorRay) : null;
                physics.Advanced = AfterStep;
                physicsLastRealS = now;
                Log(string.Format(CultureInfo.InvariantCulture,
                    "physics: SITL frames arriving; quad {0} kg, {1}x{2}x{3} props, {4} Kv {5}S, max {6:F0} rpm, hover throttle {7:F3}, resting at ({8:F2}, {9:F2}, {10:F2})",
                    frame.MassKg, frame.PropDiameterIn, frame.PropPitchIn, frame.PropBlades, frame.MotorKv, frame.BatteryCells,
                    frame.MaxRpm, frame.HoverThrottle(), ned.Origin.X, ned.Origin.Y, ned.Origin.Z));
            }

            DrainGimbal();
            // Pace: the sim may run up to the wall time since the last tick, at most 0.1 s ahead of
            // where it is, so a slow tick costs real-time factor instead of a burst afterwards.
            physicsTargetS = Math.Min(physicsTargetS + (now - physicsLastRealS), physics.SimTimeS + 0.1);
            physicsLastRealS = now;
            long started = Stopwatch.GetTimestamp();
            long budget = (long)(Stopwatch.Frequency * cfg.Physics.BudgetMs / 1000.0);
            var r = physics.Pump(physicsTargetS, ReceiveServo, ReplyServo, () => Stopwatch.GetTimestamp() - started >= budget);
            if (r.Steps > 0 || r.Resent > 0) physicsLastFrameS = now;
            if (r.BudgetHit) physicsBudgetHits++;
            if (r.TimedOut) physicsTimeouts++;
            if (r.Restarts > 0)
            {
                Log("physics: SITL restarted (frame count went back); quad back at its start");
                physicsTargetS = physics.SimTimeS;
            }

            if (now - physicsLastFrameS > cfg.TimeoutS)
            {
                if (physicsDriving)
                {
                    physicsDriving = false;
                    RemoveDrone();
                    Release($"physics: no SITL frame for {cfg.TimeoutS:g} s");
                }
                return;
            }
            if (!physicsDriving)
            {
                physicsDriving = true;
                physicsTargetS = physics.SimTimeS;
                physicsLogRealS = now; physicsLogSimS = physics.SimTimeS; physicsLogSteps = physics.TotalSteps;
                nextPhysicsLogS = now + ProgressLogIntervalS;
                Log($"physics: driving the camera, SITL frame rate {physics.FrameRate} Hz");
            }
            LogPhysics(now);

            var camPose = physics.CameraPose(originZ.Value, cfg.Physics.HfovDeg);
            if (!active) Activate(camPose);
            bool droneTick = Apply(camPose, now);
            ShowDrone();
            if (outsideView) ShowOutsideView();
            else StopOutsideView();
            Trace(now, droneTick);
            // Frames rendered from the outside view (or, in the shared view, from the player's camera) are not the drone camera's.
            appliedThisTick = droneTick && !outsideView;
        }

        static Vector3 V3(Vec3 v) => new Vector3((float)v.X, (float)v.Y, (float)v.Z);

        /// <summary>Spawn the drone prop once its model is loaded (requested without blocking the tick), then pose it at the body.</summary>
        void ShowDrone()
        {
            var body = physics.Body;
            if (drone == null || !drone.Exists())
            {
                drone = null;
                int model = Function.Call<int>(Hash.GET_HASH_KEY, DroneModelName);
                if (!Function.Call<bool>(Hash.IS_MODEL_IN_CDIMAGE, model))
                {
                    if (!droneUnavailableLogged) Log($"physics: drone model {DroneModelName} is not in the game files; flying without a visible airframe");
                    droneUnavailableLogged = true;
                    return;
                }
                Function.Call(Hash.REQUEST_MODEL, model);
                if (!Function.Call<bool>(Hash.HAS_MODEL_LOADED, model)) return;
                drone = World.CreateProp(new Model(model), V3(body.P), false, false);
                Function.Call(Hash.SET_MODEL_AS_NO_LONGER_NEEDED, model);
                if (drone == null || !drone.Exists())
                {
                    drone = null;
                    if (!droneUnavailableLogged) Log($"physics: could not spawn {DroneModelName}; flying without a visible airframe");
                    droneUnavailableLogged = true;
                    return;
                }
                // Drawn only: the physics body is the collision shape, and world rays must not see the prop.
                drone.IsCollisionEnabled = false;
                drone.IsPositionFrozen = true;
                Function.Call(Hash.SET_ENTITY_DYNAMIC, drone.Handle, false);
                var min = new OutputArgument();
                var max = new OutputArgument();
                Function.Call(Hash.GET_MODEL_DIMENSIONS, model, min, max);
                Vector3 lo = min.GetResult<Vector3>(), hi = max.GetResult<Vector3>();
                Log(string.Format(CultureInfo.InvariantCulture,
                    "physics: drone prop {0} spawned (entity {1}), model box right {2:F2}..{3:F2}, forward {4:F2}..{5:F2}, up {6:F2}..{7:F2} m; collision sphere radius {8:F2} m; camera mount (right, forward, up) ({9:F2}, {10:F2}, {11:F2}) m; outside view key {12}",
                    DroneModelName, drone.Handle, lo.X, hi.X, lo.Y, hi.Y, lo.Z, hi.Z, body.Frame.RadiusM,
                    body.Frame.CameraMountM.X, body.Frame.CameraMountM.Y, body.Frame.CameraMountM.Z, OutsideViewKey));
            }
            drone.PositionNoOffset = V3(body.P);
            body.Orientation(out double x, out double y, out double z, out double w);
            Function.Call(Hash.SET_ENTITY_QUATERNION, drone.Handle, (float)x, (float)y, (float)z, (float)w);
            // The 0.9 m prop's legs reach into the drone camera's 84 deg view from the mount (both
            // lower corners; HORUS listed a leg as a candidate), so it is drawn only for the
            // outside view.
            if (drone.IsVisible != outsideView) drone.IsVisible = outsideView;
        }

        void RemoveDrone()
        {
            StopOutsideView();
            outsideView = false;
            if (drone != null && drone.Exists()) drone.Delete();
            drone = null;
        }

        /// <summary>Render a camera behind and above the drone, pointed at it (F10).</summary>
        void ShowOutsideView()
        {
            var body = physics.Body;
            var back = new Vec3(body.F.X, body.F.Y, 0).Normalized * -OutsideViewBackM;
            var eye = V3(body.P + back + new Vec3(0, 0, OutsideViewUpM));
            if (outsideCam == -1 || !Function.Call<bool>(Hash.DOES_CAM_EXIST, outsideCam))
            {
                outsideCam = Function.Call<int>(Hash.CREATE_CAM, CameraName, false);
                Function.Call(Hash.SET_CAM_FOV, outsideCam, OutsideViewFovDeg);
            }
            Function.Call(Hash.SET_CAM_COORD, outsideCam, eye.X, eye.Y, eye.Z);
            var at = V3(body.P);
            Function.Call(Hash.POINT_CAM_AT_COORD, outsideCam, at.X, at.Y, at.Z);
            if (!Function.Call<bool>(Hash.IS_CAM_RENDERING, outsideCam))
            {
                Function.Call(Hash.SET_CAM_ACTIVE, outsideCam, true);
                Function.Call(Hash.RENDER_SCRIPT_CAMS, true, false, 0, true, false, 0);
            }
        }

        void StopOutsideView()
        {
            if (outsideCam == -1) return;
            if (Function.Call<bool>(Hash.DOES_CAM_EXIST, outsideCam))
            {
                Function.Call(Hash.SET_CAM_ACTIVE, outsideCam, false);
                Function.Call(Hash.DESTROY_CAM, outsideCam, false);
            }
            outsideCam = -1;
            if (cam != -1 && Function.Call<bool>(Hash.DOES_CAM_EXIST, cam))
            {
                Function.Call(Hash.SET_CAM_ACTIVE, cam, true);
                Function.Call(Hash.RENDER_SCRIPT_CAMS, true, false, 0, true, false, 0);
            }
        }

        /// <summary>
        /// [physics] trace_hz rows (at most one per tick) of HorusGtaTrace.csv: the HORUS camera as
        /// read back (the previous tick's) and as set this tick, the drone prop as read back, and
        /// the physics state ArduPilot was sent (NED from the resting body centre).
        /// </summary>
        void Trace(double now, bool droneTick)
        {
            if (cfg.Physics.TraceHz <= 0 || now < nextTraceS || cam == -1) return;
            nextTraceS = Math.Max(nextTraceS + 1 / cfg.Physics.TraceHz, now);
            if (trace == null)
            {
                try
                {
                    string file = Path.Combine(BaseDirectory ?? "scripts", "HorusGtaTrace.csv");
                    trace = new StreamWriter(file, false) { AutoFlush = true };
                    trace.WriteLine("unix_s,sim_s,frame,game_frame,view,cam_x,cam_y,cam_z,cam_rot_x,cam_rot_y,cam_rot_z,drone_x,drone_y,drone_z,drone_qx,drone_qy,drone_qz,drone_qw,body_n,body_e,body_d,body_roll,body_pitch,body_yaw,gimbal_roll,gimbal_pitch,gimbal_yaw,origin_x,origin_y,origin_z,origin_heading,cam_set_x,cam_set_y,cam_set_z,cam_set_rot_x,cam_set_rot_y,cam_set_rot_z");
                    Log($"physics: tracing the camera and drone pose to {file} at {cfg.Physics.TraceHz:g} Hz");
                }
                catch (Exception e)
                {
                    Log($"physics: cannot write the trace: {e.Message}");
                    cfg.Physics.TraceHz = 0;
                    return;
                }
            }
            var c = Function.Call<Vector3>(Hash.GET_CAM_COORD, cam);
            var cr = Function.Call<Vector3>(Hash.GET_CAM_ROT, cam, PoseMapping.RotationOrder);
            Vector3 d = default;
            float qx = float.NaN, qy = float.NaN, qz = float.NaN, qw = float.NaN;
            if (drone != null && drone.Exists())
            {
                d = Function.Call<Vector3>(Hash.GET_ENTITY_COORDS, drone.Handle, false);
                OutputArgument ox = new OutputArgument(), oy = new OutputArgument(), oz = new OutputArgument(), ow = new OutputArgument();
                Function.Call(Hash.GET_ENTITY_QUATERNION, drone.Handle, ox, oy, oz, ow);
                qx = ox.GetResult<float>(); qy = oy.GetResult<float>(); qz = oz.GetResult<float>(); qw = ow.GetResult<float>();
            }
            var f = physics.Frame;
            var body = physics.Body;
            Vec3 p = f.ToNed(body.P - f.Origin);
            f.Euler(body, out double roll, out double pitch, out double yaw);
            const double Deg = 180 / Math.PI;
            trace.WriteLine(string.Format(CultureInfo.InvariantCulture,
                "{0:F3},{1:F4},{2},{3},{4},{5:F3},{6:F3},{7:F3},{8:F2},{9:F2},{10:F2},{11:F3},{12:F3},{13:F3},{14:F5},{15:F5},{16:F5},{17:F5},{18:F3},{19:F3},{20:F3},{21:F2},{22:F2},{23:F2},{24:F2},{25:F2},{26:F2},{27:F3},{28:F3},{29:F3},{30:F1},{31:F3},{32:F3},{33:F3},{34:F2},{35:F2},{36:F2}",
                (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds, physics.SimTimeS, physics.LastFrame, Game.FrameCount,
                outsideView ? "outside" : droneTick ? "drone" : "player",
                c.X, c.Y, c.Z, cr.X, cr.Y, cr.Z, d.X, d.Y, d.Z, qx, qy, qz, qw,
                p.X, p.Y, p.Z, roll * Deg, pitch * Deg, yaw * Deg,
                physics.Gimbal.RollDeg, physics.Gimbal.TiltDeg, physics.Gimbal.PanDeg,
                f.Origin.X, f.Origin.Y, f.Origin.Z, f.HeadingDeg,
                camSet.X, camSet.Y, camSet.Z, camSet.RotX, camSet.RotY, camSet.RotZ));
        }

        ServoFrame? ReceiveServo()
        {
            try
            {
                for (;;)
                {
                    // SITL answers within a fraction of a millisecond in lockstep; 2 ms means it is idle.
                    if (!physicsSocket.Poll(2000, SelectMode.SelectRead)) return null;
                    int n = physicsSocket.ReceiveFrom(physicsBuffer, ref physicsPeer);
                    if (SitlJson.TryDecode(physicsBuffer, n, out var frame)) return frame;
                }
            }
            catch (SocketException e)
            {
                Log($"physics: udp receive failed: {e.SocketErrorCode}");
                return null;
            }
        }

        void ReplyServo(string state)
        {
            try { physicsSocket.SendTo(Encoding.ASCII.GetBytes(state), physicsPeer); }
            catch (SocketException e) { Log($"physics: udp send failed: {e.SocketErrorCode}"); }
        }

        void LogPhysics(double now)
        {
            if (now < nextPhysicsLogS) return;
            var body = physics.Body;
            double real = now - physicsLogRealS, sim = physics.SimTimeS - physicsLogSimS;
            physics.Frame.Euler(body, out double roll, out double pitch, out double yaw);
            Vec3 p = physics.Frame.ToNed(body.P - physics.Frame.Origin);
            Log(string.Format(CultureInfo.InvariantCulture,
                "physics: sim {0:F1} s, real-time factor {1:F3}, {2:F0} steps/s at {3} Hz, {4} budget hits, {5} idle ticks, {18:F0} world rays/s{22}; ned ({6:F2}, {7:F2}, {8:F2}) rpy ({9:F1}, {10:F1}, {11:F1}) gimbal motors rpy ({20:F1}, {12:F1}, {21:F1}){24}; rpm {13:F0} {14:F0} {15:F0} {16:F0}{17}{19}{23}",
                physics.SimTimeS, sim / real, (physics.TotalSteps - physicsLogSteps) / real, physics.FrameRate, physicsBudgetHits, physicsTimeouts,
                p.X, p.Y, p.Z, roll * 180 / Math.PI, pitch * 180 / Math.PI, yaw * 180 / Math.PI, physics.Gimbal.TiltDeg,
                body.Rpm[0], body.Rpm[1], body.Rpm[2], body.Rpm[3],
                body.InContact ? (body.LastContact.Value.Normal.Z > 0.7 ? ", on ground" : ", against a wall") : "", (probe.Rays - probeRaysLogged) / real,
                DroneRayCheck(body), physics.Gimbal.RollDeg, physics.Gimbal.PanDeg, SensorStats(real), ViewStats(), GimbalStats()));
            physicsLogRealS = now; physicsLogSimS = physics.SimTimeS; physicsLogSteps = physics.TotalSteps;
            physicsBudgetHits = 0; physicsTimeouts = 0; probeRaysLogged = probe.Rays;
            if (sensors != null) sensorRaysLogged = sensors.Rays;
            nextPhysicsLogS = now + ProgressLogIntervalS;
        }

        /// <summary>For the 5 s log: sensor rays/s over <paramref name="realS"/> and the latest smallest horizontal range.</summary>
        string SensorStats(double realS)
        {
            if (sensors == null) return "";
            double min = sensors.MinHorizontalM;
            return string.Format(CultureInfo.InvariantCulture, ", {0:F0} sensor rays/s, min horizontal range {1}",
                (sensors.Rays - sensorRaysLogged) / realS, double.IsInfinity(min) ? "clear" : min.ToString("F2", CultureInfo.InvariantCulture) + " m");
        }

        /// <summary>For the 5 s log in the shared view: drone and player ticks since the last line.</summary>
        string ViewStats()
        {
            if (!shared) return "";
            string s = $"; view: {schedule.DroneTicks - viewDroneLogged} drone / {schedule.PlayerTicks - viewPlayerLogged} player ticks";
            viewDroneLogged = schedule.DroneTicks;
            viewPlayerLogged = schedule.PlayerTicks;
            return s;
        }

        /// <summary>For the 5 s log: the gimbal's target and the camera attitude its motors hold.</summary>
        string GimbalStats()
        {
            var g = physics.Gimbal;
            g.Measure(physics.Body, physics.Frame, out double cr, out double cp, out double cy, out _, out _);
            return string.Format(CultureInfo.InvariantCulture, ", camera pitch {0:F1} yaw {1:F1} roll {2:F1} (target pitch {3:F1} yaw {4:F1}{5})",
                cp, cy, cr, g.TargetPitchDeg, g.TargetYawDeg, g.HasSetpoint ? "" : ", at rest: no setpoint yet");
        }

        /// <summary>After each physics step: sweep the range sensors and report the gimbal when due (a failed send is logged once each).</summary>
        void AfterStep()
        {
            if (sensors != null && sensors.Update(physics.SimTimeS, physics.Body))
            {
                try { sensorSocket.SendTo(sensors.Datagram, 0, RangeProtocol.Size, SocketFlags.None, sensorTarget); }
                catch (Exception e)
                {
                    if (!sensorSendFailed)
                        Log($"range sensors: udp send to {cfg.Sensors.Target} failed ({(e is SocketException se ? se.SocketErrorCode.ToString() : e.Message)}); further failures are not logged");
                    sensorSendFailed = true;
                }
            }
            if (gimbalChannel != null && physics.Gimbal.Report(physics.SimTimeS, physics.Body, physics.Frame))
            {
                try { gimbalChannel.Send(physics.Gimbal); }
                catch (Exception e)
                {
                    if (!gimbalSendFailed)
                        Log($"gimbal: udp send to {cfg.Gimbal.Target} failed ({(e is SocketException se ? se.SocketErrorCode.ToString() : e.Message)}); further failures are not logged");
                    gimbalSendFailed = true;
                }
            }
        }

        /// <summary>Once per tick: hand HORUS's waiting gimbal setpoints to the mount; log each new target and rejected datagrams.</summary>
        void DrainGimbal()
        {
            if (gimbalChannel == null) return;
            var g = physics.Gimbal;
            double pitch = g.TargetPitchDeg, yaw = g.TargetYawDeg;
            bool had = g.HasSetpoint;
            if (gimbalChannel.Drain(g) > 0 && (!had || g.TargetPitchDeg != pitch || g.TargetYawDeg != yaw))
                Log(string.Format(CultureInfo.InvariantCulture, "gimbal: setpoint pitch {0:F1} yaw {1:F1}", g.TargetPitchDeg, g.TargetYawDeg));
            if (gimbalChannel.Rejected > gimbalRejectedLogged)
            {
                Log($"gimbal: {gimbalChannel.Rejected - gimbalRejectedLogged} datagram(s) rejected (not a version 1 HGIM setpoint of {GimbalProtocol.SetpointSize} bytes with finite angles)");
                gimbalRejectedLogged = gimbalChannel.Rejected;
            }
        }

        /// <summary>Sensor beam: distance from a to the first <see cref="Solid"/> hit towards b (player ped and drone prop ignored), or null.</summary>
        double? SensorRay(Vec3 a, Vec3 b)
        {
            var from = V3(a);
            var hit = Cast(from, V3(b), Solid);
            if (!hit.DidHit) return null;
            return (hit.HitPosition - from).Length();
        }

        /// <summary>
        /// Tell the capture plugin, once per tick, whether this tick put the drone camera on screen
        /// at a received pose (the frame it renders then shows that pose's seq), and the view mode.
        /// </summary>
        void PublishCamera(uint viewMode)
        {
            try { camera?.Publish(appliedThisTick, appliedSeq, (ulong)Game.FrameCount, viewMode); }
            catch (Exception e) { Log($"camera block write failed: {e.Message}"); }
        }

        /// <summary>
        /// Once per tick, before the physics: start the survey when its request file appears (polled
        /// every second), stop it when the file goes, suspend it while SITL is connected (frames
        /// waiting or the physics link driving), and otherwise run one survey step within
        /// [physics] budget_ms, holding the player like the drone view does.
        /// </summary>
        void SurveyTick(double now)
        {
            string dir = BaseDirectory ?? "scripts";
            string requestPath = Path.Combine(dir, SurveyRequestFile);
            if (now >= nextSurveyPollS)
            {
                nextSurveyPollS = now + SurveyPollS;
                bool requested = File.Exists(requestPath);
                if (requested && surveyRequest == null && survey == null) ReadSurveyRequest(requestPath, dir);
                else if (!requested && (surveyRequest != null || survey != null)) EndSurvey(now, "the request file was removed");
            }
            if (surveyRequest == null && survey == null) return;
            if (physicsDriving || (physicsSocket != null && physicsSocket.Available > 0))
            {
                ReleaseSurveyHold();
                survey?.Suspend(now, "SITL connected");
                return;
            }
            if (survey == null)
            {
                if (!originZ.HasValue && !ResolveGround(now)) return;
                StartSurvey(now, dir, requestPath);
                if (survey == null) return;
            }
            survey.Resume(now);
            surveyHolding = true;
            HoldPlayer();
            bool going = survey.Step(now, cfg.Physics.BudgetMs / 1000.0);
            if (now >= nextSurveyLogS)
            {
                Log("survey: " + survey.Summary());
                nextSurveyLogS = now + SurveyLogIntervalS;
            }
            if (!going) EndSurvey(now, null);
        }

        void ReadSurveyRequest(string requestPath, string dir)
        {
            string text, error;
            try { text = File.ReadAllText(requestPath); }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException) { Log($"survey: cannot read {SurveyRequestFile} yet ({e.Message})"); return; }
            if (SurveyRequest.TryParse(text, out surveyRequest, out error))
            {
                Log($"survey: request {text.Trim()}");
                return;
            }
            Log($"survey: {SurveyRequestFile} refused: {error}");
            try
            {
                SurveyRun.WriteRejected(Path.Combine(dir, SurveyProgressFile), $"{SurveyRequestFile} refused: {error}");
                File.Delete(requestPath);
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException) { Log($"survey: cannot report the refusal: {e.Message}"); }
        }

        void StartSurvey(double now, string dir, string requestPath)
        {
            var run = new SurveyRun(surveyRequest, cfg.Origin, originZ.Value, cfg.Origin.Z.HasValue ? "ini" : "probe", new Airframe().RestHeightM,
                dir, Path.Combine(dir, SurveyProgressFile))
            {
                CastDown = SurveyRay,
                IsWater = SurveyWater,
                KeepLoaded = KeepTileLoaded,
                Clock = () => clock.Elapsed.TotalSeconds,
                Log = Log,
            };
            surveyRequest = null;
            if (!run.Begin(now))
            {
                try { File.Delete(requestPath); } catch (Exception e) when (e is IOException || e is UnauthorizedAccessException) { }
                return;
            }
            survey = run;
            nextSurveyLogS = now + SurveyLogIntervalS;
        }

        /// <summary>End the survey: stopped early when <paramref name="why"/> is given; a finished or failed run removes its request file so it does not start again.</summary>
        void EndSurvey(double now, string why)
        {
            if (survey != null)
            {
                if (why != null) survey.Stop(now, why);
                else
                {
                    try { File.Delete(Path.Combine(BaseDirectory ?? "scripts", SurveyRequestFile)); }
                    catch (Exception e) when (e is IOException || e is UnauthorizedAccessException) { Log($"survey: cannot remove {SurveyRequestFile}: {e.Message}"); }
                }
            }
            else if (why != null && surveyRequest != null) Log($"survey: request withdrawn ({why}) before it started");
            survey = null;
            surveyRequest = null;
            ReleaseSurveyHold();
        }

        void ReleaseSurveyHold()
        {
            if (!surveyHolding) return;
            RestorePed();
            ClearFocus();
            surveyHolding = false;
        }

        /// <summary>Survey ray: GTA z of the first map, object, foliage or glass surface straight down through (x, y), or null.</summary>
        double? SurveyRay(double x, double y)
        {
            var hit = Cast(new Vector3((float)x, (float)y, (float)SurveyGrid.RayTopZ), new Vector3((float)x, (float)y, (float)SurveyGrid.RayBottomZ), SurveyFlags);
            return hit.DidHit ? hit.HitPosition.Z : (double?)null;
        }

        bool SurveyWater(double x, double y) =>
            Function.Call<bool>(Hash.GET_WATER_HEIGHT_NO_WAVES, (float)x, (float)y, (float)SurveyGrid.RayTopZ, new OutputArgument());

        /// <summary>Focus the streaming on the tile centre and request its collision there and at the quadrant centres.</summary>
        void KeepTileLoaded(double x, double y, double z)
        {
            float fx = (float)x, fy = (float)y, fz = (float)z;
            Function.Call(Hash.SET_FOCUS_POS_AND_VEL, fx, fy, fz, 0f, 0f, 0f);
            focusHeld = true;
            Function.Call(Hash.REQUEST_COLLISION_AT_COORD, fx, fy, fz);
            foreach (float dx in new[] { -SurveyQuadrantM, SurveyQuadrantM })
                foreach (float dy in new[] { -SurveyQuadrantM, SurveyQuadrantM })
                    Function.Call(Hash.REQUEST_ADDITIONAL_COLLISION_AT_COORD, fx + dx, fy + dy, fz);
        }

        void OnAborted(object sender, EventArgs e)
        {
            RemoveDrone();
            survey?.Stop(clock.Elapsed.TotalSeconds, "script aborted");
            ReleaseSurveyHold();
            Release("script aborted");
            try { trace?.Dispose(); } catch { }
            appliedThisTick = false;
            // No marker is drawn after the abort, so the plugin goes back to the drone view's handling.
            PublishCamera(CameraBlock.ViewDrone);
            try { camera?.Dispose(); } catch { }
            try { physicsSocket?.Close(); } catch { }
            try { sensorSocket?.Close(); } catch { }
            try { gimbalChannel?.Dispose(); } catch { }
            try { log?.Dispose(); } catch { }
        }

        /// <summary>Stream the origin and probe its ground height; true once known.</summary>
        bool ResolveGround(double now)
        {
            float x = (float)cfg.Origin.X, y = (float)cfg.Origin.Y;
            if (!groundNoticeShown)
            {
                Notification.PostTicker("HORUS GTA: waiting for the ground height at the origin", false);
                Log("probing ground height at the origin");
                groundNoticeShown = true;
                nextGroundLogS = now + ProgressLogIntervalS;
            }
            groundAttempts++;
            Function.Call(Hash.SET_FOCUS_POS_AND_VEL, x, y, GroundProbeTopZ, 0f, 0f, 0f);
            focusHeld = true;
            Function.Call(Hash.REQUEST_COLLISION_AT_COORD, x, y, 0f);
            var groundZ = new OutputArgument();
            if (Function.Call<bool>(Hash.GET_GROUND_Z_FOR_3D_COORD, x, y, GroundProbeTopZ, groundZ, false, false))
            {
                float z = groundZ.GetResult<float>();
                if (!float.IsNaN(z) && !float.IsInfinity(z))
                {
                    originZ = z;
                    Log(string.Format(CultureInfo.InvariantCulture, "ground z at the origin = {0:F2} (after {1} attempts)", z, groundAttempts));
                    // The shared view keeps streaming around the player (KeepDroneStreamed covers the drone).
                    if (shared) ClearFocus();
                    return true;
                }
            }
            if (now >= nextGroundLogS)
            {
                Log($"ground height not available yet ({groundAttempts} attempts)");
                nextGroundLogS = now + ProgressLogIntervalS;
            }
            return false;
        }

        void Activate(NedPose p)
        {
            Log(string.Format(CultureInfo.InvariantCulture,
                "activated at seq {0}: ned=({1:F2}, {2:F2}, {3:F2}) rpy=({4:F1}, {5:F1}, {6:F1}) hfov={7:F1}",
                p.Seq, p.NorthM, p.EastM, p.DownM, p.RollDeg, p.PitchDeg, p.YawDeg, p.HfovDeg));
            if (shared)
            {
                schedule.Reset();
                viewDroneLogged = viewPlayerLogged = 0;
                Log($"view: shared, drone camera every {cfg.View.DroneEvery} ticks, player keeps control");
            }
            else
            {
                Log("view: drone");
                prevMaxWanted = Function.Call<int>(Hash.GET_MAX_WANTED_LEVEL);
                Function.Call(Hash.SET_MAX_WANTED_LEVEL, 0);
            }
            frozenPed = null;
            ApplyScene();
            path.Reset();
            active = true;
        }

        /// <summary>Set [scene] weather, hour and clock freeze; RestoreScene undoes exactly what was set.</summary>
        void ApplyScene()
        {
            var scene = cfg.Scene;
            if (scene.Weather != null)
            {
                Function.Call(Hash.SET_WEATHER_TYPE_NOW_PERSIST, scene.Weather);
                weatherSet = true;
            }
            if (scene.Hour.HasValue)
            {
                prevClock = new[]
                {
                    Function.Call<int>(Hash.GET_CLOCK_HOURS), Function.Call<int>(Hash.GET_CLOCK_MINUTES),
                    Function.Call<int>(Hash.GET_CLOCK_SECONDS),
                };
                Function.Call(Hash.SET_CLOCK_TIME, scene.Hour.Value, 0, 0);
            }
            if (scene.FreezeTime)
            {
                Function.Call(Hash.PAUSE_CLOCK, true);
                clockPaused = true;
            }
            if (weatherSet || prevClock != null || clockPaused)
            {
                Log(string.Format(CultureInfo.InvariantCulture, "scene: weather {0}, clock {1}{2}",
                    scene.Weather ?? "unchanged",
                    prevClock != null ? $"{scene.Hour:00}:00 (was {prevClock[0]:00}:{prevClock[1]:00}:{prevClock[2]:00})" : "unchanged",
                    clockPaused ? ", frozen" : ""));
            }
        }

        void RestoreScene()
        {
            if (weatherSet)
            {
                Function.Call(Hash.CLEAR_WEATHER_TYPE_PERSIST);   // the game's weather cycle resumes
                weatherSet = false;
            }
            if (clockPaused)
            {
                Function.Call(Hash.PAUSE_CLOCK, false);
                clockPaused = false;
            }
            if (prevClock != null)
            {
                Function.Call(Hash.SET_CLOCK_TIME, prevClock[0], prevClock[1], prevClock[2]);
                prevClock = null;
            }
        }

        /// <summary>
        /// Put the scripted camera at the pose and on screen. In the shared view that happens
        /// on drone ticks only ([view] drone_every); the other ticks give the screen back to the
        /// gameplay camera, and the streaming focus stays with the player. True when this tick's
        /// frame renders the drone camera (every tick in the drone view; the F10 outside view aside).
        /// </summary>
        bool Apply(NedPose ned, double now)
        {
            bool droneTick = !shared || schedule.Next();
            if (droneTick) appliedSeq = ned.Seq;
            var resolution = Screen.Resolution;
            var pose = PoseMapping.Map(ned, cfg.Origin, originZ.Value, resolution.Width, resolution.Height);
            float x = (float)pose.X, y = (float)pose.Y, z = (float)pose.Z;
            var here = new Point3(x, y, z);
            if (path.NextSegment(here, out var from)) ProbeSegment(from, here, now);

            if (cam == -1 || !Function.Call<bool>(Hash.DOES_CAM_EXIST, cam))
            {
                cam = Function.Call<int>(Hash.CREATE_CAM_WITH_PARAMS, CameraName, x, y, z,
                    pose.RotX, pose.RotY, pose.RotZ, pose.VfovDeg, true, PoseMapping.RotationOrder);
                Log($"scripted camera {cam} created");
            }
            if (droneTick)
            {
                Function.Call(Hash.SET_CAM_COORD, cam, x, y, z);
                Function.Call(Hash.SET_CAM_ROT, cam, pose.RotX, pose.RotY, pose.RotZ, PoseMapping.RotationOrder);
                Function.Call(Hash.SET_CAM_FOV, cam, pose.VfovDeg);
                camSet = pose;
                // The outside view (F10) renders its own camera; the drone camera keeps its pose meanwhile.
                // The shared view turns script cams on at every drone tick (the player tick before turned them off).
                if (!outsideView && ((shared && !scriptCamsOn) || !Function.Call<bool>(Hash.IS_CAM_RENDERING, cam)))
                {
                    Function.Call(Hash.SET_CAM_ACTIVE, cam, true);
                    Function.Call(Hash.RENDER_SCRIPT_CAMS, true, false, 0, true, false, 0);
                    scriptCamsOn = true;
                }
            }
            else if (scriptCamsOn || Function.Call<bool>(Hash.IS_CAM_RENDERING, cam))
            {
                Function.Call(Hash.RENDER_SCRIPT_CAMS, false, false, 0, true, false, 0);
                scriptCamsOn = false;
            }
            if (shared) KeepDroneStreamed(x, y, z);
            else
            {
                // Stream the world around the drone camera, not around the player.
                Function.Call(Hash.SET_FOCUS_POS_AND_VEL, x, y, z, 0f, 0f, 0f);
                focusHeld = true;
            }
            if (droneTick) Function.Call(Hash.HIDE_HUD_AND_RADAR_THIS_FRAME);
            if (!shared) HoldPlayer();
            return droneTick;
        }

        /// <summary>
        /// Shared view: keep the drone's surroundings loaded without taking the streaming focus off
        /// the player. Collision (what the physics rays and range sensors hit) is requested every
        /// tick; map models stay streamed by a sphere volume, re-created when the drone moves more
        /// than <see cref="StreamAnchor.RecentreM"/> from its centre.
        /// </summary>
        void KeepDroneStreamed(float x, float y, float z)
        {
            Function.Call(Hash.REQUEST_COLLISION_AT_COORD, x, y, z);
            if (StreamVolumeRadiusM <= 0 || !streamAnchor.Update(new Point3(x, y, z))) return;
            DeleteStreamVolume();
            int handle = Function.Call<int>(Hash.STREAMVOL_CREATE_SPHERE, x, y, z, StreamVolumeRadiusM, StreamVolumeFlags, StreamVolumeLodFlags);
            if (Function.Call<bool>(Hash.STREAMVOL_IS_VALID, handle))
            {
                streamVolume = handle;
                streamVolumeHeld = true;
                if (!streamVolumeLogged)
                    Log(string.Format(CultureInfo.InvariantCulture,
                        "view: stream volume {0} around the drone at ({1:F1}, {2:F1}, {3:F1}), radius {4:g} m, flags {5}, lod flags {6}; moved when the drone is {7:g} m away",
                        handle, x, y, z, StreamVolumeRadiusM, StreamVolumeFlags, StreamVolumeLodFlags, StreamAnchor.RecentreM));
                streamVolumeLogged = true;
            }
            else
            {
                if (!streamVolumeFailedLogged)
                    Log("view: STREAMVOL_CREATE_SPHERE gave no valid volume; the drone's map models load only as the game streams them (collision is still requested)");
                streamVolumeFailedLogged = true;
            }
        }

        void DeleteStreamVolume()
        {
            if (!streamVolumeHeld) return;
            if (Function.Call<bool>(Hash.STREAMVOL_IS_VALID, streamVolume)) Function.Call(Hash.STREAMVOL_DELETE, streamVolume);
            streamVolumeHeld = false;
        }

        void ClearFocus()
        {
            if (!focusHeld) return;
            Function.Call(Hash.CLEAR_FOCUS);
            focusHeld = false;
        }

        /// <summary>
        /// Shape-test the camera's move from <paramref name="from"/> to <paramref name="to"/> against
        /// the world (map collision, vehicles, peds, objects, foliage, water; the player's own ped is
        /// ignored) and log each new intersection with its running count.
        /// </summary>
        void ProbeSegment(Point3 from, Point3 to, double now)
        {
            var a = new Vector3((float)from.X, (float)from.Y, (float)from.Z);
            var b = new Vector3((float)to.X, (float)to.Y, (float)to.Z);
            RaycastResult hit = Cast(a, b, IntersectFlags.Everything);
            if (!hit.DidHit) return;
            var at = hit.HitPosition;
            if (!path.RecordHit(new Point3(at.X, at.Y, at.Z), now)) return;
            Log(string.Format(CultureInfo.InvariantCulture,
                "intersection #{0}: camera moved through {1} at ({2:F2}, {3:F2}, {4:F2}), normal ({5:F2}, {6:F2}, {7:F2}); segment ({8:F2}, {9:F2}, {10:F2}) -> ({11:F2}, {12:F2}, {13:F2}); {14} repeat hits so far",
                path.Intersections, Describe(hit.HitEntity), at.X, at.Y, at.Z,
                hit.SurfaceNormal.X, hit.SurfaceNormal.Y, hit.SurfaceNormal.Z,
                a.X, a.Y, a.Z, b.X, b.Y, b.Z, path.Repeats));
        }

        /// <summary>Synchronous GTA ray from a to b against <see cref="Solid"/>: the surface point, normal and entity hit.</summary>
        Contact? WorldRay(Vec3 a, Vec3 b)
        {
            var hit = Cast(V3(a), V3(b), Solid);
            if (!hit.DidHit) return null;
            var p = hit.HitPosition;
            var n = hit.SurfaceNormal;
            return new Contact
            {
                Position = new Vec3(p.X, p.Y, p.Z),
                Normal = new Vec3(n.X, n.Y, n.Z),
                Entity = hit.HitEntity != null && hit.HitEntity.Exists() ? hit.HitEntity.Handle : 0,
            };
        }

        /// <summary>
        /// GTA ray ignoring the player's ped and the drone prop. The prop has no collision, so rays
        /// should pass through it; should one still hit it, it is counted and cast again ignoring it.
        /// </summary>
        RaycastResult Cast(Vector3 a, Vector3 b, IntersectFlags flags)
        {
            var hit = World.Raycast(a, b, flags, rayIgnore);
            if (drone != null && hit.DidHit && hit.HitEntity != null && hit.HitEntity.Handle == drone.Handle)
            {
                droneSelfHits++;
                hit = World.Raycast(a, b, flags, drone);
            }
            return hit;
        }

        /// <summary>For the 5 s log: the prop's self-hit count and a plain ray straight down through it.</summary>
        string DroneRayCheck(QuadBody body)
        {
            if (drone == null || !drone.Exists()) return "; no drone prop";
            var top = V3(body.P + new Vec3(0, 0, 0.5));
            var bottom = V3(body.P - new Vec3(0, 0, 0.1));
            var hit = World.Raycast(top, bottom, IntersectFlags.Everything, rayIgnore);
            string through = !hit.DidHit ? "nothing" : hit.HitEntity != null && hit.HitEntity.Handle == drone.Handle ? "the prop" : Describe(hit.HitEntity);
            return $"; drone prop: {droneSelfHits} ray hits so far, a ray down through it hits {through}";
        }

        /// <summary>
        /// Log each new contact of the quad (repeats within 1 m and 1 s, e.g. resting on the ground or
        /// pressed against a wall, are only counted).
        /// </summary>
        void OnContact(Contact c, double speedIn)
        {
            if (!contacts.RecordHit(new Point3(c.Position.X, c.Position.Y, c.Position.Z), physics.SimTimeS)) return;
            Vec3 ned = physics.Frame.ToNed(c.Position - physics.Frame.Origin);
            Log(string.Format(CultureInfo.InvariantCulture,
                "physics: contact #{0}: quad hit {1} at ({2:F2}, {3:F2}, {4:F2}) = ned ({5:F2}, {6:F2}, {7:F2}), normal ({8:F2}, {9:F2}, {10:F2}), {11:F2} m/s into it, sim {12:F2} s; {13} repeat hits so far",
                contacts.Intersections, c.Entity == 0 ? "map" : Describe(Entity.FromHandle(c.Entity)), c.Position.X, c.Position.Y, c.Position.Z,
                ned.X, ned.Y, ned.Z, c.Normal.X, c.Normal.Y, c.Normal.Z, speedIn, physics.SimTimeS, contacts.Repeats));
        }

        /// <summary>"map" for world collision (no entity), else the entity type and model hash.</summary>
        static string Describe(Entity entity)
        {
            if (entity == null || !entity.Exists()) return "map";
            int type = Function.Call<int>(Hash.GET_ENTITY_TYPE, entity.Handle);
            string kind = type == 1 ? "ped" : type == 2 ? "vehicle" : type == 3 ? "object" : $"entity type {type}";
            return $"{kind} 0x{(uint)Function.Call<int>(Hash.GET_ENTITY_MODEL, entity.Handle):X8}";
        }

        void HoldPlayer()
        {
            var player = Game.Player;
            var ped = player.Character;
            if (ped != null && ped.Exists() && (frozenPed == null || ped.Handle != frozenPed.Handle))
            {
                RestorePed();
                frozenPed = ped;
                prevFrozen = ped.IsPositionFrozen;
                prevInvincible = player.IsInvincible;
            }
            if (frozenPed != null && frozenPed.Exists()) frozenPed.IsPositionFrozen = true;
            player.IsInvincible = true;
            if (Function.Call<int>(Hash.GET_PLAYER_WANTED_LEVEL, player.Handle) != 0)
            {
                Function.Call(Hash.SET_PLAYER_WANTED_LEVEL, player.Handle, 0, false);
                Function.Call(Hash.SET_PLAYER_WANTED_LEVEL_NOW, player.Handle, false);
            }
        }

        void RestorePed()
        {
            if (frozenPed != null && frozenPed.Exists()) frozenPed.IsPositionFrozen = prevFrozen;
            if (frozenPed != null) Game.Player.IsInvincible = prevInvincible;
            frozenPed = null;
        }

        /// <summary>
        /// Hand the screen back to the game: stop the camera, focus, stream volume and (drone view
        /// only) the player hold. The shared view never touched the player.
        /// </summary>
        void Release(string why)
        {
            if (cam != -1)
            {
                Function.Call(Hash.RENDER_SCRIPT_CAMS, false, false, 0, true, false, 0);
                if (Function.Call<bool>(Hash.DOES_CAM_EXIST, cam)) Function.Call(Hash.DESTROY_CAM, cam, false);
                cam = -1;
            }
            scriptCamsOn = false;
            ClearFocus();
            DeleteStreamVolume();
            streamAnchor.Clear();
            if (active)
            {
                if (!shared) RestorePed();
                RestoreScene();
                if (!shared) Function.Call(Hash.SET_MAX_WANTED_LEVEL, Math.Max(prevMaxWanted, 0));
                active = false;
                Log($"deactivated: {why}; camera path so far: {path.Intersections} intersections, {path.Repeats} repeat hits"
                    + (shared ? $"; view: {schedule.DroneTicks} drone / {schedule.PlayerTicks} player ticks" : ""));
            }
            else if (groundNoticeShown && !originZ.HasValue)
            {
                Log($"stopped probing the ground: {why}");
                groundNoticeShown = false;
            }
        }
    }
}
