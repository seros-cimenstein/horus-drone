using System;
using System.IO;
using System.IO.MemoryMappedFiles;
using System.Text.Json;
using HorusGta;

static class Tests
{
    static int failures, checks;

    static void Check(bool ok, string what)
    {
        checks++;
        if (ok) return;
        failures++;
        Console.WriteLine("FAIL: " + what);
    }

    static void Near(double actual, double expected, string what, double tol = 1e-9) =>
        Check(Math.Abs(actual - expected) <= tol, $"{what}: got {actual:R}, expected {expected:R}");

    static int Main()
    {
        Run(PositionMapping);
        Run(HeadingMatchesPosition);
        Run(RotationMapping);
        Run(VerticalFov);
        Run(ConfigDefaults);
        Run(ConfigParsing);
        Run(SceneConfig);
        Run(PhysicsConfig);
        Run(ShippedIni);
        Run(ViewConfig);
        Run(ViewScheduleParity);
        Run(MarkerRect);
        Run(StreamAnchorRecentre);
        Run(CameraBlockLayout);
        Run(CameraPathSegments);
        Run(CameraPathRepeats);
        Run(ServoFrameDecode);
        Run(GimbalConfig);
        Run(GimbalReportsEveryChange);
        Run(GimbalDatagrams);
        Run(GimbalHoldsTheSetpoint);
        Run(GimbalReports);
        Run(GimbalChannelRoundTrip);
        Run(QuadHover);
        Run(QuadTorqueSigns);
        Run(QuadRestsOnFloor);
        Run(NedFrameMatchesPoseMapping);
        Run(FdmJsonLine);
        Run(PhysicsLinkLockstep);
        Run(WorldProbeRays);
        Run(QuadHoldsOnSlopes);
        Run(QuadStopsAtWall);
        Run(ImuSaturates);
        Run(CameraOnTheMount);
        Run(DroneOrientation);
        Run(SensorConfig);
        Run(RangeDatagramLayout);
        Run(RangeBeamDirections);
        Run(RangeHullOffsets);
        Run(RangeSchedule);
        Run(RangeSensorsDisabled);
        Run(RangeSensorsOnPhysicsLink);
        Run(MiniJsonParse);
        Run(SurveyGridCells);
        Run(SurveyTileFormat);
        Run(SurveyRequestParsing);
        Run(SurveyRunWritesTiles);
        Run(SurveyResumes);
        Run(SurveyUnloadedAndWaterTiles);
        Console.WriteLine(failures == 0 ? $"OK: {checks} checks passed" : $"{failures} of {checks} checks FAILED");
        return failures == 0 ? 0 : 1;
    }

    static void Run(Action test)
    {
        try { test(); }
        catch (Exception e) { failures++; Console.WriteLine($"FAIL: {test.Method.Name} threw {e}"); }
    }

    static NedPose Sample() => new NedPose
    {
        Seq = 7, NorthM = 1, EastM = 2, DownM = -3, RollDeg = 0, PitchDeg = -10, YawDeg = 45, HfovDeg = 90,
    };

    static readonly Origin Legion = new Origin { X = 195, Y = -933, HeadingDeg = 0 };

    static GtaCameraPose MapNed(double n, double e, double d, Origin o, double originZ = 30, float yaw = 0)
    {
        var p = Sample();
        p.NorthM = n; p.EastM = e; p.DownM = d; p.YawDeg = yaw;
        return PoseMapping.Map(p, o, originZ, 1920, 1080);
    }

    static void PositionMapping()
    {
        var home = MapNed(0, 0, 0, Legion);
        Near(home.X, 195, "home x"); Near(home.Y, -933, "home y"); Near(home.Z, 30, "home z");
        var north = MapNed(10, 0, 0, Legion);
        Near(north.X, 195, "north x"); Near(north.Y, -923, "north y");
        var east = MapNed(0, 10, 0, Legion);
        Near(east.X, 205, "east x"); Near(east.Y, -933, "east y");
        Near(MapNed(0, 0, -20, Legion).Z, 50, "20 m above home");
        Near(MapNed(0, 0, 5, Legion, originZ: 30).Z, 25, "below home");

        // NED north at GTA heading 90 (counter-clockwise from +Y) points to GTA -X; east then points to +Y.
        var turned = new Origin { X = 0, Y = 0, HeadingDeg = 90 };
        var n90 = MapNed(10, 0, 0, turned);
        Near(n90.X, -10, "heading 90 north x", 1e-9); Near(n90.Y, 0, "heading 90 north y", 1e-9);
        var e90 = MapNed(0, 10, 0, turned);
        Near(e90.X, 0, "heading 90 east x", 1e-9); Near(e90.Y, 10, "heading 90 east y", 1e-9);

        var vector = new NedPose { NorthM = 12.5, EastM = -3.25, DownM = -20, PitchDeg = -45, RollDeg = 1.5f, YawDeg = 270, HfovDeg = 84 };
        var v = PoseMapping.Map(vector, Legion, 30, 1920, 1080);
        Near(v.X, 191.75, "vector x"); Near(v.Y, -920.5, "vector y"); Near(v.Z, 50, "vector z");
        Near(v.RotX, -45, "vector rot x"); Near(v.RotY, 1.5, "vector rot y"); Near(v.RotZ, 90, "vector rot z");
    }

    /// <summary>The camera's GTA heading must point where the same NED yaw moves the position.</summary>
    static void HeadingMatchesPosition()
    {
        foreach (double originHeading in new[] { 0.0, 37.0, 90.0, -120.0, 200.0 })
            foreach (float yaw in new[] { 0f, 30f, 90f, 135f, 180f, 270f, 359f, -45f })
            {
                var o = new Origin { X = 100, Y = 50, HeadingDeg = originHeading };
                double psi = yaw * Math.PI / 180;
                var ahead = MapNed(Math.Cos(psi), Math.Sin(psi), 0, o, yaw: yaw);
                double dx = ahead.X - o.X, dy = ahead.Y - o.Y;
                double h = ahead.RotZ * Math.PI / 180;   // GTA forward for heading h: (-sin h, cos h)
                Near(dx, -Math.Sin(h), $"forward x (origin {originHeading}, yaw {yaw})", 1e-5);
                Near(dy, Math.Cos(h), $"forward y (origin {originHeading}, yaw {yaw})", 1e-5);
            }
    }

    static void RotationMapping()
    {
        Near(MapNed(0, 0, 0, Legion, yaw: 90).RotZ, -90, "yaw 90 (east) is GTA heading -90");
        Near(MapNed(0, 0, 0, Legion, yaw: 180).RotZ, 180, "yaw 180 wraps to 180");
        Near(MapNed(0, 0, 0, new Origin { HeadingDeg = 90 }, yaw: 0).RotZ, 90, "origin heading 90, yaw 0");
        Near(PoseMapping.WrapDeg(-180), 180, "wrap -180");
        Near(PoseMapping.WrapDeg(540), 180, "wrap 540");
        Near(PoseMapping.WrapDeg(-190), 170, "wrap -190");
        Near(PoseMapping.WrapDeg(725), 5, "wrap 725");
        Near(PoseMapping.WrapDeg(0), 0, "wrap 0");
    }

    static void VerticalFov()
    {
        Near(PoseMapping.VerticalFovDeg(90, 1920, 1080), 2 * Math.Atan(0.5625) * 180 / Math.PI, "90 deg hfov at 16:9");
        Near(PoseMapping.VerticalFovDeg(90, 1000, 1000), 90, "square screen", 1e-9);
        Near(PoseMapping.VerticalFovDeg(90, 0, 0), PoseMapping.VerticalFovDeg(90, 1920, 1080), "unknown size uses 16:9");
        Near(PoseMapping.VerticalFovDeg(84, 2560, 1440), 2 * Math.Atan(Math.Tan(42 * Math.PI / 180) * 0.5625) * 180 / Math.PI, "84 deg hfov");
        var p = Sample();
        p.HfovDeg = 179.5f;
        Near(PoseMapping.Map(p, Legion, 0, 1080, 1920).VfovDeg, 130, "vfov clamped to 130");
        p.HfovDeg = 0.5f;
        Near(PoseMapping.Map(p, Legion, 0, 1920, 1080).VfovDeg, 1, "vfov clamped to 1");
    }

    static void ConfigDefaults()
    {
        var c = ModConfig.Parse("");
        Near(c.TimeoutS, 1.9, "timeout default");
        Near(c.Origin.X, 195, "origin x default"); Near(c.Origin.Y, -933, "origin y default");
        Near(c.Origin.HeadingDeg, 0, "heading default");
        Check(!c.Origin.Z.HasValue, "z probes ground by default");
        Check(c.Scene.Weather == null && !c.Scene.Hour.HasValue && !c.Scene.FreezeTime, "scene defaults: the game's own weather and clock");
        Check(c.Warnings.Count == 0, "no warnings for empty file");
    }

    static void ConfigParsing()
    {
        var c = ModConfig.Parse("; comment\r\n[POSE]\r\ntimeout_s=0.5 ; inline\r\n"
            + "[origin]\nx=-1000.5\ny=2500\nheading_deg=-30\nz=42.25\n# end\n");
        Near(c.TimeoutS, 0.5, "timeout parsed");
        Near(c.Origin.X, -1000.5, "x parsed"); Near(c.Origin.Y, 2500, "y parsed");
        Near(c.Origin.HeadingDeg, -30, "heading parsed");
        Check(c.Origin.Z == 42.25, "z parsed");
        Check(c.Warnings.Count == 0, "valid file has no warnings: " + string.Join(" | ", c.Warnings));

        var bad = ModConfig.Parse("[pose]\nbind=localhost\nport=70000\ntimeout_s=0\nfoo=1\n[origin]\nx=abc\ny=nan\nheading_deg=\nz=high\nnot a pair\n[broken\nport=1\n");
        Check(bad.Warnings.Exists(w => w.Contains("unknown setting [pose] bind")) && bad.Warnings.Exists(w => w.Contains("unknown setting [pose] port")),
            "the removed pose receiver's bind and port are unknown settings");
        Near(bad.TimeoutS, 1.9, "bad timeout falls back");
        Near(bad.Origin.X, 195, "bad x falls back"); Near(bad.Origin.Y, -933, "nan y falls back");
        Near(bad.Origin.HeadingDeg, 0, "empty heading falls back");
        Check(!bad.Origin.Z.HasValue, "bad z falls back to probing");
        Check(bad.Warnings.Count == 11, $"one warning per bad line (got {bad.Warnings.Count}: {string.Join(" | ", bad.Warnings)})");
        Check(bad.Warnings[0].StartsWith("HorusGta.ini line 2:"), "warnings carry line numbers");
    }

    static void SceneConfig()
    {
        var c = ModConfig.Parse("[scene]\nweather = clouds\nhour=0\nfreeze_time=FALSE\n");
        Check(c.Scene.Weather == "CLOUDS" && c.Scene.Hour == 0 && !c.Scene.FreezeTime, "scene parsed, weather case-insensitive");
        Check(c.Warnings.Count == 0, "valid scene has no warnings: " + string.Join(" | ", c.Warnings));

        var leave = ModConfig.Parse("[scene]\nweather=\nhour=\nfreeze_time=\n");
        Check(leave.Scene.Weather == null && leave.Scene.Hour == null && !leave.Scene.FreezeTime, "empty values leave the game's scene");
        Check(leave.Warnings.Count == 0, "empty scene values are not warnings");

        var bad = ModConfig.Parse("[scene]\nweather=sunny\nhour=24\nfreeze_time=yes\n");
        Check(bad.Scene.Weather == null && !bad.Scene.Hour.HasValue && !bad.Scene.FreezeTime, "bad scene values fall back to the defaults");
        Check(bad.Warnings.Count == 3 && bad.Warnings[0].Contains("scene.weather 'sunny'"), "one warning per bad scene line: " + string.Join(" | ", bad.Warnings));
    }

    static void PhysicsConfig()
    {
        var d = ModConfig.Parse("");
        Check(d.Physics.Bind == "127.0.0.1" && d.Physics.Port == 9002, "physics defaults: SITL JSON port 9002 on loopback");
        Near(d.Physics.BudgetMs, 10, "budget default"); Near(d.Physics.HfovDeg, 84, "hfov default = horus_gta.yaml");
        Near(d.Physics.TraceHz, 0, "trace off by default");
        var c = ModConfig.Parse("[physics]\nbind=0.0.0.0\nport=9102\nbudget_ms=12.5\nhfov_deg=90\ntrace_hz=20\n");
        Check(c.Physics.Bind == "0.0.0.0" && c.Physics.Port == 9102, "physics endpoint parsed");
        Near(c.Physics.BudgetMs, 12.5, "budget parsed"); Near(c.Physics.HfovDeg, 90, "hfov parsed"); Near(c.Physics.TraceHz, 20, "trace rate parsed");
        Check(c.Warnings.Count == 0, "valid physics section has no warnings");
        var bad = ModConfig.Parse("[physics]\nbind=host\nport=0\nbudget_ms=31\nhfov_deg=0\ntrace_hz=-1\n");
        Check(bad.Physics.Bind == "127.0.0.1" && bad.Physics.Port == 9002, "bad physics endpoint falls back");
        Near(bad.Physics.BudgetMs, 10, "budget over 30 ms falls back"); Near(bad.Physics.HfovDeg, 84, "hfov 0 falls back");
        Near(bad.Physics.TraceHz, 0, "negative trace rate falls back");
        Check(bad.Warnings.Count == 5, "one warning per bad physics line: " + string.Join(" | ", bad.Warnings));
    }

    static void ShippedIni()
    {
        var c = ModConfig.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "HorusGta.ini")));
        Check(c.Warnings.Count == 0, "shipped HorusGta.ini parses cleanly: " + string.Join(" | ", c.Warnings));
        Check(!c.Origin.Z.HasValue, "shipped ini probes the ground at the origin");
        Near(c.TimeoutS, new ModConfig().TimeoutS, "shipped timeout equals the default");
        Near(c.Origin.X, 195, "shipped origin x"); Near(c.Origin.Y, -933, "shipped origin y");
        Check(c.Scene.Weather == null && !c.Scene.Hour.HasValue && !c.Scene.FreezeTime, "shipped scene: the game's own weather and clock");
        var d = new ModConfig().Physics;
        Check(c.Physics.Bind == d.Bind && c.Physics.Port == d.Port && c.Physics.BudgetMs == d.BudgetMs && c.Physics.HfovDeg == d.HfovDeg
            && c.Physics.TraceHz == d.TraceHz, "shipped [physics] equals the defaults");
        var s = new ModConfig().Sensors;
        Check(c.Sensors.Enabled == s.Enabled && c.Sensors.Host == s.Host && c.Sensors.Port == s.Port && c.Sensors.RateHz == s.RateHz,
            "shipped [sensors] equals the defaults");
        var v = new ModConfig().View;
        Check(c.View.Mode == v.Mode && c.View.DroneEvery == v.DroneEvery, "shipped [view] equals the defaults (drone camera on screen)");
    }

    static void ViewConfig()
    {
        var d = ModConfig.Parse("");
        Check(d.View.Mode == ViewMode.Drone && d.View.DroneEvery == 2, "view defaults: drone camera on screen, drone_every 2");
        var c = ModConfig.Parse("[view]\nmode = Shared\ndrone_every=3 ; comment\n");
        Check(c.View.Mode == ViewMode.Shared && c.View.DroneEvery == 3, "view parsed, mode case-insensitive");
        Check(c.Warnings.Count == 0, "valid view section has no warnings: " + string.Join(" | ", c.Warnings));
        Check(ModConfig.Parse("[view]\nmode=drone\ndrone_every=4\n").View.Mode == ViewMode.Drone, "mode=drone parsed");
        Check(ModConfig.Parse("[view]\ndrone_every=4\n").View.DroneEvery == 4, "drone_every 4 accepted");
        Check(ModConfig.Parse("[view]\ndrone_every=2\n").View.DroneEvery == 2, "drone_every 2 accepted");
        var bad = ModConfig.Parse("[view]\nmode=both\ndrone_every=1\ndrone_every=5\ndrone_every=2.5\ndrone_every=\nmode=\n");
        Check(bad.View.Mode == ViewMode.Drone && bad.View.DroneEvery == 2, "bad view values fall back to the defaults");
        Check(bad.Warnings.Count == 6 && bad.Warnings[0].Contains("view.mode 'both'") && bad.Warnings[0].EndsWith("using the default")
            && bad.Warnings[1].Contains("view.drone_every '1' is not 2..4"), "one warning per bad view line: " + string.Join(" | ", bad.Warnings));
        var keep = ModConfig.Parse("[view]\nmode=shared\ndrone_every=3\ndrone_every=9\n");
        Check(keep.View.Mode == ViewMode.Shared && keep.View.DroneEvery == 3 && keep.Warnings.Count == 1, "a bad line keeps the earlier valid value");
    }

    // Shared view: the first tick of an activation renders the drone camera, then one in drone_every.
    static void ViewScheduleParity()
    {
        Check(SharedView.IsDroneTick(0, 2) && !SharedView.IsDroneTick(1, 2) && SharedView.IsDroneTick(2, 2), "every 2: even ticks are drone ticks");
        Check(SharedView.IsDroneTick(3, 3) && !SharedView.IsDroneTick(4, 3) && !SharedView.IsDroneTick(5, 3) && SharedView.IsDroneTick(6, 3), "every 3");
        foreach (int every in new[] { 2, 3, 4 })
        {
            var s = new ViewSchedule(every);
            string pattern = "";
            for (int i = 0; i < 3 * every; i++) pattern += s.Next() ? "D" : "p";
            string one = "D" + new string('p', every - 1);
            Check(pattern == one + one + one, $"every {every}: {pattern}");
            Check(s.DroneTicks == 3 && s.PlayerTicks == 3 * (every - 1), $"every {every}: counts {s.DroneTicks} drone / {s.PlayerTicks} player");
            s.Next();
            s.Reset();
            Check(s.DroneTicks == 0 && s.PlayerTicks == 0, $"every {every}: reset clears the counts");
            Check(s.Next(), $"every {every}: the first tick after a reset is a drone tick");
        }
        foreach (int bad in new[] { 1, 5, 0 })
        {
            bool threw = false;
            try { new ViewSchedule(bad); } catch (ArgumentOutOfRangeException) { threw = true; }
            Check(threw, $"drone_every {bad} rejected");
        }
    }

    // The marker horus_capture.asi classifies (texel (2, 2), drone iff max(R, G, B) <= 16).
    static void MarkerRect()
    {
        Check(SharedView.MarkerWidth == 0.00625f && SharedView.MarkerHeight == 0.0111f, "marker size 0.00625 x 0.0111");
        Check(SharedView.MarkerCenterX == 0.003125f && SharedView.MarkerCenterY == 0.00555f, "DRAW_RECT centre (0.003125, 0.00555)");
        Near(SharedView.MarkerCenterX - SharedView.MarkerWidth / 2, 0, "marker left edge at x 0", 1e-9);
        Near(SharedView.MarkerCenterY - SharedView.MarkerHeight / 2, 0, "marker top edge at y 0", 1e-9);
        Near(SharedView.MarkerWidth * 1280, 8, "8 px wide at 1280", 1e-3);
        Near(SharedView.MarkerHeight * 720, 8, "8 px high at 720", 0.01);
        foreach (var (w, h) in new[] { (1280, 720), (1920, 1080), (2560, 1440), (3840, 2160) })
        {
            // Pixel (2, 2)'s centre (2.5, 2.5) lies inside the rectangle, so the marker's colour is there.
            Check(2.5 < SharedView.MarkerWidth * w && 2.5 < SharedView.MarkerHeight * h, $"texel (2, 2) inside the marker at {w}x{h}");
        }
        Check(SharedView.DroneMarkerLevel <= 16 && SharedView.PlayerMarkerLevel > 16, "drone marker classifies as drone, player marker as player");
        Check(SharedView.DroneMarkerLevel == 0 && SharedView.PlayerMarkerLevel == 255 && SharedView.MarkerAlpha == 255, "black / white, opaque");
    }

    // The stream volume around the drone moves with it once it is more than 30 m from its centre.
    static void StreamAnchorRecentre()
    {
        var a = new StreamAnchor();
        Check(a.Centre == null, "no centre before the first update");
        Check(a.Update(new Point3(100, 200, 30)), "first position creates the volume");
        Check(!a.Update(new Point3(100, 229.9, 30)), "29.9 m away: kept");
        Check(!a.Update(new Point3(100, 230, 30)), "30 m away: kept");
        Check(a.Centre.Value.Y == 200, "centre unchanged while kept");
        Check(a.Update(new Point3(100, 200, 60.5)), "30.5 m up: recreated");
        Check(a.Centre.Value.Z == 60.5, "new centre at the drone");
        Check(!a.Update(new Point3(110, 200, 60.5)), "measured from the new centre");
        a.Clear();
        Check(a.Centre == null && a.Update(new Point3(110, 200, 60.5)), "after release the next update creates a volume again");
    }

    // The bytes horus_capture.asi reads (gta/common/camera_block.hpp): magic, version, seqlock
    // sequence (even when not writing, +2 per tick), active, pose seq, game frame, view mode.
    static void CameraBlockLayout()
    {
        var file = MemoryMappedFile.CreateNew(null, CameraBlock.Size);
        using var block = new CameraBlock(file);
        using var reader = file.CreateViewAccessor(0, CameraBlock.Size);   // second view, like the plugin's
        byte[] Dump()
        {
            var raw = new byte[CameraBlock.Size];
            reader.ReadArray(0, raw, 0, raw.Length);
            return raw;
        }
        Check(block.Ticks == 0, "fresh block has no ticks");
        block.Publish(true, 907, 5120, CameraBlock.ViewDrone);
        block.Publish(true, 908, 5121, CameraBlock.ViewDrone);
        Check(block.Ticks == 2, "two ticks counted");
        string hex = Convert.ToHexString(Dump()).ToLowerInvariant();
        Check(hex == "4847434d" + "01000000" + "0400000000000000" + "01000000" + "00000000"
            + "8c03000000000000" + "0114000000000000" + new string('0', 48),
            "block bytes after two active ticks: " + hex);
        block.Publish(false, 908, 5122, CameraBlock.ViewDrone);
        byte[] after = Dump();
        Check(BitConverter.ToUInt64(after, CameraBlock.SequenceOffset) == 6, "third tick: sequence 6 (even)");
        Check(BitConverter.ToUInt32(after, CameraBlock.ActiveOffset) == 0, "inactive tick clears active");
        Check(BitConverter.ToUInt64(after, CameraBlock.GameFrameOffset) == 5122, "game frame updated while inactive");

        // Shared view: view_mode 1 at offset 40, the rest of the block (44..63) stays zero.
        Check(CameraBlock.ViewModeOffset == 40 && CameraBlock.ViewDrone == 0 && CameraBlock.ViewShared == 1, "view_mode at offset 40: 0 drone, 1 shared");
        block.Publish(false, 908, 5123, CameraBlock.ViewShared);
        block.Publish(true, 909, 5124, CameraBlock.ViewShared);
        hex = Convert.ToHexString(Dump()).ToLowerInvariant();
        Check(hex == "4847434d" + "01000000" + "0a00000000000000" + "01000000" + "00000000"
            + "8d03000000000000" + "0414000000000000" + "01000000" + new string('0', 40),
            "block bytes after a shared-view drone tick: " + hex);
        block.Publish(false, 909, 5125, CameraBlock.ViewDrone);
        after = Dump();
        Check(BitConverter.ToUInt32(after, CameraBlock.ViewModeOffset) == 0 && BitConverter.ToUInt64(after, CameraBlock.SequenceOffset) == 12,
            "view_mode back to 0, written under the seqlock");
    }

    // The segments HorusGta shape-tests between consecutive camera positions.
    static void CameraPathSegments()
    {
        var path = new CameraPath();
        var a = new Point3(195, -963, 54.69);
        var b = new Point3(195, -962.8, 54.69);
        Check(!path.NextSegment(a, out _), "first position after start: no segment (camera placed)");
        Check(path.NextSegment(b, out var from) && from.Y == -963, "moved: segment from the previous position");
        Check(!path.NextSegment(b, out _), "unchanged pose: no zero-length segment");
        var c = new Point3(195, -962.6, 54.69);
        Check(path.NextSegment(c, out from) && from.Y == -962.8, "segment starts at the last position, not the last tested one");
        path.Reset();
        Check(!path.NextSegment(new Point3(0, 0, 100), out _), "after Reset the jump to the new position is not tested");
        Check(path.NextSegment(new Point3(0, 1, 100), out from) && from.Y == 0 && from.Z == 100, "then segments resume");
    }

    static void CameraPathRepeats()
    {
        var path = new CameraPath();
        var wall = new Point3(210, -930, 40);
        Check(path.RecordHit(wall, 10.0), "first hit is new");
        Check(!path.RecordHit(new Point3(210.3, -930, 40), 10.03), "jitter at the same surface next tick: repeat");
        Check(!path.RecordHit(new Point3(210, -930.4, 40.2), 10.9), "still within 1 m and 1 s of the last hit: repeat");
        Check(!path.RecordHit(wall, 11.8), "repeats extend the window (0.9 s after the last repeat)");
        Check(path.RecordHit(wall, 12.9), "same spot after 1.1 s quiet: new");
        Check(path.RecordHit(new Point3(230, -930, 40), 12.95), "another surface 20 m away within the window: new");
        Check(path.Intersections == 3 && path.Repeats == 3, $"counts 3 new / 3 repeats, got {path.Intersections}/{path.Repeats}");
        path.Reset();
        Check(!path.RecordHit(new Point3(230.5, -930, 40), 13.0), "Reset keeps the repeat window");
        Check(path.Intersections == 3 && path.Repeats == 4, "Reset keeps the counts");
    }

    // ArduPilot JSON SITL backend.
    static byte[] ServoBytes(uint frame, ushort rate, ushort[] pwm, ushort magic = SitlJson.Magic16)
    {
        var b = new byte[8 + 2 * pwm.Length];
        BitConverter.GetBytes(magic).CopyTo(b, 0);
        BitConverter.GetBytes(rate).CopyTo(b, 2);
        BitConverter.GetBytes(frame).CopyTo(b, 4);
        for (int i = 0; i < pwm.Length; i++) BitConverter.GetBytes(pwm[i]).CopyTo(b, 8 + 2 * i);
        return b;
    }

    static ushort[] Pwm(int channels, ushort motors, ushort rest = 0)
    {
        var p = new ushort[channels];
        for (int i = 0; i < channels; i++) p[i] = i < 4 ? motors : rest;
        return p;
    }

    static void ServoFrameDecode()
    {
        var pwm = Pwm(16, 1500, 1200);
        pwm[9] = 1700;
        var b = ServoBytes(4242, 400, pwm);
        Check(SitlJson.TryDecode(b, b.Length, out var f), "16-channel frame decodes");
        Check(f.FrameRate == 400 && f.FrameCount == 4242 && f.Pwm.Length == 16 && f.Pwm[0] == 1500 && f.Pwm[9] == 1700, "16-channel fields");
        var b32 = ServoBytes(7, 1000, Pwm(32, 1100), SitlJson.Magic32);
        Check(SitlJson.TryDecode(b32, b32.Length, out var f32) && f32.Pwm.Length == 32 && f32.FrameRate == 1000, "32-channel frame decodes");
        Check(!SitlJson.TryDecode(b, b.Length - 2, out _), "short frame rejected");
        Check(!SitlJson.TryDecode(b32, b.Length, out _), "32-channel magic with 16-channel length rejected");
        var bad = (byte[])b.Clone(); bad[0] ^= 1;
        Check(!SitlJson.TryDecode(bad, bad.Length, out _), "wrong magic rejected");
        var zeroRate = ServoBytes(1, 0, pwm);
        Check(!SitlJson.TryDecode(zeroRate, zeroRate.Length, out _), "zero frame rate rejected");
        Near(SitlJson.Throttle(1000), 0, "pwm 1000 = 0"); Near(SitlJson.Throttle(1500), 0.5, "pwm 1500 = 0.5");
        Near(SitlJson.Throttle(2100), 1, "pwm over 2000 clamps"); Near(SitlJson.Throttle(0), 0, "pwm 0 clamps");
    }

    static void GimbalConfig()
    {
        var d = new ModConfig().Gimbal;
        Check(d.Bind == "127.0.0.1" && d.Port == 5613 && d.Target == "127.0.0.1:5614" && d.RateHz == 50 && d.SlewDegS == 90,
            "gimbal defaults: setpoints on 127.0.0.1:5613, status to 127.0.0.1:5614, 50 Hz, 90 deg/s");
        var c = ModConfig.Parse("[gimbal]\nbind=0.0.0.0\nport=6000\ntarget=10.0.0.2:6001\nrate_hz=20\nslew_deg_s=180\n");
        Check(c.Warnings.Count == 0 && c.Gimbal.Bind == "0.0.0.0" && c.Gimbal.Port == 6000 && c.Gimbal.Target == "10.0.0.2:6001"
            && c.Gimbal.RateHz == 20 && c.Gimbal.SlewDegS == 180, "gimbal settings parsed");
        var bad = ModConfig.Parse("[gimbal]\nbind=localhost\nport=0\ntarget=127.0.0.1\nrate_hz=5\nslew_deg_s=0\n");
        Check(bad.Warnings.Count == 5 && bad.Gimbal.Port == 5613 && bad.Gimbal.RateHz == 50 && bad.Gimbal.SlewDegS == 90 && bad.Gimbal.Target == "127.0.0.1:5614",
            "bad gimbal settings warn and keep the defaults: " + string.Join(" | ", bad.Warnings));
    }

    static void GimbalDatagrams()
    {
        var b = new byte[GimbalProtocol.SetpointSize];
        GimbalProtocol.EncodeSetpoint(b, 9, -30.5f, 60.25f);
        Check(b[0] == 'H' && b[1] == 'G' && b[2] == 'I' && b[3] == 'M' && BitConverter.ToUInt32(b, 4) == 1 && BitConverter.ToUInt32(b, 8) == 9, "setpoint header: HGIM, version 1, seq");
        Check(GimbalProtocol.TryDecodeSetpoint(b, b.Length, out uint seq, out float pitch, out float yaw) && seq == 9 && pitch == -30.5f && yaw == 60.25f, "setpoint round trip");
        Check(!GimbalProtocol.TryDecodeSetpoint(b, b.Length - 1, out _, out _, out _), "short setpoint rejected");
        var big = new byte[GimbalProtocol.SetpointSize + 4]; Array.Copy(b, big, b.Length);
        Check(!GimbalProtocol.TryDecodeSetpoint(big, big.Length, out _, out _, out _), "long setpoint rejected");
        var magic = (byte[])b.Clone(); magic[3] = (byte)'X';
        Check(!GimbalProtocol.TryDecodeSetpoint(magic, magic.Length, out _, out _, out _), "wrong magic rejected");
        var version = (byte[])b.Clone(); version[4] = 2;
        Check(!GimbalProtocol.TryDecodeSetpoint(version, version.Length, out _, out _, out _), "wrong version rejected");
        GimbalProtocol.EncodeSetpoint(b, 10, float.NaN, 0);
        Check(!GimbalProtocol.TryDecodeSetpoint(b, b.Length, out _, out _, out _), "NaN pitch rejected");
        GimbalProtocol.EncodeSetpoint(b, 11, 0, float.PositiveInfinity);
        Check(!GimbalProtocol.TryDecodeSetpoint(b, b.Length, out _, out _, out _), "infinite yaw rejected");

        var s = new byte[GimbalProtocol.StatusSize];
        GimbalProtocol.EncodeStatus(s, 3, 1234, 0.5f, -45f, 30f, 2f, -3f);
        Check(s[0] == 'H' && s[1] == 'G' && s[2] == 'A' && s[3] == 'T' && BitConverter.ToUInt32(s, 4) == 1, "status header: HGAT, version 1");
        Check(BitConverter.ToUInt32(s, 8) == 3 && BitConverter.ToUInt32(s, 12) == 1234, "status seq and time_ms");
        Check(BitConverter.ToSingle(s, 16) == 0.5f && BitConverter.ToSingle(s, 20) == -45f && BitConverter.ToSingle(s, 24) == 30f
            && BitConverter.ToSingle(s, 28) == 2f && BitConverter.ToSingle(s, 32) == -3f, "status floats: camera roll, pitch, yaw, vehicle roll, pitch");
    }

    /// <summary>Body leaning by <paramref name="rollDeg"/> (right side down) and <paramref name="noseDeg"/> (nose up), at NED yaw <paramref name="yawDeg"/>.</summary>
    static QuadBody Leaning(NedFrame ned, double yawDeg, double rollDeg, double noseDeg)
    {
        var body = new QuadBody(new Airframe()) { FloorZ = -1000 };
        body.Place(new Vec3(200, -930, 45), ned.GtaHeadingRad(yawDeg * Math.PI / 180));
        double roll = rollDeg * Math.PI / 180, nose = noseDeg * Math.PI / 180;
        body.F = Vec3.Rotate(body.F, body.R, nose); body.U = Vec3.Rotate(body.U, body.R, nose);
        body.R = Vec3.Rotate(body.R, body.F, roll); body.U = Vec3.Rotate(body.U, body.F, roll);
        return body;
    }

    static void GimbalHoldsTheSetpoint()
    {
        var ned = new NedFrame { Origin = new Vec3(195, -933, 30), HeadingDeg = 30 };
        var g = new GimbalMount();
        var level = Leaning(ned, 70, 0, 0);
        g.Reset(level, ned);
        Check(!g.HasSetpoint && g.TargetPitchDeg == -45 && g.TargetYawDeg == 0, "at rest: no setpoint, pitch -45, yaw 0");
        g.Measure(level, ned, out double cr, out double cp, out double cy, out _, out _);
        Near(cp, -45, "resting camera 45 deg down", 1e-6); Near(cy, 0, "resting camera faces the nose", 1e-6); Near(cr, 0, "resting camera level", 1e-6);

        // A new setpoint is approached at 90 deg/s per axis: 0.1 s moves 9 deg.
        g.SetTarget(-10, 60);
        for (int i = 0; i < 40; i++) g.Step(0.0025, level, ned);
        Near(g.PitchDeg, -36, "pitch slews 9 deg in 0.1 s", 1e-9); Near(g.YawDeg, 9, "yaw slews 9 deg in 0.1 s", 1e-9);
        for (int i = 0; i < 400; i++) g.Step(0.0025, level, ned);
        g.Measure(level, ned, out cr, out cp, out cy, out _, out _);
        Near(cp, -10, "settled at pitch -10", 1e-6); Near(cy, 60, "settled 60 deg right of the nose", 1e-6); Near(cr, 0, "still level", 1e-6);
        var pose = new PhysicsLink(level, ned, g, level.P, ned.GtaHeadingRad(70 * Math.PI / 180));
        g.SetTarget(-10, 60);
        for (int i = 0; i < 400; i++) g.Step(0.0025, level, ned);
        Near(pose.CameraPose(30, 84).YawDeg, 130, "NED camera yaw = body yaw 70 + pan 60", 1e-3);

        // Leaning 15 deg right and 10 deg nose up: the motors hold the horizon pitch, level roll and nose-relative yaw.
        var lean = Leaning(ned, 70, 15, 10);
        g.SetTarget(-30, -40);
        for (int i = 0; i < 1000; i++) g.Step(0.0025, lean, ned);
        g.Measure(lean, ned, out cr, out cp, out cy, out double vr, out double vp);
        Near(cp, -30, "horizon pitch held against the lean", 1e-6); Near(cr, 0, "roll held level against the lean", 1e-6);
        Near(cy, -40, "yaw relative to the nose held against the lean", 1e-6);
        Check(Math.Abs(vr - 15) < 0.5 && Math.Abs(vp - 10) < 0.5, $"vehicle lean reported (roll {vr:F2}, pitch {vp:F2})");
        Check(Math.Abs(g.RollDeg) > 5 && Math.Abs(g.TiltDeg + 30) > 5, $"the motors moved against the lean (roll {g.RollDeg:F1}, tilt {g.TiltDeg:F1})");

        // Straight down keeps the body's yaw in NED.
        g.SetTarget(-90, 0);
        for (int i = 0; i < 1000; i++) g.Step(0.0025, level, ned);
        var down = pose.CameraPose(30, 84);
        Near(down.PitchDeg, -90, "nadir pitch", 1e-3); Near(down.YawDeg, 70, "nadir camera keeps the body's yaw", 1e-3);

        // Setpoints are clamped to the setpoint range; non-finite ones are ignored.
        g.SetTarget(-120, 200);
        Check(g.TargetPitchDeg == GimbalMount.PitchMinDeg && g.TargetYawDeg == GimbalMount.YawLimitDeg, "setpoint clamped to pitch -90, yaw 160");
        g.SetTarget(double.NaN, 0);
        Check(g.TargetPitchDeg == GimbalMount.PitchMinDeg, "NaN setpoint ignored");

        // Motor travel: looking 45 deg up with the nose 30 deg down needs tilt +75, beyond +45.
        var noseDown = Leaning(ned, 0, 0, -30);
        g.SetTarget(45, 0);
        for (int i = 0; i < 2000; i++) g.Step(0.0025, noseDown, ned);
        g.Measure(noseDown, ned, out _, out cp, out _, out _, out _);
        Near(g.TiltDeg, GimbalMount.TiltMaxDeg, "tilt motor stops at +45", 1e-9);
        Near(cp, 15, "the measured camera pitch shows the stop", 1e-6);

        g.Reset(level, ned);
        Check(!g.HasSetpoint && g.PitchDeg == -45 && g.YawDeg == 0 && Math.Abs(g.TiltDeg + 45) < 1e-9, "reset: back to rest at once");
        Check(new GimbalMount(10, 20).SlewDegS == 10, "slew from the settings");
        foreach (double bad in new[] { 0.5, 1001, double.NaN })
        {
            bool threw = false;
            try { new GimbalMount(bad); } catch (ArgumentOutOfRangeException) { threw = true; }
            Check(threw, $"slew {bad} refused");
        }
    }

    static void GimbalReports()
    {
        // Holding still: only the heartbeat, every 1/rate_hz of sim time.
        var ned = new NedFrame { Origin = new Vec3(0, 0, 0) };
        var body = Leaning(ned, 0, 0, 0);
        var g = new GimbalMount(rateHz: 50);
        g.Reset(body, ned);
        var at = new System.Collections.Generic.List<double>();
        for (int i = 1; i <= 400; i++)
        {
            double t = i / 400.0;
            g.Step(1 / 400.0, body, ned);
            if (g.Report(t, body, ned)) at.Add(t);
        }
        Check(at.Count == 50, $"still: a 50 Hz heartbeat, 50 reports per sim second ({at.Count})");
        bool gaps = true;
        for (int i = 1; i < at.Count; i++) gaps &= Math.Abs(at[i] - at[i - 1] - 0.02) < 1e-9;
        Check(gaps, "still: heartbeats exactly 20 ms apart");
        Check(BitConverter.ToUInt32(g.Datagram, 8) == 50 && BitConverter.ToUInt32(g.Datagram, 12) == RangeProtocol.TimeMs(at[at.Count - 1]), "last report: seq 50, time_ms its sim time");
        Near(BitConverter.ToSingle(g.Datagram, 20), -45, "reported camera pitch", 1e-5);
        Check(g.Report(0.1, body, ned), "the sim clock went back: report at once");
        Check(!g.Report(0.11, body, ned) && g.Report(0.12, body, ned), "then one period later");
        var slow = new GimbalMount(rateHz: GimbalMount.MinRateHz);
        slow.Reset(body, ned);
        int beats = 0;
        for (int i = 1; i <= 400; i++) if (slow.Report(i / 400.0, body, ned)) beats++;
        Check(beats == 20, $"the slowest heartbeat is 20 Hz ({beats})");
        bool refused = false;
        try { new GimbalMount(rateHz: 19); } catch (ArgumentOutOfRangeException) { refused = true; }
        Check(refused, "a heartbeat under 20 Hz is refused");
    }

    static void GimbalReportsEveryChange()
    {
        var ned = new NedFrame { Origin = new Vec3(0, 0, 0) };
        const double h = 1 / 400.0;

        // The vehicle rolls 0.05 deg per 400 Hz step: the camera stays level, the reported vehicle
        // roll changes, so every step reports, with consecutive seqs and its own sim time.
        var body = Leaning(ned, 0, 0, 0);
        var g = new GimbalMount();
        g.Reset(body, ned);
        double d = 0.05 * Math.PI / 180;
        int reports = 0; bool consecutive = true; double t = 0;
        for (int i = 0; i < 40; i++)
        {
            body.R = Vec3.Rotate(body.R, body.F, d); body.U = Vec3.Rotate(body.U, body.F, d);
            t += h;
            g.Step(h, body, ned);
            if (!g.Report(t, body, ned)) continue;
            reports++;
            consecutive &= BitConverter.ToUInt32(g.Datagram, 8) == g.Reports && BitConverter.ToUInt32(g.Datagram, 12) == RangeProtocol.TimeMs(t);
        }
        Check(reports == 40 && consecutive, $"a changing lean reports on each of 40 consecutive steps ({reports})");
        Near(BitConverter.ToSingle(g.Datagram, 28), 2.0, "the last report carries the 2 deg roll", 1e-4);
        Near(BitConverter.ToSingle(g.Datagram, 16), 0, "while the camera stays level", 1e-4);

        // No deadband: a lean changing 0.005 deg per 400 Hz step, well under any earlier threshold,
        // still reports on each step, and each report carries that step's roll.
        var fine = Leaning(ned, 0, 0, 0);
        var f = new GimbalMount();
        f.Reset(fine, ned);
        double small = 0.005 * Math.PI / 180;
        reports = 0; bool tracks = true; t = 0;
        for (int i = 1; i <= 40; i++)
        {
            fine.R = Vec3.Rotate(fine.R, fine.F, small); fine.U = Vec3.Rotate(fine.U, fine.F, small);
            t += h;
            f.Step(h, fine, ned);
            if (!f.Report(t, fine, ned)) continue;
            reports++;
            tracks &= Math.Abs(BitConverter.ToSingle(f.Datagram, 28) - 0.005 * i) < 1e-4;
        }
        Check(reports == 40 && tracks, $"a 0.005 deg/step lean reports on each of 40 consecutive steps with its roll ({reports})");
        // Then still: the same float32 angles again send nothing until the heartbeat.
        Check(!f.Report(t + h, fine, ned) && !f.Report(t + 2 * h, fine, ned), "unchanged angles: no report before the heartbeat");

        // A slewing setpoint on a level body: every step's camera angle differs, so every step reports.
        var level = Leaning(ned, 0, 0, 0);
        var s = new GimbalMount();
        s.Reset(level, ned);
        s.SetTarget(-10, 60);
        reports = 0;
        for (int i = 1; i <= 40; i++)
        {
            s.Step(h, level, ned);
            if (s.Report(1 + i * h, level, ned)) reports++;
        }
        Check(reports == 40, $"a slewing camera reports on each of 40 consecutive steps ({reports})");
        Near(BitConverter.ToSingle(s.Datagram, 24), 9, "reported yaw after 0.1 s of slew", 1e-4);

        // Through the physics link, as the mod and horus-gta-physics-sim run it: a report from
        // Advanced on every changing step, then only the heartbeat once the gimbal settles.
        var frame = new Airframe();
        var quad = new QuadBody(frame) { FloorZ = 0 };
        var origin = new NedFrame { Origin = new Vec3(0, 0, frame.RadiusM + 0.001) };
        var link = new PhysicsLink(quad, origin, new GimbalMount(), origin.Origin, 0);
        int sent = 0;
        link.Advanced = () => { if (link.Gimbal.Report(link.SimTimeS, link.Body, link.Frame)) sent++; };
        uint n = 0;
        PumpResult Run(int frames)
        {
            var q = new System.Collections.Generic.Queue<ServoFrame?>();
            for (int i = 0; i < frames; i++) q.Enqueue(new ServoFrame { FrameRate = 400, FrameCount = ++n, Pwm = Pwm(16, 1000) });
            return link.Pump(link.SimTimeS + 10, () => q.Count > 0 ? q.Dequeue() : null, _ => { }, () => false);
        }
        Run(400);   // settle on the floor at rest
        link.Gimbal.SetTarget(0, 0);
        sent = 0;
        var r = Run(40);
        Check(r.Steps == 40 && sent == 40, $"physics link: 40 slewing steps, 40 reports ({sent})");
        Run(400);   // the 45 deg slew ends after 0.5 s
        sent = 0;
        Run(400);
        Check(sent == 50, $"physics link: settled, a 50 Hz heartbeat over 1 s ({sent})");
    }

    static void GimbalChannelRoundTrip()
    {
        using var horus = new System.Net.Sockets.Socket(System.Net.Sockets.AddressFamily.InterNetwork, System.Net.Sockets.SocketType.Dgram, System.Net.Sockets.ProtocolType.Udp);
        horus.Bind(new System.Net.IPEndPoint(System.Net.IPAddress.Loopback, 0));
        horus.ReceiveTimeout = 2000;
        int horusPort = ((System.Net.IPEndPoint)horus.LocalEndPoint).Port;
        var settings = ModConfig.Parse($"[gimbal]\ntarget=127.0.0.1:{horusPort}\n").Gimbal;
        settings.Port = 0;   // any free port
        using var channel = new GimbalChannel(settings);
        var to = new System.Net.IPEndPoint(System.Net.IPAddress.Loopback, channel.LocalPort);
        var b = new byte[GimbalProtocol.SetpointSize];
        GimbalProtocol.EncodeSetpoint(b, 1, -20, 10);
        horus.SendTo(b, to);
        GimbalProtocol.EncodeSetpoint(b, 2, -25, 15);
        horus.SendTo(b, to);
        horus.SendTo(new byte[] { 1, 2, 3 }, to);
        var g = new GimbalMount();
        int taken = 0;
        for (int i = 0; i < 200 && taken < 2; i++) { taken += channel.Drain(g); System.Threading.Thread.Sleep(5); }
        for (int i = 0; i < 50 && channel.Rejected == 0; i++) { channel.Drain(g); System.Threading.Thread.Sleep(5); }
        Check(taken == 2 && g.TargetPitchDeg == -25 && g.TargetYawDeg == 15, $"two setpoints taken, the last wins ({taken}: {g.TargetPitchDeg}, {g.TargetYawDeg})");
        Check(channel.Received == 2 && channel.Rejected == 1, $"the 3-byte datagram rejected ({channel.Received} taken, {channel.Rejected} rejected)");
        var ned = new NedFrame { Origin = new Vec3(0, 0, 0) };
        var body = Leaning(ned, 0, 0, 0);
        g.Report(0.5, body, ned);
        channel.Send(g);
        var got = new byte[64];
        int n = horus.Receive(got);
        Check(n == GimbalProtocol.StatusSize && got[3] == 'T' && BitConverter.ToUInt32(got, 12) == 500, $"status datagram reaches the target ({n} bytes)");
    }

    static double[] All(double t) => new[] { t, t, t, t };

    static void QuadHover()
    {
        var frame = new Airframe();
        double hover = frame.HoverThrottle();
        Check(hover > 0.3 && hover < 0.7, $"default airframe hovers mid-range, got {hover:F3}");
        var body = new QuadBody(frame) { FloorZ = -1000 };
        body.Place(new Vec3(0, 0, 50), 0);
        for (int i = 0; i < 4; i++) body.Rpm[i] = hover * frame.MaxRpm;   // motors already at hover speed
        for (int i = 0; i < 1200; i++) body.Step(0.0025, All(hover));
        Check(Math.Abs(body.V.Z) < 1e-3 && Math.Abs(body.P.Z - 50) < 1e-3, $"hover throttle holds height for 3 s: vz {body.V.Z:F5}, z {body.P.Z:F4}");
        Check(body.U.Z > 0.9999 && Math.Abs(body.W.Length) < 1e-6, "symmetric thrust keeps it level without rotation");
        Near(body.SpecificForce.Z, QuadBody.G, "accelerometer reads 1 g up in steady hover", 1e-3);
        for (int i = 0; i < 400; i++) body.Step(0.0025, All(Math.Min(1, hover * 1.2)));
        Check(body.V.Z > 0.5, $"20 % more throttle climbs: vz {body.V.Z:F3}");
    }

    static void QuadTorqueSigns()
    {
        var frame = new Airframe();
        double h = frame.HoverThrottle();
        var ned = new NedFrame { Origin = default, HeadingDeg = 0 };
        (double roll, double pitch, double yaw) After(double[] t)
        {
            var body = new QuadBody(frame) { FloorZ = -1000 };
            body.Place(new Vec3(0, 0, 50), 0);
            for (int i = 0; i < 40; i++) body.Step(0.0025, t);
            ned.Euler(body, out double r, out double p, out double y);
            return (r, p, y);
        }
        // Motors: 1 front right (CCW), 2 back left (CCW), 3 front left (CW), 4 back right (CW).
        var right = After(new[] { h + 0.1, h, h, h + 0.1 });
        Check(right.roll < -0.001, $"more thrust on the right rolls left (roll {right.roll:F4})");
        var front = After(new[] { h + 0.1, h, h + 0.1, h });
        Check(front.pitch > 0.001, $"more thrust at the front pitches nose up (pitch {front.pitch:F4})");
        var ccw = After(new[] { h + 0.1, h + 0.1, h, h });
        Check(ccw.yaw > 0.0005, $"more on the CCW props yaws right (yaw {ccw.yaw:F5})");
        Check(Math.Abs(ccw.roll) < 1e-6 && Math.Abs(ccw.pitch) < 1e-6, "diagonal pair makes no roll or pitch");
    }

    static void QuadRestsOnFloor()
    {
        var frame = new Airframe();
        var body = new QuadBody(frame) { FloorZ = 10 };
        body.Place(new Vec3(0, 0, 13), 0);
        for (int i = 0; i < 800; i++) body.Step(0.0025, All(0));
        Near(body.P.Z, 10 + frame.RadiusM, "falls to the floor and rests at its radius", 0.01);
        Check(body.InContact && body.V.Length < 1e-9, "at rest on the floor");
        Near(body.SpecificForce.Z, QuadBody.G, "accelerometer reads 1 g up at rest");
        var ned = new NedFrame { Origin = new Vec3(0, 0, 10 + frame.RadiusM) };
        var line = SitlJson.Encode(1.0, body, ned);
        var doc = JsonDocument.Parse(line.Trim()).RootElement;
        Near(doc.GetProperty("imu").GetProperty("accel_body")[2].GetDouble(), -QuadBody.G, "accel_body z = -g at rest (FRD)", 1e-6);
        Near(doc.GetProperty("position")[2].GetDouble(), 0, "resting centre is NED down 0", 0.01);
    }

    static void NedFrameMatchesPoseMapping()
    {
        var origin = new Origin { X = 195, Y = -933, HeadingDeg = 30 };
        var ned = new NedFrame { Origin = new Vec3(195, -933, 29.69), HeadingDeg = 30 };
        var p = new NedPose { NorthM = 12, EastM = -7, DownM = -4, HfovDeg = 84 };
        var cam = PoseMapping.Map(p, origin, 29.69, 1920, 1080);
        var back = ned.ToNed(new Vec3(cam.X, cam.Y, cam.Z) - ned.Origin);
        Near(back.X, 12, "north round-trips", 1e-9); Near(back.Y, -7, "east round-trips", 1e-9); Near(back.Z, -4, "down round-trips", 1e-9);
        // A body facing GTA heading (origin heading - yaw) has NED yaw = yaw.
        var body = new QuadBody(new Airframe());
        body.Place(default, ned.GtaHeadingRad(40 * Math.PI / 180));
        ned.Euler(body, out double r, out double pi, out double y);
        Near(y * 180 / Math.PI, 40, "yaw round-trips through the GTA heading", 1e-9);
        Near(r, 0, "level roll"); Near(pi, 0, "level pitch");
    }

    static void FdmJsonLine()
    {
        var body = new QuadBody(new Airframe()) { FloorZ = -1000 };
        body.Place(new Vec3(3, 4, 5), 0);
        body.V = new Vec3(1, 2, -3);                 // east 1, north 2, up -3 (falling)
        body.W = new Vec3(0.1, 0.2, 0.3);            // nose up, roll right, yaw left
        var ned = new NedFrame { Origin = new Vec3(0, 0, 1) };
        string line = SitlJson.Encode(12.5, body, ned);
        Check(line.StartsWith("\n{") && line.EndsWith("}\n"), "framed by newlines");
        var d = JsonDocument.Parse(line.Trim()).RootElement;
        Near(d.GetProperty("timestamp").GetDouble(), 12.5, "timestamp");
        var pos = d.GetProperty("position"); var vel = d.GetProperty("velocity"); var gyro = d.GetProperty("imu").GetProperty("gyro");
        Near(pos[0].GetDouble(), 4, "north = world Y"); Near(pos[1].GetDouble(), 3, "east = world X"); Near(pos[2].GetDouble(), -4, "down = -(Z - origin)");
        Near(vel[0].GetDouble(), 2, "vn"); Near(vel[1].GetDouble(), 1, "ve"); Near(vel[2].GetDouble(), 3, "vd");
        Near(gyro[0].GetDouble(), 0.2, "p = roll right rate"); Near(gyro[1].GetDouble(), 0.1, "q = nose-up rate"); Near(gyro[2].GetDouble(), -0.3, "r = -yaw-left rate");
        Check(d.TryGetProperty("attitude", out var att) && att.GetArrayLength() == 3, "euler attitude present");
    }

    static void PhysicsLinkLockstep()
    {
        var frame = new Airframe();
        var body = new QuadBody(frame) { FloorZ = 0 };
        var ned = new NedFrame { Origin = new Vec3(0, 0, frame.RadiusM + 0.001) };
        var link = new PhysicsLink(body, ned, new GimbalMount(), ned.Origin, 0);
        var queue = new System.Collections.Generic.Queue<ServoFrame?>();
        var replies = new System.Collections.Generic.List<string>();
        ServoFrame Fr(uint n) => new ServoFrame { FrameRate = 400, FrameCount = n, Pwm = Pwm(16, 1000) };
        ServoFrame? Next() => queue.Count > 0 ? queue.Dequeue() : null;
        void Reply(string s) => replies.Add(s);

        foreach (uint n in new uint[] { 1, 2, 2, 3, 4, 5 }) queue.Enqueue(Fr(n));
        var r = link.Pump(2 / 400.0 + 1e-9, Next, Reply, () => false);
        Check(r.Steps == 3 && r.Resent == 1, $"frames 1,2 stepped, 2 again re-answered, 3 stepped (steps {r.Steps}, resent {r.Resent})");
        Check(replies.Count == 4 && replies[2] == replies[1], "the repeated frame gets the same answer");
        Check(link.LastFrame == 3, "sim stops at the target: frame 4 held");
        Near(link.SimTimeS, 3 / 400.0, "three steps of 1/400 s");
        Near(link.Gimbal.TiltDeg, -45, "no setpoint yet: the gimbal rests 45 deg down", 1e-9);
        link.Gimbal.SetTarget(0, 0);

        r = link.Pump(1.0, Next, Reply, () => false);
        Check(r.Steps == 2 && r.TimedOut && link.LastFrame == 5, "held frame 4 answered first, then 5, then timeout");
        Near(link.Gimbal.PitchDeg, -45 + 2 * 90 / 400.0, "each physics step slews the gimbal by 90 deg/s", 1e-9);

        queue.Enqueue(Fr(1));
        r = link.Pump(1.0, Next, Reply, () => false);
        Check(r.Restarts == 1 && r.Steps == 1 && link.LastFrame == 1, "lower frame count = SITL restarted");
        Near(link.SimTimeS, 1 / 400.0, "restart resets the clock");

        queue.Enqueue(Fr(2)); queue.Enqueue(Fr(3));
        r = link.Pump(1.0, Next, Reply, () => true);
        Check(r.Steps == 0 && r.BudgetHit && queue.Count == 2, "an exhausted budget answers nothing");
    }

    // World contact. One-sided planes (a point on each and its outward normal): a ray hits a plane
    // only crossing it from the open side into the solid, as GTA's rays pass out of terrain from
    // underneath without a hit.
    static Contact? PlanesRay(Vec3 a, Vec3 b, (Vec3 at, Vec3 normal)[] planes)
    {
        double bestT = double.MaxValue; Contact? best = null;
        foreach (var (at, normal) in planes)
        {
            double ta = Vec3.Dot(a - at, normal), tb = Vec3.Dot(b - at, normal);
            if (ta < 0 || tb >= 0) continue;
            double t = ta / (ta - tb);
            if (t >= bestT) continue;
            bestT = t;
            best = new Contact { Position = a + (b - a) * t, Normal = normal, Entity = 0 };
        }
        return best;
    }

    // A fake world: ground plane z = 0 and a wall plane x = 10 facing -X.
    static readonly (Vec3, Vec3)[] FloorAndWall = { (new Vec3(0, 0, 0), new Vec3(0, 0, 1)), (new Vec3(10, 0, 0), new Vec3(-1, 0, 0)) };

    static Contact? FakeRay(Vec3 a, Vec3 b, System.Collections.Generic.List<(Vec3, Vec3)> log)
    {
        log?.Add((a, b));
        return PlanesRay(a, b, FloorAndWall);
    }

    static void WorldProbeRays()
    {
        var rays = new System.Collections.Generic.List<(Vec3, Vec3)>();
        var probe = new WorldProbe((a, b) => FakeRay(a, b, rays));
        var hit = probe.Sweep(new Vec3(9.5, 0, 5), new Vec3(9.9, 0, 5), 0.122);
        Check(hit.HasValue && Math.Abs(hit.Value.Position.X - 10) < 1e-9 && hit.Value.Normal.X == -1 && hit.Value.OnPath, "motion ray extended by the reach finds the wall 0.1 m past the step, on the path");
        Check(rays.Count == 1 && Math.Abs(rays[0].Item2.X - 10.022) < 1e-9, "a hit along the motion needs no further ray");

        rays.Clear();
        probe = new WorldProbe((a, b) => FakeRay(a, b, rays));
        var p = new Vec3(0, 0, 5);
        Check(!probe.Sweep(p, p + new Vec3(0.004, 0, 0), 0.122).HasValue && rays.Count == 2 && rays[1].Item2.Z == 5 - WorldProbe.GroundProbeDepthM,
            "first step: motion ray and the long ground probe");
        rays.Clear();
        for (int i = 1; i < WorldProbe.ReprobeSteps; i++) { var q = p + new Vec3(0.004 * i, 0, 0); probe.Sweep(q, q + new Vec3(0.004, 0, 0), 0.122); }
        Check(rays.Count == WorldProbe.ReprobeSteps - 1, $"5 m above the ground only the motion ray is cast ({rays.Count} rays in {WorldProbe.ReprobeSteps - 1} steps)");
        rays.Clear();
        var r = p + new Vec3(0.4, 0, 0);
        probe.Sweep(r, r + new Vec3(0.004, 0, 0), 0.122);
        Check(rays.Count == 2, "after the reprobe interval the ground is probed again");

        rays.Clear();
        probe = new WorldProbe((a, b) => FakeRay(a, b, rays));
        var low = new Vec3(0, 0, 0.5);
        probe.Sweep(low, low, 0.122);
        rays.Clear();
        Check(!probe.Sweep(low, low, 0.122).HasValue && rays.Count == 1 && rays[0].Item2.Z == 0.5 - 2 * 0.122, "near the ground: the short down ray (two reaches) every step");
        var rest = new Vec3(0, 0, 0.121);
        var under = probe.Sweep(rest, rest, 0.122);
        Check(under.HasValue && !under.Value.OnPath, "resting: the down ray finds the ground, off the path");
        var slopeProbe = new WorldProbe((a, b) => PlanesRay(a, b, new[] { (new Vec3(0, 0, 0), new Vec3(-0.5, 0, Math.Sqrt(0.75))) }));
        var touching = new Vec3(0, 0, 0.122 / Math.Sqrt(0.75));
        Check(slopeProbe.Sweep(touching, touching, 0.122).HasValue, "a sphere touching a 30 deg slope (0.141 m above it) finds it with the down ray");
    }

    static void QuadStopsAtWall()
    {
        var frame = new Airframe();
        var events = new System.Collections.Generic.List<(Contact c, double speed)>();
        var body = new QuadBody(frame)
        {
            Sweep = new WorldProbe((x, y) => FakeRay(x, y, null)).Sweep,
            Contacted = (c, s) => events.Add((c, s)),
        };
        double hover = frame.HoverThrottle();
        body.Place(new Vec3(5, 0, 5), 0);
        for (int i = 0; i < 4; i++) body.Rpm[i] = hover * frame.MaxRpm;
        body.V = new Vec3(4, 0, 0);   // flying east at 4 m/s, 5 m from the wall
        double peak = 0;
        for (int i = 0; i < 2400; i++)
        {
            body.Step(1 / 800.0, All(hover));
            peak = Math.Max(peak, body.SpecificForce.Length);
        }
        Check(body.P.X <= 10 - frame.RadiusM && body.P.X > 10 - frame.RadiusM - 0.01, $"stopped one radius short of the wall: x {body.P.X:F4}");
        Check(events.Count > 0 && events[0].c.Normal.X == -1 && events[0].speed > 2, $"first contact is the wall at the approach speed ({(events.Count > 0 ? events[0].speed : 0):F2} m/s)");
        Check(peak > SitlJson.AccelRangeMs2, $"the impact exceeds the accelerometer range ({peak / QuadBody.G:F0} g)");
        Check(Math.Abs(body.P.Z - 5) < 0.5, "still flying at about its height after the hit");

        // Landing: falls onto the ground, then rests in contact without hopping.
        var lander = new QuadBody(frame) { Sweep = new WorldProbe((x, y) => FakeRay(x, y, null)).Sweep };
        lander.Place(new Vec3(0, 0, 2), 0);
        int inContact = 0;
        for (int i = 0; i < 2400; i++) { lander.Step(1 / 800.0, All(0)); if (i >= 2000 && lander.InContact) inContact++; }
        Near(lander.P.Z, frame.RadiusM + 0.001, "rests one radius above the ground", 1e-6);
        Check(inContact == 400, $"in contact on every step while resting ({inContact} of 400)");
    }

    // The 2026-11-02 fall through a Vinewood hillside, offline: a 30 deg slope falling toward +X into
    // a vertical rock face at x = 10 (an acute concave corner, like the hill and its rock).
    static void QuadHoldsOnSlopes()
    {
        var frame = new Airframe();
        double a = 30 * Math.PI / 180;
        var slope = (at: new Vec3(10, 0, 0), normal: new Vec3(Math.Sin(a), 0, Math.Cos(a)));
        var wall = (at: new Vec3(10, 0, 0), normal: new Vec3(-1, 0, 0));
        var planes = new[] { slope, wall };
        double Gap((Vec3 at, Vec3 normal) plane, Vec3 p) => Vec3.Dot(p - plane.at, plane.normal);
        double SlopeZ(double x) => (10 - x) * Math.Tan(a);

        // Landed on the slope 5 m from the rock with the motors off: it stays where it touched down
        // (and so never reaches the corner).
        var lander = new QuadBody(frame) { Sweep = new WorldProbe((x, y) => PlanesRay(x, y, planes)).Sweep };
        lander.Place(new Vec3(5, 0, SlopeZ(5) + 0.5), 0);
        Vec3? landed = null;
        double lowest = double.MaxValue;
        for (int i = 0; i < 8000; i++)
        {
            lander.Step(1 / 800.0, All(0));
            if (landed == null && lander.InContact) landed = lander.P;
            lowest = Math.Min(lowest, Math.Min(Gap(slope, lander.P), Gap(wall, lander.P)));
        }
        Check(landed.HasValue, "lands on the slope");
        double moved = landed.HasValue ? (lander.P - landed.Value).Length : double.NaN;
        Check(moved < 0.01, $"rests where it landed on a 30 deg slope (moved {moved:F3} m in 10 s, to x {lander.P.X:F2})");
        Check(lander.InContact && Gap(slope, lander.P) > frame.RadiusM - 0.01, $"resting on the slope one radius off it (gap {Gap(slope, lander.P):F4} m)");
        Check(lowest >= 0, $"landed: never behind the slope or the rock (closest {lowest:F3} m; ends at x {lander.P.X:F3}, z {lander.P.Z:F3}, slope there {SlopeZ(lander.P.X):F3})");

        // Driven down the slope into the rock (tilted 25 deg toward it, sinking): the centre never
        // passes behind either surface, and the quad ends up wedged in the corner.
        var pushed = new QuadBody(frame) { Sweep = new WorldProbe((x, y) => PlanesRay(x, y, planes)).Sweep };
        pushed.Place(new Vec3(8, 0, SlopeZ(8) + 0.3), 0);
        double tilt = 25 * Math.PI / 180;
        pushed.U = new Vec3(Math.Sin(tilt), 0, Math.Cos(tilt));
        pushed.R = Vec3.Cross(pushed.F, pushed.U);
        double hover = frame.HoverThrottle();
        for (int i = 0; i < 4; i++) pushed.Rpm[i] = hover * frame.MaxRpm;
        double worst = double.MaxValue;
        for (int i = 0; i < 8000; i++)
        {
            // Pressed against the rock for 5 s, then the motors stop and it drops into the crease.
            pushed.Step(1 / 800.0, All(i < 4000 ? hover : 0));
            worst = Math.Min(worst, Math.Min(Gap(slope, pushed.P), Gap(wall, pushed.P)));
        }
        Check(worst >= 0, $"never behind the slope or the rock (closest {worst:F3} m; ends at x {pushed.P.X:F3}, z {pushed.P.Z:F3}, slope there {SlopeZ(pushed.P.X):F3})");
        Check(Gap(wall, pushed.P) < 0.2 && Gap(slope, pushed.P) < 0.2, $"wedged in the corner (gaps rock {Gap(wall, pushed.P):F3}, slope {Gap(slope, pushed.P):F3} m)");
    }

    static void ImuSaturates()
    {
        var body = new QuadBody(new Airframe()) { FloorZ = -1000 };
        body.Place(new Vec3(0, 0, 5), 0);
        body.SpecificForce = new Vec3(0, -500, 9.8);     // 51 g backwards (nose is +Y)
        body.W = new Vec3(0, 50, 0);                     // 2865 deg/s roll
        var d = JsonDocument.Parse(SitlJson.Encode(1, body, new NedFrame()).Trim()).RootElement;
        var acc = d.GetProperty("imu").GetProperty("accel_body"); var gyro = d.GetProperty("imu").GetProperty("gyro");
        Near(acc[0].GetDouble(), -SitlJson.AccelRangeMs2, "x accel clips at -16 g", 1e-6);
        Near(acc[2].GetDouble(), -9.8, "z accel within range is unchanged", 1e-6);
        Near(gyro[0].GetDouble(), SitlJson.GyroRangeRadS, "roll rate clips at 2000 deg/s", 1e-6);
    }

    static void CameraOnTheMount()
    {
        const double groundZ = 29.69;
        var frame = new Airframe();
        var body = new QuadBody(frame) { FloorZ = groundZ };
        var origin = new Origin { X = 195, Y = -933, HeadingDeg = 30 };
        var ned = new NedFrame { Origin = new Vec3(195, -933, groundZ + frame.RadiusM + 0.001), HeadingDeg = 30 };
        var link = new PhysicsLink(body, ned, new GimbalMount(), ned.Origin, ned.GtaHeadingRad(0));

        // Resting: the camera at ArduPilot's down 0 above the ground origin would be in the ground (white frame).
        var cam = PoseMapping.Map(link.CameraPose(groundZ, 84), origin, groundZ, 1920, 1080);
        Near(cam.Z, groundZ + frame.RadiusM + 0.001 + frame.CameraMountM.Z, "resting camera at the mount, above the ground", 1e-6);
        var mount = body.ToWorld(frame.CameraMountM);
        Near(cam.X, mount.X, "resting camera x = mount", 1e-6); Near(cam.Y, mount.Y, "resting camera y = mount", 1e-6);

        // Turned, rolled 15 deg right and pitched 10 deg up in the air: the mount moves with the
        // body, and the gimbal's motors turn against the lean, so the resting camera still looks
        // 45 deg down, level, toward the body's heading.
        body.Place(new Vec3(200, -930, 45), ned.GtaHeadingRad(70 * Math.PI / 180));
        double roll = 15 * Math.PI / 180, nose = 10 * Math.PI / 180;
        body.R = Vec3.Rotate(body.R, body.F, roll); body.U = Vec3.Rotate(body.U, body.F, roll);
        body.F = Vec3.Rotate(body.F, body.R, nose); body.U = Vec3.Rotate(body.U, body.R, nose);
        link.Gimbal.Step(0.0025, body, ned);
        cam = PoseMapping.Map(link.CameraPose(groundZ, 84), origin, groundZ, 1920, 1080);
        mount = body.ToWorld(frame.CameraMountM);
        Near(cam.X, mount.X, "tilted camera x = mount", 1e-6); Near(cam.Y, mount.Y, "tilted camera y = mount", 1e-6);
        Near(cam.Z, mount.Z, "tilted camera z = mount", 1e-6);
        Check(Math.Abs(mount.Z - (45 + frame.CameraMountM.Z)) > 0.005, "the tilt moves the mount off its level height");
        GtaCameraAxes(cam, out var cr, out var cf, out _);
        Near(cr.Z, 0, "held against the lean: right axis level", 1e-5);
        Near(Math.Asin(cf.Z) * 180 / Math.PI, -45, "camera looks 45 deg down", 1e-4);
        double bodyHeading = Math.Atan2(-body.F.X, body.F.Y), camHeading = Math.Atan2(-cf.X, cf.Y);
        Near(Math.IEEERemainder(camHeading - bodyHeading, 2 * Math.PI), 0, "camera faces the body's heading", 1e-5);

        // Pan 30 deg right on a level body: the camera heading turns clockwise.
        body.Place(new Vec3(200, -930, 45), ned.GtaHeadingRad(70 * Math.PI / 180));
        link.Gimbal.SetTarget(-45, 30);
        for (int i = 0; i < 400; i++) link.Gimbal.Step(0.0025, body, ned);
        var pose = link.CameraPose(groundZ, 84);
        Near(pose.YawDeg, 100, "NED camera yaw = body yaw 70 + pan 30", 1e-3);
        Near(pose.PitchDeg, -45, "NED camera pitch", 1e-3); Near(pose.RollDeg, 0, "NED camera roll", 1e-3);
    }

    /// <summary>Camera axes (right, forward, up) of a GTA camera rotation (order 2: Rz(heading) Rx(pitch) Ry(roll)).</summary>
    static void GtaCameraAxes(GtaCameraPose cam, out Vec3 right, out Vec3 forward, out Vec3 up)
    {
        double p = cam.RotX * Math.PI / 180, r = cam.RotY * Math.PI / 180, h = cam.RotZ * Math.PI / 180;
        Vec3 Apply(Vec3 v) => Vec3.Rotate(Vec3.Rotate(Vec3.Rotate(v, new Vec3(0, 1, 0), r), new Vec3(1, 0, 0), p), new Vec3(0, 0, 1), h);
        right = Apply(new Vec3(1, 0, 0)); forward = Apply(new Vec3(0, 1, 0)); up = Apply(new Vec3(0, 0, 1));
    }

    static Vec3 RotateByQuaternion(Vec3 v, double x, double y, double z, double w)
    {
        var q = new Vec3(x, y, z);
        var t = Vec3.Cross(q, v) * 2;
        return v + t * w + Vec3.Cross(q, t);
    }

    static void DroneOrientation()
    {
        var body = new QuadBody(new Airframe());
        body.Place(default, Math.PI / 2);
        body.Orientation(out double x, out double y, out double z, out double w);
        Near(x, 0, "heading 90: qx", 1e-12); Near(y, 0, "heading 90: qy", 1e-12);
        Near(z, Math.Sqrt(0.5), "heading 90: qz", 1e-12); Near(w, Math.Sqrt(0.5), "heading 90: qw", 1e-12);

        // Each branch of the matrix-to-quaternion conversion: a small tilt (trace > 0) and
        // half turns about right, forward and up.
        var cases = new (string what, Vec3 r, Vec3 f, Vec3 u)[]
        {
            ("tilted", default, default, default),
            ("upside down about right", new Vec3(1, 0, 0), new Vec3(0, -1, 0), new Vec3(0, 0, -1)),
            ("upside down about forward", new Vec3(-1, 0, 0), new Vec3(0, 1, 0), new Vec3(0, 0, -1)),
            ("facing back", new Vec3(-1, 0, 0), new Vec3(0, -1, 0), new Vec3(0, 0, 1)),
        };
        foreach (var c in cases)
        {
            body.Place(default, 0.4);
            if (c.what == "tilted")
            {
                body.R = Vec3.Rotate(body.R, body.F, 0.3); body.U = Vec3.Rotate(body.U, body.F, 0.3);
                body.F = Vec3.Rotate(body.F, body.R, -0.2); body.U = Vec3.Rotate(body.U, body.R, -0.2);
            }
            else { body.R = c.r; body.F = c.f; body.U = c.u; }
            body.Orientation(out x, out y, out z, out w);
            Near(x * x + y * y + z * z + w * w, 1, $"{c.what}: unit quaternion", 1e-9);
            foreach (var (local, world, axis) in new[] { (new Vec3(1, 0, 0), body.R, "right"), (new Vec3(0, 1, 0), body.F, "forward"), (new Vec3(0, 0, 1), body.U, "up") })
            {
                var got = RotateByQuaternion(local, x, y, z, w);
                Near((got - world).Length, 0, $"{c.what}: entity {axis} axis = body {axis}", 1e-9);
            }
        }
    }

    static void SensorConfig()
    {
        var d = ModConfig.Parse("");
        Check(d.Sensors.Enabled && d.Sensors.Host == "127.0.0.1" && d.Sensors.Port == 5612 && d.Sensors.Target == "127.0.0.1:5612",
            "sensors default: on, to udp 127.0.0.1:5612");
        Near(d.Sensors.RateHz, 20, "sensor rate default 20 Hz");
        var c = ModConfig.Parse("[sensors]\nenabled=0\ntarget = 10.0.0.2:6000 ; bridge host\nrate_hz=10\n");
        Check(!c.Sensors.Enabled && c.Sensors.Host == "10.0.0.2" && c.Sensors.Port == 6000, "sensors parsed: off, other target");
        Near(c.Sensors.RateHz, 10, "sensor rate 10 accepted");
        Check(c.Warnings.Count == 0, "valid sensors section has no warnings: " + string.Join(" | ", c.Warnings));
        var t = ModConfig.Parse("[sensors]\nenabled=true\nrate_hz=12.5\n[sensors]\nenabled=FALSE\n");
        Check(!t.Sensors.Enabled && t.Warnings.Count == 0, "enabled accepts true/false too");
        Near(t.Sensors.RateHz, 12.5, "fractional sensor rate within 10..20 accepted");
        var bad = ModConfig.Parse("[sensors]\nenabled=yes\ntarget=localhost:5612\ntarget=127.0.0.1\ntarget=127.0.0.1:70000\ntarget=127.0.0.1:-1\nrate_hz=25\nrate_hz=5\nrate_hz=fast\n");
        Check(bad.Sensors.Enabled && bad.Sensors.Target == "127.0.0.1:5612", "bad sensor values fall back");
        Near(bad.Sensors.RateHz, 20, "sensor rate outside 10..20 falls back to 20");
        Check(bad.Warnings.Count == 8 && bad.Warnings[5].Contains("sensors.rate_hz '25' is not 10..20"),
            "one warning per bad sensors line: " + string.Join(" | ", bad.Warnings));
    }

    static void RangeDatagramLayout()
    {
        var b = new byte[RangeProtocol.Size];
        RangeProtocol.Encode(b, 5, 1234, new[] { 0.25f, 1f, 2f, 3f, 4f, 0.5f, float.PositiveInfinity });
        Check(RangeProtocol.Size == 44, "range datagram is 44 bytes");
        Check(Convert.ToHexString(b).ToLowerInvariant() ==
            "48524e47" + "01000000" + "05000000" + "d2040000"
            + "0000803e" + "0000803f" + "00000040" + "00004040" + "00008040" + "0000003f" + "0000807f",
            "HRNG | version 1 | seq 5 | time_ms 1234 | 7 little-endian f32, +inf = 0x7f800000: " + Convert.ToHexString(b));

        // Through the sensors: a distinct distance per beam shows the order on the wire.
        var frame = new Airframe();
        var body = new QuadBody(frame);
        body.Place(new Vec3(0, 0, 50), 0);
        double? ByBeam(Vec3 a, Vec3 e)
        {
            var dir = (e - a).Normalized;
            for (int i = 0; i < RangeSensors.Beams; i++)
                if ((dir - RangeSensors.Direction(i, body.R, body.F, body.U)).Length < 1e-9)
                    return i == 3 ? null : frame.RadiusM + 1 + 0.25 * i + (i == RangeSensors.Down ? QuadBody.RestGapM : 0);
            return null;
        }
        var s = new RangeSensors(frame, 20, ByBeam);
        Check(s.Update(0.0126, body), "first update sweeps");
        var d = s.Datagram;
        Check(d[0] == 'H' && d[1] == 'R' && d[2] == 'N' && d[3] == 'G' && BitConverter.ToUInt32(d, 4) == 1, "magic and version");
        Check(BitConverter.ToUInt32(d, 8) == 1, "first datagram seq 1");
        Check(BitConverter.ToUInt32(d, 12) == 13, $"time_ms = round(sim time * 1000) (got {BitConverter.ToUInt32(d, 12)})");
        for (int i = 0; i < RangeSensors.Beams; i++)
        {
            float v = BitConverter.ToSingle(d, 16 + 4 * i);
            if (i == 3) Check(float.IsPositiveInfinity(v), "rear beam clear: +inf");
            else Near(v, 1 + 0.25 * i, $"{RangeSensors.Names[i]} is float #{i}", 1e-6);
            Check(v == s.Ranges[i], $"{RangeSensors.Names[i]}: datagram = Ranges");
        }
        Check(s.Update(0.0626, body) && BitConverter.ToUInt32(s.Datagram, 8) == 2 && BitConverter.ToUInt32(s.Datagram, 12) == 63,
            "next sweep: seq 2, time_ms 63");
        Check(RangeProtocol.TimeMs(0.05) == 50 && RangeProtocol.TimeMs(-1) == 0 && RangeProtocol.TimeMs(1e12) == uint.MaxValue, "time_ms rounds and stays in range");
        Check(string.Join(",", RangeSensors.Names) == "front,front_right,right,rear,left,front_left,down", "beam order");
    }

    static void RangeBeamDirections()
    {
        var body = new QuadBody(new Airframe());
        double h = Math.Sqrt(0.5);
        Vec3 Dir(int i) => RangeSensors.Direction(i, body.R, body.F, body.U);
        void Expect(int i, Vec3 want, string what) => Near((Dir(i) - want).Length, 0, $"{what}: {RangeSensors.Names[i]} = ({want.X:F3}, {want.Y:F3}, {want.Z:F3})", 1e-12);

        // Level, GTA heading 0: nose north (+Y), right east (+X).
        body.Place(default, 0);
        Expect(0, new Vec3(0, 1, 0), "level"); Expect(1, new Vec3(h, h, 0), "level"); Expect(2, new Vec3(1, 0, 0), "level");
        Expect(3, new Vec3(0, -1, 0), "level"); Expect(4, new Vec3(-1, 0, 0), "level"); Expect(5, new Vec3(-h, h, 0), "level");
        Expect(6, new Vec3(0, 0, -1), "level");

        // Heading 90 deg (counter-clockwise from +Y): nose west (-X), so right is north.
        body.Place(default, Math.PI / 2);
        Expect(0, new Vec3(-1, 0, 0), "heading 90"); Expect(2, new Vec3(0, 1, 0), "heading 90"); Expect(4, new Vec3(0, -1, 0), "heading 90");
        Expect(3, new Vec3(1, 0, 0), "heading 90"); Expect(1, new Vec3(-h, h, 0), "heading 90"); Expect(6, new Vec3(0, 0, -1), "heading 90");

        // Nose 10 deg up: the front beam climbs with the nose, the down beam leans back, the side beams stay level.
        double nose = 10 * Math.PI / 180;
        body.Place(default, 0);
        body.F = Vec3.Rotate(body.F, body.R, nose); body.U = Vec3.Rotate(body.U, body.R, nose);
        Expect(0, new Vec3(0, Math.Cos(nose), Math.Sin(nose)), "pitched up 10");
        Expect(3, new Vec3(0, -Math.Cos(nose), -Math.Sin(nose)), "pitched up 10");
        Expect(6, new Vec3(0, Math.Sin(nose), -Math.Cos(nose)), "pitched up 10");
        Expect(2, new Vec3(1, 0, 0), "pitched up 10");

        // Rolled 15 deg right: the right beam dips, the left beam rises.
        double roll = 15 * Math.PI / 180;
        body.Place(default, 0);
        body.R = Vec3.Rotate(body.R, body.F, roll); body.U = Vec3.Rotate(body.U, body.F, roll);
        Near(Dir(2).Z, -Math.Sin(roll), "rolled right: right beam points 15 deg down", 1e-12);
        Near(Dir(4).Z, Math.Sin(roll), "rolled right: left beam points 15 deg up", 1e-12);
        for (int i = 0; i < RangeSensors.Beams; i++) Near(Dir(i).Length, 1, $"{RangeSensors.Names[i]} is a unit vector", 1e-12);

        // The rays: from the body centre along each beam for hull + 4 m.
        var rays = new System.Collections.Generic.List<(Vec3, Vec3)>();
        var frame = new Airframe();
        var s = new RangeSensors(frame, 20, (a, b) => { rays.Add((a, b)); return null; });
        body.Place(new Vec3(3, 4, 5), Math.PI / 2);
        s.Update(0, body);
        Check(rays.Count == 7 && s.Rays == 7, "one ray per beam");
        for (int i = 0; i < rays.Count; i++)
        {
            Near((rays[i].Item1 - body.P).Length, 0, $"{RangeSensors.Names[i]} ray starts at the body centre", 1e-12);
            Near((rays[i].Item2 - (body.P + Dir(i) * (s.HullM(i) + RangeSensors.MaxRangeM))).Length, 0, $"{RangeSensors.Names[i]} ray ends hull + 4 m along the beam", 1e-12);
            Check(float.IsPositiveInfinity(s.Ranges[i]), $"{RangeSensors.Names[i]} clear: +inf");
        }
        Check(double.IsPositiveInfinity(s.MinHorizontalM), "all clear: min horizontal +inf");
    }

    static double? FakeRange(Vec3 a, Vec3 b)
    {
        var c = FakeRay(a, b, null);
        return c.HasValue ? (c.Value.Position - a).Length : (double?)null;
    }

    static void RangeHullOffsets()
    {
        var frame = new Airframe();
        Near(new RangeSensors(frame, 20, FakeRange).HorizontalHullM, 0.12, "horizontal hull = collision sphere radius");
        Near(new RangeSensors(frame, 20, FakeRange).DownHullM, 0.121, "down hull = resting centre height (radius + 1 mm rest gap)");

        // Fixed hit distances from the centre: minus the hull, clamped at 0, +inf for none or past the reach.
        var body = new QuadBody(frame);
        body.Place(new Vec3(0, 0, 10), 0);
        foreach (var (dist, want, what) in new (double?, double, string)[]
        {
            (1.12, 1.0, "1.12 m from the centre reads 1 m"),
            (0.05, 0, "a hit inside the hull clamps at 0"),
            (-0.5, 0, "a negative distance clamps at 0"),
            (4.12, 4.0, "4 m past the hull is in range"),
            (4.2, double.PositiveInfinity, "beyond hull + 4 m reads +inf"),
            (null, double.PositiveInfinity, "no hit reads +inf"),
            (double.NaN, double.PositiveInfinity, "a NaN distance reads +inf, never NaN"),
        })
        {
            var s = new RangeSensors(frame, 20, (a, b) => dist);
            s.Update(0, body);
            if (double.IsInfinity(want)) Check(float.IsPositiveInfinity(s.Ranges[0]), what);
            else Near(s.Ranges[0], want, what, 1e-6);
            Check(!float.IsNaN(s.Ranges[RangeSensors.Down]) && s.Ranges[RangeSensors.Down] >= 0, $"{what}: down never NaN or negative");
        }

        // The fake world (ground z = 0, wall x = 10): facing the wall from x 8 at 2 m up.
        body.Place(new Vec3(8, 0, 2), -Math.PI / 2);
        var w = new RangeSensors(frame, 20, FakeRange);
        w.Update(0, body);
        Near(w.Ranges[0], 2 - 0.12, "front: 2 m to the wall minus the 0.12 m hull", 1e-5);
        Near(w.Ranges[1], 2 * Math.Sqrt(2) - 0.12, "front_right (towards the wall at 45 deg)", 1e-5);
        Near(w.Ranges[5], 2 * Math.Sqrt(2) - 0.12, "front_left (towards the wall at 45 deg)", 1e-5);
        Check(float.IsPositiveInfinity(w.Ranges[3]) && float.IsPositiveInfinity(w.Ranges[2]), "rear and side beams clear");
        Near(w.Ranges[RangeSensors.Down], 2 - 0.121, "down: height minus the resting centre height", 1e-5);
        Near(w.MinHorizontalM, 2 - 0.12, "min horizontal = front", 1e-5);

        // Landed on the ground, the down sensor reads 0; stopped at a wall, the front reads the 1 mm gap.
        var lander = new QuadBody(frame) { Sweep = new WorldProbe((x, y) => FakeRay(x, y, null)).Sweep };
        lander.Place(new Vec3(0, 0, 1), 0);
        for (int i = 0; i < 1600; i++) lander.Step(1 / 800.0, All(0));
        var l = new RangeSensors(frame, 20, FakeRange);
        l.Update(0, lander);
        Check(lander.InContact, "landed");
        Near(l.Ranges[RangeSensors.Down], 0, "landed on flat ground: down reads 0", 1e-6);

        double hover = frame.HoverThrottle();
        var flyer = new QuadBody(frame) { Sweep = new WorldProbe((x, y) => FakeRay(x, y, null)).Sweep };
        flyer.Place(new Vec3(8, 0, 5), -Math.PI / 2);
        for (int i = 0; i < 4; i++) flyer.Rpm[i] = hover * frame.MaxRpm;
        flyer.V = new Vec3(3, 0, 0);
        for (int i = 0; i < 800; i++) flyer.Step(1 / 800.0, All(hover));
        var f = new RangeSensors(frame, 20, FakeRange);
        f.Update(0, flyer);
        Check(flyer.P.X > 9.8, $"flew into the wall (x {flyer.P.X:F3})");
        Check(f.Ranges[0] >= 0 && f.Ranges[0] < 0.01, $"against the wall: front reads ~0 ({f.Ranges[0]:F4} m)");
    }

    static void RangeSchedule()
    {
        var body = new QuadBody(new Airframe());
        body.Place(new Vec3(0, 0, 5), 0);
        foreach (var (rate, hz) in new[] { (400, 20.0), (800, 20.0), (800, 10.0), (1200, 15.0) })
        {
            var s = new RangeSensors(body.Frame, hz, FakeRange);
            Near(s.PeriodS, 1 / hz, $"{hz:g} Hz period");
            var swept = new System.Collections.Generic.List<double>();
            double t = 0;
            int stepsPerPeriod = (int)Math.Round(rate / hz);
            var at = new System.Collections.Generic.List<int>();
            for (int k = 1; k <= rate; k++)
            {
                t += 1.0 / rate;   // as PhysicsLink sums its steps
                if (s.Update(t, body)) { swept.Add(t); at.Add(k); }
            }
            Check(at.Count == (int)hz && at[0] == 1, $"{rate} Hz steps, {hz:g} Hz sensors: first step sweeps, {hz:g} sweeps in 1 s (got {at.Count})");
            bool even = true;
            for (int i = 1; i < at.Count; i++) even &= at[i] - at[i - 1] == stepsPerPeriod;
            Check(even, $"{rate} Hz steps, {hz:g} Hz sensors: one sweep every {stepsPerPeriod} steps, none early ({string.Join(",", at)})");
            Check(s.Sweeps == at.Count && s.Rays == 7 * at.Count, "7 rays per sweep, none between sweeps");
        }

        var r = new RangeSensors(body.Frame, 20, FakeRange);
        Check(r.Update(1.0, body), "first call sweeps immediately");
        Check(!r.Update(1.0499, body), "no sweep before 50 ms of sim time");
        Check(r.Update(1.05, body), "sweep at 50 ms");
        Check(!r.Update(1.05, body), "the same time does not sweep twice");
        Check(r.Update(1.4, body) && !r.Update(1.42, body), "after a 350 ms gap: one sweep, not a burst of seven");
        Check(r.Update(1.45, body), "then one period from the late sweep");
        Check(r.Update(0.0025, body), "sim clock went back (SITL restart): sweeps at once");
        Check(!r.Update(0.05, body) && r.Update(0.0525, body), "then on the new clock");
        Check(BitConverter.ToUInt32(r.Datagram, 8) == r.Sweeps && r.Sweeps == 6, "seq keeps counting across the restart");

        foreach (double bad in new[] { 9.9, 20.1, 0, double.NaN })
        {
            bool threw = false;
            try { _ = new RangeSensors(body.Frame, bad, FakeRange); } catch (ArgumentOutOfRangeException) { threw = true; }
            Check(threw, $"rate {bad} Hz rejected");
        }
    }

    static void RangeSensorsDisabled()
    {
        int rays = 0;
        double? Count(Vec3 a, Vec3 b) { rays++; return null; }
        var off = ModConfig.Parse("[sensors]\nenabled=0\n").Sensors;
        Check(RangeSensors.Create(off, new Airframe(), Count) == null && rays == 0, "enabled=0: no sensors, nothing cast");
        var on = RangeSensors.Create(ModConfig.Parse("[sensors]\nrate_hz=10\n").Sensors, new Airframe(), Count);
        Check(on != null && on.RateHz == 10, "enabled by default, at the configured rate");
    }

    static void RangeSensorsOnPhysicsLink()
    {
        var frame = new Airframe();
        var body = new QuadBody(frame) { FloorZ = 0 };
        var ned = new NedFrame { Origin = new Vec3(0, 0, frame.RadiusM + 0.001) };
        var link = new PhysicsLink(body, ned, new GimbalMount(), ned.Origin, 0);
        var s = new RangeSensors(frame, 20, (a, b) => b.Z < a.Z ? a.Z : (double?)null);   // flat ground at z = 0
        var sent = new System.Collections.Generic.List<(uint seq, uint ms)>();
        var sentAt = new System.Collections.Generic.List<double>();
        int advanced = 0;
        link.Advanced = () =>
        {
            advanced++;
            if (!s.Update(link.SimTimeS, link.Body)) return;
            sent.Add((BitConverter.ToUInt32(s.Datagram, 8), BitConverter.ToUInt32(s.Datagram, 12)));
            sentAt.Add(link.SimTimeS);
        };
        var queue = new System.Collections.Generic.Queue<ServoFrame?>();
        for (uint n = 1; n <= 41; n++) queue.Enqueue(new ServoFrame { FrameRate = 400, FrameCount = n, Pwm = Pwm(16, 1000) });
        queue.Enqueue(new ServoFrame { FrameRate = 400, FrameCount = 41, Pwm = Pwm(16, 1000) });
        var r = link.Pump(1.0, () => queue.Count > 0 ? queue.Dequeue() : null, _ => { }, () => false);
        Check(r.Steps == 41 && r.Resent == 1 && advanced == 41, $"Advanced runs once per step, not for a re-answered frame ({advanced})");
        bool times = sent.Count == 3;
        for (int i = 0; times && i < sent.Count; i++) times = sent[i].seq == i + 1 && sent[i].ms == RangeProtocol.TimeMs(sentAt[i]) && Math.Abs(sent[i].ms - sentAt[i] * 1000) <= 0.5 + 1e-9;
        Check(times && Math.Abs(sentAt[0] - 1 / 400.0) < 1e-12 && Math.Abs(sentAt[1] - 21 / 400.0) < 1e-9 && Math.Abs(sentAt[2] - 41 / 400.0) < 1e-9,
            "sweeps at sim 2.5, 52.5, 102.5 ms, seq 1..3, time_ms the sim time: " + string.Join(" ", sent));
        Near(s.Ranges[RangeSensors.Down], 0, "resting at the origin: down reads 0", 1e-6);
    }

    static void MiniJsonParse()
    {
        var o = (System.Collections.Generic.Dictionary<string, object>)MiniJson.Parse(
            " {\"a\": [1, -2.5e1, true, false, null], \"s\": \"q\\\"\\\\\\n\\u00e9\", \"o\": {}} \n");
        var a = (System.Collections.Generic.List<object>)o["a"];
        Check(a.Count == 5 && (double)a[0] == 1 && (double)a[1] == -25 && (bool)a[2] && !(bool)a[3] && a[4] == null, "array values");
        Check((string)o["s"] == "q\"\\\n\u00e9", "string escapes");
        Check(MiniJson.Parse(MiniJson.Quote("tab\t\"x\"\\")) as string == "tab\t\"x\"\\", "Quote round-trips");
        Check(MiniJson.Number(0.1) == "0.1" && MiniJson.Number(double.NaN) == "null", "numbers: round-trip text, NaN as null");
        foreach (string bad in new[] { "", "{", "[1,]", "{\"a\":1}x", "{\"a\":1,\"a\":2}", "{a:1}", "\"\\x\"", "1e999", "tru" })
        {
            bool threw = false;
            try { MiniJson.Parse(bad); } catch (FormatException) { threw = true; }
            Check(threw, $"refuses {bad}");
        }
    }

    static void SurveyGridCells()
    {
        Check(SurveyGrid.TryCellsPerSide(5, out int n) && n == 50, "5 m cells: 50 per 250 m tile");
        Check(SurveyGrid.TryCellsPerSide(1, out n) && n == 250 && SurveyGrid.TryCellsPerSide(50, out n) && n == 5, "1 m and 50 m cells divide the tile");
        Check(!SurveyGrid.TryCellsPerSide(4, out _) && !SurveyGrid.TryCellsPerSide(0.5, out _) && !SurveyGrid.TryCellsPerSide(100, out _)
            && !SurveyGrid.TryCellsPerSide(0, out _) && !SurveyGrid.TryCellsPerSide(double.NaN, out _), "cells that do not divide the tile into 5..250 are refused");

        // Tiles as horus/maps/tiles.py tiles_touched: floor(e / 250) by floor(n / 250), serpentine rows from the south.
        var tiles = SurveyGrid.Tiles(-10, -10, 10, 260);
        string order = string.Join(" ", tiles.ConvertAll(t => t.ToString()));
        Check(order == "-1_-1 0_-1 1_-1 1_0 0_0 -1_0", "tile order: " + order);
        Check(SurveyGrid.Tiles(0, 0, 249.9, 249.9).Count == 1 && SurveyGrid.Tiles(0, 0, 250, 250).Count == 4, "a box edge on 250 touches the next tile");

        var t = new SurveyTileKey(2, -3);
        SurveyGrid.CellCentreNed(t, 0, 0, 5, out double north, out double east);
        Near(east, 502.5, "first cell east"); Near(north, -747.5, "first cell north");
        SurveyGrid.CellCentreNed(t, 49, 49, 5, out north, out east);
        Near(east, 747.5, "last cell east"); Near(north, -502.5, "last cell north");
        SurveyGrid.CellCentreNed(t, 3, 0, 5, out north, out east);
        Near(east, 517.5, "i runs east"); Near(north, -747.5, "i keeps the row");

        // Cell centres in GTA through PoseMapping: the same place the camera would be at that NED position.
        SurveyGrid.CellCentreGta(t, 0, 0, 5, Legion, out double x, out double y);
        Near(x, 195 + 502.5, "Legion heading 0: GTA x = origin x + east"); Near(y, -933 - 747.5, "GTA y = origin y + north");
        foreach (double heading in new[] { 0.0, 37.0, 90.0, -120.0 })
        {
            var o = new Origin { X = 100, Y = 50, HeadingDeg = heading };
            SurveyGrid.CellCentreGta(t, 7, 11, 5, o, out x, out y);
            SurveyGrid.CellCentreNed(t, 7, 11, 5, out north, out east);
            var cam = MapNed(north, east, 0, o);
            Near(x, cam.X, $"cell x = camera x at heading {heading}", 1e-9); Near(y, cam.Y, $"cell y = camera y at heading {heading}", 1e-9);
        }
    }

    static void SurveyTileFormat()
    {
        Check(SurveyTile.Decimetres(29.69) == 297 && SurveyTile.Decimetres(-0.05) == -1 && SurveyTile.Decimetres(1500) == 15000, "decimetres, half away from zero");
        Check(SurveyTile.Decimetres(null) == SurveyTile.NoHit && SurveyTile.Decimetres(double.NaN) == SurveyTile.NoHit, "no hit is the sentinel");
        Check(SurveyTile.Decimetres(-4000) == -32767 && SurveyTile.Decimetres(4000) == 32767, "out of range clamps above the sentinel");

        var key = new SurveyTileKey(-3, 7);
        var h = new short[25];
        for (int k = 0; k < h.Length; k++) h[k] = (short)(k * 10 - 50);
        h[0] = 297; h[3] = SurveyTile.NoHit; h[24] = SurveyTile.NoHit;
        byte[] b = SurveyTile.Encode(key, 5, 50, h);
        Check(b.Length == SurveyTile.HeaderSize + 50, $"24-byte header + 2 bytes per cell ({b.Length})");
        Check(b[0] == 'H' && b[1] == 'S' && b[2] == 'R' && b[3] == 'V' && b[4] == 1 && b[5] == 0 && b[6] == 5 && b[7] == 0, "magic, version 1, n");
        Check(b[8] == 0xFD && b[9] == 0xFF && b[10] == 0xFF && b[11] == 0xFF && b[12] == 7, "tile x -3, y 7 little-endian");
        Check(BitConverter.ToSingle(b, 16) == 50f && BitConverter.ToUInt32(b, 20) == 23, "cell_m and the hit count");
        Check(b[24] == 0x29 && b[25] == 0x01 && b[30] == 0x00 && b[31] == 0x80, "cell 0 = 297 dm, cell 3 = 0x8000 no hit");
        Check(SurveyTile.TryDecode(b, out var k2, out int n2, out double cell2, out var h2, out string err)
            && k2.X == -3 && k2.Y == 7 && n2 == 5 && cell2 == 50 && System.Linq.Enumerable.SequenceEqual(h, h2), "round trip: " + err);

        Check(!SurveyTile.TryDecode(b[..^1], out _, out _, out _, out _, out err) && err.Contains("bytes"), "truncated file refused: " + err);
        var bad = (byte[])b.Clone(); bad[0] = (byte)'X';
        Check(!SurveyTile.TryDecode(bad, out _, out _, out _, out _, out _), "wrong magic refused");
        bad = (byte[])b.Clone(); bad[4] = 2;
        Check(!SurveyTile.TryDecode(bad, out _, out _, out _, out _, out _), "version 2 refused");
        bad = (byte[])b.Clone(); bad[30] = 0; bad[31] = 0;
        Check(!SurveyTile.TryDecode(bad, out _, out _, out _, out _, out err) && err.Contains("hit count"), "a cell changed under the header refused");

        string dir = TempDir();
        try
        {
            string path = Path.Combine(dir, SurveyTile.FileName(key));
            Check(Path.GetFileName(path) == "-3_7.bin", "file name <x>_<y>.bin");
            SurveyTile.WriteAtomic(path, new byte[] { 1 });
            SurveyTile.WriteAtomic(path, b);
            Check(System.Linq.Enumerable.SequenceEqual(File.ReadAllBytes(path), b) && !File.Exists(path + ".tmp"), "atomic rewrite replaces the file, no temporary left");
        }
        finally { Directory.Delete(dir, true); }
    }

    static void SurveyRequestParsing()
    {
        Check(SurveyRequest.TryParse("{\"box\": [-500, -500.5, 500, 500]}", out var r, out string err), "minimal request: " + err);
        Check(r.MinN == -500 && r.MinE == -500.5 && r.MaxN == 500 && r.MaxE == 500 && r.CellM == 5 && r.Output == "HorusGtaSurvey"
            && r.SettleS == 1 && r.LoadTimeoutS == 10 && r.LoadAttempts == 3, "box order [min_n, min_e, max_n, max_e] and defaults");
        Check(SurveyRequest.TryParse("{\"box\":[-6237.1,-4436.4,8988.9,7347.1],\"cell_m\":5,\"output\":\"whole_map-1.a\",\"settle_s\":0,\"load_timeout_s\":8,\"load_attempts\":2}",
            out r, out err) && r.Output == "whole_map-1.a" && r.SettleS == 0 && r.LoadTimeoutS == 8 && r.LoadAttempts == 2, "full request (the map's bounds): " + err);
        foreach (var (json, why) in new[]
        {
            ("{}", "box is required"), ("[1]", "not a JSON object"), ("{\"box\": [1, 2, 3]}", "box must be"),
            ("{\"box\": [10, 0, 10, 5]}", "min_n < max_n"), ("{\"box\": [0, 0, 1, \"5\"]}", "box must be"), ("{\"box\": [0, 0, 1, 25000]}", "box must be"),
            ("{\"box\": [0, 0, 1, 1], \"cell_m\": 4}", "cell_m"), ("{\"box\": [0, 0, 1, 1], \"output\": \"../x\"}", "output"),
            ("{\"box\": [0, 0, 1, 1], \"output\": \"a/b\"}", "output"), ("{\"box\": [0, 0, 1, 1], \"output\": \"\"}", "output"),
            ("{\"box\": [0, 0, 1, 1], \"load_attempts\": 1.5}", "load_attempts"), ("{\"box\": [0, 0, 1, 1], \"load_timeout_s\": 1}", "load_timeout_s"),
            ("{\"box\": [0, 0, 1, 1], \"cells\": 5}", "unknown key \"cells\""), ("box=1", "not JSON"),
        })
            Check(!SurveyRequest.TryParse(json, out r, out err) && r == null && err.Contains(why), $"{json} refused with '{why}' (got '{err}')");
    }

    /// <summary>A world for the survey: a top surface per GTA x, y, water, and a wall clock that each ray advances by 0.25 ms (the in-game cost).</summary>
    sealed class SurveyWorld
    {
        public double ClockS;
        public Func<double, double, double?> Top = (x, y) => 30;
        public Func<double, double, bool> Water = (x, y) => false;
        public int Rays, RaysThisTick, MaxRaysPerTick;
        public readonly System.Collections.Generic.List<(double X, double Y, double Z)> Focus = new System.Collections.Generic.List<(double, double, double)>();
    }

    static string TempDir()
    {
        string dir = Path.Combine(Path.GetTempPath(), "horus_survey_" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        return dir;
    }

    static SurveyRun NewSurvey(string dir, string json, SurveyWorld w, Origin? origin = null)
    {
        Check(SurveyRequest.TryParse(json, out var req, out string err), "survey request parses: " + err);
        return new SurveyRun(req, origin ?? Legion, 29.69, "probe", 0.121, dir, Path.Combine(dir, "HorusGtaSurvey.progress.json"))
        {
            CastDown = (x, y) => { w.ClockS += 0.00025; w.Rays++; w.RaysThisTick++; return w.Top(x, y); },
            IsWater = (x, y) => w.Water(x, y),
            KeepLoaded = (x, y, z) => w.Focus.Add((x, y, z)),
            Clock = () => w.ClockS,
        };
    }

    /// <summary>Tick at 30 Hz with a 10 ms budget until the survey ends; the number of ticks.</summary>
    static int RunSurvey(SurveyRun run, SurveyWorld w, ref double now, int maxTicks = 20000, Func<int, bool> before = null)
    {
        for (int tick = 0; tick < maxTicks; tick++)
        {
            if (before != null && !before(tick)) continue;
            w.RaysThisTick = 0;
            bool going = run.Step(now, 0.010);
            w.MaxRaysPerTick = Math.Max(w.MaxRaysPerTick, w.RaysThisTick);
            now += 1 / 30.0;
            if (!going) return tick;
        }
        return maxTicks;
    }

    static System.Collections.Generic.Dictionary<string, object> ReadJson(string path) =>
        (System.Collections.Generic.Dictionary<string, object>)MiniJson.Parse(File.ReadAllText(path));

    static short[] ReadTile(string folder, int x, int y)
    {
        Check(SurveyTile.TryDecode(File.ReadAllBytes(Path.Combine(folder, $"{x}_{y}.bin")), out var k, out _, out _, out var h, out string err)
            && k.X == x && k.Y == y, $"tile {x}_{y} decodes: {err}");
        return h;
    }

    const string TwoTiles = "{\"box\": [0, 0, 10, 260], \"cell_m\": 25, \"output\": \"out\", \"settle_s\": 0.5}";

    static void SurveyRunWritesTiles()
    {
        string dir = TempDir();
        try
        {
            // A 60 m block over GTA x 200..260 (east 5..65 from Legion), ground at z 30 elsewhere.
            var w = new SurveyWorld { Top = (x, y) => x >= 200 && x < 260 ? 60.04 : 30 };
            var run = NewSurvey(dir, TwoTiles, w);
            Check(run.Begin(0), "survey begins");
            Check(run.Tiles.Count == 2 && run.CellsPerSide == 10, "two tiles of 10x10 cells");
            int settleTicks = 0;
            double now = 0;
            int ticks = RunSurvey(run, w, ref now, before: tick =>
            {
                // Ticks between the passing probes and the first cast (only the first tile: tile 2 is already streamed).
                if (run.Rays == 0 && run.ProbeRays >= 5) settleTicks++;
                return true;
            });
            Check(run.State == "done" && run.TilesWritten == 2 && run.Rays == 200, $"done: {run.TilesWritten} tiles, {run.Rays} rays ({run.State} {run.Message})");
            Check(ticks < 200, $"finished in {ticks} ticks");
            Check(w.MaxRaysPerTick <= 41 && w.MaxRaysPerTick >= 40, $"casting stops at the 10 ms budget: at most {w.MaxRaysPerTick} rays of 0.25 ms per tick");
            Check(settleTicks >= 15, $"probes passed, then the tile stays streamed settle_s 0.5 before casting ({settleTicks} ticks at 30 Hz)");
            Check(w.Focus.Count > 0 && Math.Abs(w.Focus[0].X - (195 + 125)) < 1e-9 && Math.Abs(w.Focus[0].Y - (-933 + 125)) < 1e-9, "the focus goes to the first tile's centre");
            Check(w.Focus.Exists(f => Math.Abs(f.X - (195 + 375)) < 1e-9) && Math.Abs(w.Focus[w.Focus.Count - 1].Z - 30) < 1e-9, "then to the second, at the centre probe's ground z");

            string folder = Path.Combine(dir, "out");
            var t0 = ReadTile(folder, 0, 0);
            for (int j = 0; j < 10; j++)
                for (int i = 0; i < 10; i++)
                {
                    double gx = 195 + (i + 0.5) * 25;
                    short want = (short)(gx >= 200 && gx < 260 ? 600 : 300);
                    if (t0[j * 10 + i] != want) Check(false, $"tile 0_0 cell i {i} j {j}: {t0[j * 10 + i]} dm, want {want}");
                }
            Check(t0[0] == 600 && t0[1] == 600 && t0[2] == 600 && t0[3] == 300, "the block covers cells 0-2 of each row (x 207.5, 232.5, 257.5)");
            Check(Array.TrueForAll(ReadTile(folder, 1, 0), v => v == 300), "tile 1_0 is ground");

            var meta = ReadJson(Path.Combine(folder, SurveyRun.MetadataFile));
            Check((double)meta["home_ground_z"] == 29.69 && (string)meta["home_ground_source"] == "probe" && (double)meta["rest_above_ground_m"] == 0.121
                && (double)meta["cell_m"] == 25 && (double)meta["cells_per_side"] == 10, "survey.json records the home ground z and the grid");
            var o = (System.Collections.Generic.Dictionary<string, object>)meta["origin"];
            Check((double)o["x"] == 195 && (double)o["y"] == -933 && (double)o["heading_deg"] == 0, "survey.json records [origin]");
            var p = ReadJson(Path.Combine(dir, "HorusGtaSurvey.progress.json"));
            Check((string)p["state"] == "done" && (double)p["tiles_total"] == 2 && (double)p["tiles_done"] == 2 && (double)p["rays"] == 200
                && ((System.Collections.Generic.List<object>)p["unloaded"]).Count == 0 && p["current"] == null, "progress: done, 2 of 2, 200 rays");
            Near((double)p["rays_per_s"], 4000, "rays per second while casting (0.25 ms each)", 1);
            Check(Directory.GetFiles(folder, "*.tmp").Length == 0, "no temporary files left");

            // SITL connects mid-cast: the run suspends, and the tile starts again (load, settle, all its cells) on resume.
            string dir2 = Path.Combine(dir, "again");
            Directory.CreateDirectory(dir2);
            var w2 = new SurveyWorld { Top = w.Top };
            var again = NewSurvey(dir2, TwoTiles, w2);
            again.Begin(0);
            now = 0;
            bool suspended = false;
            long raysAtSuspend = 0;
            RunSurvey(again, w2, ref now, before: tick =>
            {
                if (!suspended && again.Rays > 0)
                {
                    raysAtSuspend = again.Rays;
                    again.Suspend(now, "SITL connected");
                    suspended = true;
                    Check(ReadJson(Path.Combine(dir2, "HorusGtaSurvey.progress.json"))["state"] as string == "suspended", "progress shows suspended");
                    Check(again.Step(now, 0.010) && again.Rays == raysAtSuspend, "a suspended run casts nothing and is not over");
                    again.Resume(now);
                }
                return true;
            });
            Check(suspended && raysAtSuspend > 0 && raysAtSuspend < 100 && again.State == "done" && again.Rays == 200 + raysAtSuspend,
                $"resumed run re-casts the interrupted tile: {again.Rays} rays, {raysAtSuspend} before the suspension");
            Check(System.Linq.Enumerable.SequenceEqual(ReadTile(Path.Combine(dir2, "out"), 0, 0), t0), "and writes the same tile");
        }
        finally { Directory.Delete(dir, true); }
    }

    static void SurveyResumes()
    {
        string dir = TempDir();
        try
        {
            var w = new SurveyWorld();
            double now = 0;
            var first = NewSurvey(dir, TwoTiles, w);
            first.Begin(now);
            RunSurvey(first, w, ref now);
            string folder = Path.Combine(dir, "out");

            // Restart: both tiles are on disk, nothing is cast.
            var w2 = new SurveyWorld();
            var second = NewSurvey(dir, TwoTiles, w2);
            Check(second.Begin(now) && second.TilesSkipped == 2, $"restart skips the written tiles ({second.TilesSkipped})");
            RunSurvey(second, w2, ref now);
            Check(second.State == "done" && w2.Rays == 0 && w2.Focus.Count == 0, "and casts nothing");

            // A truncated tile (the game died while writing it without the rename) is surveyed again.
            string path = Path.Combine(folder, "1_0.bin");
            File.WriteAllBytes(path, File.ReadAllBytes(path)[..100]);
            var w3 = new SurveyWorld { Top = (x, y) => 31 };
            var third = NewSurvey(dir, TwoTiles, w3);
            Check(third.Begin(now) && third.TilesSkipped == 1, "the truncated tile is not skipped");
            RunSurvey(third, w3, ref now);
            Check(third.TilesWritten == 1 && third.Rays == 100 && Array.TrueForAll(ReadTile(folder, 1, 0), v => v == 310), "only it is surveyed again");

            // The folder holds another survey: refused, nothing touched.
            var w4 = new SurveyWorld();
            var other = NewSurvey(dir, TwoTiles.Replace("\"cell_m\": 25", "\"cell_m\": 50"), w4);
            Check(!other.Begin(now) && other.State == "error" && other.Message.Contains("another cell size"), "another cell size refused: " + other.Message);
            var p = ReadJson(Path.Combine(dir, "HorusGtaSurvey.progress.json"));
            Check((string)p["state"] == "error" && ((string)p["message"]).Contains("new output folder"), "progress reports the error");
            var moved = NewSurvey(dir, TwoTiles, w4, new Origin { X = 196, Y = -933, HeadingDeg = 0 });
            Check(!moved.Begin(now) && moved.Message.Contains("another origin"), "another origin refused: " + moved.Message);
            Check(Array.TrueForAll(ReadTile(folder, 1, 0), v => v == 310) && w4.Rays == 0, "the refused runs cast and wrote nothing");
        }
        finally { Directory.Delete(dir, true); }
    }

    static void SurveyUnloadedAndWaterTiles()
    {
        string dir = TempDir();
        try
        {
            // Tile 1_0 (GTA x 445..695) never loads: no ray hits and it is not water.
            const string Req = "{\"box\": [0, 0, 10, 260], \"cell_m\": 25, \"output\": \"out\", \"settle_s\": 0, \"load_timeout_s\": 5, \"load_attempts\": 2}";
            var w = new SurveyWorld { Top = (x, y) => x < 445 ? 30 : (double?)null };
            var run = NewSurvey(dir, Req, w);
            run.Begin(0);
            double now = 0;
            int ticks = RunSurvey(run, w, ref now);
            string folder = Path.Combine(dir, "out");
            Check(run.State == "done" && run.TilesWritten == 1 && run.Unloaded.Count == 1 && run.Unloaded[0].X == 1, "tile 1_0 listed as unloaded");
            Check(now >= 10 && now < 11, $"after 2 attempts of 5 s ({now:F2} s)");
            Check(!File.Exists(Path.Combine(folder, "1_0.bin")) && File.Exists(Path.Combine(folder, "0_0.bin")), "no file for the unloaded tile");
            var p = ReadJson(Path.Combine(dir, "HorusGtaSurvey.progress.json"));
            var unloaded = (System.Collections.Generic.List<object>)p["unloaded"];
            Check(unloaded.Count == 1 && (double)((System.Collections.Generic.List<object>)unloaded[0])[0] == 1 && ((string)p["message"]).Contains("1 tile(s) unloaded"),
                "progress lists it");

            // The next run retries it; now it is sea: every probe stands on water, nothing hits.
            var sea = new SurveyWorld { Top = (x, y) => x < 445 ? 30 : (double?)null, Water = (x, y) => x >= 445 };
            var retry = NewSurvey(dir, Req, sea);
            Check(retry.Begin(0) && retry.TilesSkipped == 1, "the retry skips the written tile");
            now = 0;
            double castAt = -1;
            RunSurvey(retry, sea, ref now, before: tick => { if (castAt < 0 && retry.Rays > 0) castAt = now; return true; });
            Check(retry.State == "done" && retry.Unloaded.Count == 0 && retry.TilesWritten == 1, "the sea tile is written");
            Check(castAt >= SurveyRun.WaterWaitS, $"an all-water tile waits {SurveyRun.WaterWaitS:g} s before casting ({castAt:F2} s)");
            Check(Array.TrueForAll(ReadTile(folder, 1, 0), v => v == SurveyTile.NoHit), "its cells are no-hit");
            Check(Math.Abs(sea.Focus[sea.Focus.Count - 1].Z) < 1e-9, "the focus goes to sea level over water");

            // A probe point neither hit nor water keeps a tile from loading even when the others hit.
            string dir2 = Path.Combine(dir, "partial");
            Directory.CreateDirectory(dir2);
            var holes = new SurveyWorld { Top = (x, y) => Math.Abs(x - (195 + 62.5)) < 1 && Math.Abs(y - (-933 + 62.5)) < 1 ? (double?)null : 30 };
            var partial = NewSurvey(dir2, "{\"box\": [0, 0, 10, 10], \"cell_m\": 25, \"output\": \"out\", \"load_timeout_s\": 5, \"load_attempts\": 1}", holes);
            partial.Begin(0);
            now = 0;
            RunSurvey(partial, holes, ref now);
            Check(partial.Unloaded.Count == 1 && partial.Rays == 0, "one silent quadrant probe: unloaded, no cells cast");
        }
        finally { Directory.Delete(dir, true); }
    }
}
