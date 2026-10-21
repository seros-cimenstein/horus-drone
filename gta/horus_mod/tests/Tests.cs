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
        Run(SharedVector);
        Run(Rejections);
        Run(SeqOrdering);
        Run(LinkFreshness);
        Run(LinkDrain);
        Run(PositionMapping);
        Run(HeadingMatchesPosition);
        Run(RotationMapping);
        Run(VerticalFov);
        Run(ConfigDefaults);
        Run(ConfigParsing);
        Run(SceneConfig);
        Run(PhysicsConfig);
        Run(ShippedIni);
        Run(CameraBlockLayout);
        Run(CameraPathSegments);
        Run(CameraPathRepeats);
        Run(ServoFrameDecode);
        Run(GimbalFromServo);
        Run(QuadHover);
        Run(QuadTorqueSigns);
        Run(QuadRestsOnFloor);
        Run(NedFrameMatchesPoseMapping);
        Run(FdmJsonLine);
        Run(PhysicsLinkLockstep);
        Run(WorldProbeRays);
        Run(QuadStopsAtWall);
        Run(ImuSaturates);
        Run(CameraOnTheMount);
        Run(DroneOrientation);
        Console.WriteLine(failures == 0 ? $"OK: {checks} checks passed" : $"{failures} of {checks} checks FAILED");
        return failures == 0 ? 0 : 1;
    }

    static void Run(Action test)
    {
        try { test(); }
        catch (Exception e) { failures++; Console.WriteLine($"FAIL: {test.Method.Name} threw {e}"); }
    }

    static byte[] Hex(string hex)
    {
        var b = new byte[hex.Length / 2];
        for (int i = 0; i < b.Length; i++) b[i] = Convert.ToByte(hex.Substring(2 * i, 2), 16);
        return b;
    }

    static PosePacket Sample(ulong seq = 7) => new PosePacket
    {
        Seq = seq, NorthM = 1, EastM = 2, DownM = -3, RollDeg = 0, PitchDeg = -10, YawDeg = 45, HfovDeg = 90,
    };

    static void SharedVector()
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "pose_vector.json")));
        var v = doc.RootElement;
        byte[] bytes = Hex(v.GetProperty("hex").GetString());
        Check(PoseProtocol.Size == 56, "packet size is 56 bytes");
        Check(bytes.Length == PoseProtocol.Size, "vector is one packet");
        Check(PoseProtocol.TryParse(bytes, bytes.Length, out var p) == PacketError.None, "vector parses");
        Check(p.Seq == v.GetProperty("seq").GetUInt64(), "vector seq");
        Near(p.NorthM, v.GetProperty("north_m").GetDouble(), "vector north");
        Near(p.EastM, v.GetProperty("east_m").GetDouble(), "vector east");
        Near(p.DownM, v.GetProperty("down_m").GetDouble(), "vector down");
        Near(p.RollDeg, v.GetProperty("roll_deg").GetDouble(), "vector roll");
        Near(p.PitchDeg, v.GetProperty("pitch_deg").GetDouble(), "vector pitch");
        Near(p.YawDeg, v.GetProperty("yaw_deg").GetDouble(), "vector yaw");
        Near(p.HfovDeg, v.GetProperty("hfov_deg").GetDouble(), "vector hfov");
        Check(Convert.ToHexString(PoseProtocol.Encode(p)).ToLowerInvariant() == v.GetProperty("hex").GetString(),
            "Encode reproduces the vector bytes");
    }

    static void Rejections()
    {
        byte[] good = PoseProtocol.Encode(Sample());
        Check(PoseProtocol.TryParse(good, good.Length, out _) == PacketError.None, "sample parses");

        var longer = new byte[57];
        Array.Copy(good, longer, good.Length);
        Check(PoseProtocol.TryParse(longer, 57, out _) == PacketError.Size, "57 bytes rejected");
        Check(PoseProtocol.TryParse(good, 55, out _) == PacketError.Size, "55 bytes rejected");
        Check(PoseProtocol.TryParse(null, 0, out _) == PacketError.Size, "null rejected");

        var magic = (byte[])good.Clone();
        magic[0] = (byte)'X';
        Check(PoseProtocol.TryParse(magic, magic.Length, out _) == PacketError.Magic, "bad magic rejected");

        var version = (byte[])good.Clone();
        version[4] = 2;
        Check(PoseProtocol.TryParse(version, version.Length, out _) == PacketError.Version, "version 2 rejected");

        foreach (var bad in new[]
        {
            new PosePacket { Seq = 1, NorthM = double.NaN, HfovDeg = 90 },
            new PosePacket { Seq = 1, DownM = double.PositiveInfinity, HfovDeg = 90 },
            new PosePacket { Seq = 1, YawDeg = float.NaN, HfovDeg = 90 },
            new PosePacket { Seq = 1, HfovDeg = float.PositiveInfinity },
        })
        {
            var b = PoseProtocol.Encode(bad);
            Check(PoseProtocol.TryParse(b, b.Length, out var parsed) == PacketError.NonFinite, "non-finite field rejected");
            Check(parsed.Seq == 0, "rejected packet is not returned");
        }
        foreach (float fov in new[] { 0f, -5f, 180f, 200f })
        {
            var p = Sample();
            p.HfovDeg = fov;
            var b = PoseProtocol.Encode(p);
            Check(PoseProtocol.TryParse(b, b.Length, out _) == PacketError.FovRange, $"hfov {fov} rejected");
        }
    }

    static void SeqOrdering()
    {
        var t = new SeqTracker();
        Check(t.Accept(10), "first seq accepted");
        Check(t.Accept(12), "newer seq accepted");
        Check(!t.Accept(11), "older seq dropped");
        Check(!t.Accept(12), "duplicate seq dropped");
        Check(t.Newest == 12, "newest kept");
        Check(t.Accept(5000), "jump ahead accepted");
        Check(t.Accept(3), "restart (far older seq) accepted");
        Check(t.Newest == 3, "restart becomes newest");
        t.Reset();
        Check(t.Accept(0), "seq 0 accepted after reset");
    }

    static void LinkFreshness()
    {
        var link = new PoseLink(2.0);
        Check(!link.IsFresh(0), "empty link is not fresh");
        var a = PoseProtocol.Encode(Sample(5));
        Check(link.Offer(a, a.Length, 10.0) == PacketError.None, "valid offer");
        Check(link.HasLatest && link.Latest.Seq == 5, "latest stored");
        Check(link.IsFresh(11.9), "fresh within timeout");
        Check(!link.IsFresh(12.1), "stale after timeout");

        var older = PoseProtocol.Encode(Sample(4));
        link.Offer(older, older.Length, 12.0);
        Check(link.Latest.Seq == 5, "older packet does not replace the newest");
        Check(link.IsFresh(13.5), "older but valid packet still counts as traffic");

        var junk = new byte[10];
        Check(link.Offer(junk, junk.Length, 13.0) == PacketError.Size, "junk rejected");
        Check(link.BadPackets == 1 && link.LastError == PacketError.Size, "bad packet counted");
        Check(!link.IsFresh(15.0), "junk does not keep the link alive");

        link.Reset();
        Check(!link.HasLatest && !link.IsFresh(13.0), "reset clears the stream");
        var restart = PoseProtocol.Encode(Sample(0));
        link.Offer(restart, restart.Length, 20.0);
        Check(link.Latest.Seq == 0 && link.IsFresh(20.0), "restarted bridge accepted after reset");
    }

    // A8: one drain reaches the newest queued pose, a time budget may split it across ticks, and a
    // backlog queued while the script did not tick (pause) is thrown away, not applied.
    static void LinkDrain()
    {
        var queue = new System.Collections.Generic.Queue<byte[]>();
        void Enqueue(ulong from, ulong to)
        {
            for (ulong s = from; s <= to; s++) queue.Enqueue(PoseProtocol.Encode(Sample(s)));
        }
        int Receive(byte[] into)
        {
            if (queue.Count == 0) return -1;
            byte[] d = queue.Dequeue();
            Array.Copy(d, into, d.Length);
            return d.Length;
        }
        var buffer = new byte[2048];

        var link = new PoseLink(2.0);
        Enqueue(1, 220);
        queue.Enqueue(new byte[10]);   // one junk datagram in the backlog
        var all = link.Drain(buffer, Receive, 10.0, () => false);
        Check(all.Datagrams == 221 && all.Discarded == 0 && !all.BudgetHit, $"whole backlog read in one drain ({all.Datagrams})");
        Check(link.Latest.Seq == 220 && link.BadPackets == 1, "newest packet kept, junk counted");
        Check(link.Drain(buffer, Receive, 10.016, () => false).Datagrams == 0, "empty socket: nothing read");

        Enqueue(4001, 4250);
        int calls = 0;
        var first = link.Drain(buffer, Receive, 10.032, () => ++calls >= 100);
        Check(first.Datagrams == 100 && first.BudgetHit, "budget ends the drain after the datagram that used it up");
        Check(link.Latest.Seq == 4100 && queue.Count == 150, "the rest stays queued");
        var second = link.Drain(buffer, Receive, 10.048, () => false);
        Check(second.Datagrams == 150 && !second.BudgetHit && link.Latest.Seq == 4250, "next tick reaches the newest");

        // Paused 30 s: the socket filled with the poses sent right after the pause began (newer ones
        // were dropped). The first drain after the gap discards them, over two budget-limited ticks.
        Enqueue(4251, 4471);
        calls = 0;
        var gap = link.Drain(buffer, Receive, 40.0, () => ++calls >= 150);
        Check(gap.Discarded == 150 && gap.BudgetHit && link.Latest.Seq == 4250, "backlog after a tick gap is discarded, not applied");
        var rest = link.Drain(buffer, Receive, 40.016, () => false);
        Check(rest.Discarded == 71 && !rest.BudgetHit, "discarding continues until the socket is empty");
        Check(!link.HasLatest && !link.IsFresh(40.016), "then the link restarts empty");
        Enqueue(5751, 5751);   // the bridge's current pose
        var fresh = link.Drain(buffer, Receive, 40.032, () => false);
        Check(fresh.Discarded == 0 && link.Latest.Seq == 5751 && link.IsFresh(40.032), "first pose after the gap is a fresh one");
    }

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

        var vector = new PosePacket { NorthM = 12.5, EastM = -3.25, DownM = -20, PitchDeg = -45, RollDeg = 1.5f, YawDeg = 270, HfovDeg = 84 };
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
        Check(c.Bind == "127.0.0.1" && c.Port == 5611, "pose defaults");
        Near(c.TimeoutS, 1.9, "timeout default");
        Near(c.Origin.X, 195, "origin x default"); Near(c.Origin.Y, -933, "origin y default");
        Near(c.Origin.HeadingDeg, 0, "heading default");
        Check(!c.Origin.Z.HasValue, "z probes ground by default");
        Check(c.Scene.Weather == "EXTRASUNNY" && c.Scene.Hour == 12 && c.Scene.FreezeTime, "scene defaults: clear midday, clock frozen");
        Check(c.Warnings.Count == 0, "no warnings for empty file");
    }

    static void ConfigParsing()
    {
        var c = ModConfig.Parse("; comment\r\n[POSE]\r\nBind = 0.0.0.0\r\nport=6000 ; inline\r\ntimeout_s=0.5\r\n"
            + "[origin]\nx=-1000.5\ny=2500\nheading_deg=-30\nz=42.25\n# end\n");
        Check(c.Bind == "0.0.0.0" && c.Port == 6000, "pose parsed");
        Near(c.TimeoutS, 0.5, "timeout parsed");
        Near(c.Origin.X, -1000.5, "x parsed"); Near(c.Origin.Y, 2500, "y parsed");
        Near(c.Origin.HeadingDeg, -30, "heading parsed");
        Check(c.Origin.Z == 42.25, "z parsed");
        Check(c.Warnings.Count == 0, "valid file has no warnings: " + string.Join(" | ", c.Warnings));

        var bad = ModConfig.Parse("[pose]\nbind=localhost\nport=70000\ntimeout_s=0\nfoo=1\n[origin]\nx=abc\ny=nan\nheading_deg=\nz=high\nnot a pair\n[broken\nport=1\n");
        Check(bad.Bind == "127.0.0.1" && bad.Port == 5611, "bad pose values fall back");
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
        Check(bad.Scene.Weather == "EXTRASUNNY" && bad.Scene.Hour == 12 && bad.Scene.FreezeTime, "bad scene values fall back to the defaults");
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
        Check(c.Port == 5611 && !c.Origin.Z.HasValue, "shipped ini matches the defaults");
        Near(c.TimeoutS, new ModConfig().TimeoutS, "shipped timeout equals the default");
        Near(c.Origin.X, 195, "shipped origin x"); Near(c.Origin.Y, -933, "shipped origin y");
        Check(c.Scene.Weather == "EXTRASUNNY" && c.Scene.Hour == 12 && c.Scene.FreezeTime, "shipped scene: clear midday, frozen");
        var d = new ModConfig().Physics;
        Check(c.Physics.Bind == d.Bind && c.Physics.Port == d.Port && c.Physics.BudgetMs == d.BudgetMs && c.Physics.HfovDeg == d.HfovDeg
            && c.Physics.TraceHz == d.TraceHz, "shipped [physics] equals the defaults");
    }

    // The bytes horus_capture.asi reads (gta/common/camera_block.hpp): magic, version, seqlock
    // sequence (even when not writing, +2 per tick), active, pose seq, game frame.
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
        block.Publish(true, 907, 5120);
        block.Publish(true, 908, 5121);
        Check(block.Ticks == 2, "two ticks counted");
        string hex = Convert.ToHexString(Dump()).ToLowerInvariant();
        Check(hex == "4847434d" + "01000000" + "0400000000000000" + "01000000" + "00000000"
            + "8c03000000000000" + "0114000000000000" + new string('0', 48),
            "block bytes after two active ticks: " + hex);
        block.Publish(false, 908, 5122);
        byte[] after = Dump();
        Check(BitConverter.ToUInt64(after, CameraBlock.SequenceOffset) == 6, "third tick: sequence 6 (even)");
        Check(BitConverter.ToUInt32(after, CameraBlock.ActiveOffset) == 0, "inactive tick clears active");
        Check(BitConverter.ToUInt64(after, CameraBlock.GameFrameOffset) == 5122, "game frame updated while inactive");
    }

    // C3: the segments HorusGta shape-tests between consecutive camera positions.
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

    // G2: ArduPilot JSON SITL backend.
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

    static void GimbalFromServo()
    {
        var g = new GimbalServo();   // common.parm: MNT1_PITCH_MIN -135, MAX 45; SERVO10 1100..1900
        ServoFrame F(ushort pwm) { var p = Pwm(16, 1000); p[9] = pwm; return new ServoFrame { FrameRate = 400, Pwm = p }; }
        Near(g.PitchDeg(F(1500)).Value, -45, "centre = -45 deg", 1e-9);
        Near(g.PitchDeg(F(1100)).Value, -135, "servo min = pitch min");
        Near(g.PitchDeg(F(1900)).Value, 45, "servo max = pitch max");
        Near(g.PitchDeg(F(2100)).Value, 45, "beyond servo max clamps");
        Check(g.PitchDeg(F(0)) == null, "undriven channel gives no pitch");
        var all = Pwm(16, 1000); all[8] = 1100; all[9] = 1500; all[10] = 1900;
        var three = new ServoFrame { FrameRate = 400, Pwm = all };
        Near(g.RollDeg(three).Value, -30, "SERVO9 min = roll min (MNT1_ROLL_MIN -30)");
        Near(g.YawDeg(three).Value, 160, "SERVO11 max = yaw max (MNT1_YAW_MAX 160)");
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
        var p = new PosePacket { NorthM = 12, EastM = -7, DownM = -4, HfovDeg = 84 };
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
        var link = new PhysicsLink(body, ned, new GimbalServo(), ned.Origin, 0);
        var queue = new System.Collections.Generic.Queue<ServoFrame?>();
        var replies = new System.Collections.Generic.List<string>();
        ServoFrame Fr(uint n) { var p = Pwm(16, 1000); p[9] = 1500; return new ServoFrame { FrameRate = 400, FrameCount = n, Pwm = p }; }
        ServoFrame? Next() => queue.Count > 0 ? queue.Dequeue() : null;
        void Reply(string s) => replies.Add(s);

        foreach (uint n in new uint[] { 1, 2, 2, 3, 4, 5 }) queue.Enqueue(Fr(n));
        var r = link.Pump(2 / 400.0 + 1e-9, Next, Reply, () => false);
        Check(r.Steps == 3 && r.Resent == 1, $"frames 1,2 stepped, 2 again re-answered, 3 stepped (steps {r.Steps}, resent {r.Resent})");
        Check(replies.Count == 4 && replies[2] == replies[1], "the repeated frame gets the same answer");
        Check(link.LastFrame == 3, "sim stops at the target: frame 4 held");
        Near(link.SimTimeS, 3 / 400.0, "three steps of 1/400 s");
        Near(link.GimbalPitchDeg.Value, -45, "gimbal pitch taken from servo 10");

        r = link.Pump(1.0, Next, Reply, () => false);
        Check(r.Steps == 2 && r.TimedOut && link.LastFrame == 5, "held frame 4 answered first, then 5, then timeout");

        queue.Enqueue(Fr(1));
        r = link.Pump(1.0, Next, Reply, () => false);
        Check(r.Restarts == 1 && r.Steps == 1 && link.LastFrame == 1, "lower frame count = SITL restarted");
        Near(link.SimTimeS, 1 / 400.0, "restart resets the clock");

        queue.Enqueue(Fr(2)); queue.Enqueue(Fr(3));
        r = link.Pump(1.0, Next, Reply, () => true);
        Check(r.Steps == 0 && r.BudgetHit && queue.Count == 2, "an exhausted budget answers nothing");
    }

    // G3: world contact. A fake world: ground plane z = 0 and a wall plane x = 10 facing -X.
    static Contact? FakeRay(Vec3 a, Vec3 b, System.Collections.Generic.List<(Vec3, Vec3)> log)
    {
        log?.Add((a, b));
        double bestT = double.MaxValue; Contact? best = null;
        void Plane(double ta, double tb, Vec3 normal)
        {
            if (ta < 0 || tb >= 0) return;   // only crossings from the open side into the solid
            double t = ta / (ta - tb);
            if (t >= bestT) return;
            bestT = t;
            best = new Contact { Position = a + (b - a) * t, Normal = normal, Entity = 0 };
        }
        Plane(a.Z, b.Z, new Vec3(0, 0, 1));
        Plane(10 - a.X, 10 - b.X, new Vec3(-1, 0, 0));
        return best;
    }

    static void WorldProbeRays()
    {
        var rays = new System.Collections.Generic.List<(Vec3, Vec3)>();
        var probe = new WorldProbe((a, b) => FakeRay(a, b, rays));
        var hit = probe.Sweep(new Vec3(9.5, 0, 5), new Vec3(9.9, 0, 5), 0.122);
        Check(hit.HasValue && Math.Abs(hit.Value.Position.X - 10) < 1e-9 && hit.Value.Normal.X == -1, "motion ray extended by the reach finds the wall 0.1 m past the step");
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
        Check(!probe.Sweep(low, low, 0.122).HasValue && rays.Count == 1 && rays[0].Item2.Z == 0.5 - 0.122, "near the ground: the short down ray every step");
        var rest = new Vec3(0, 0, 0.121);
        Check(probe.Sweep(rest, rest, 0.122).HasValue, "resting: the down ray finds the ground within the reach");
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
        var link = new PhysicsLink(body, ned, new GimbalServo(), ned.Origin, ned.GtaHeadingRad(0));

        // Resting: G2 put the camera at ArduPilot's down 0 above the ground origin, i.e. in the ground (white frame).
        var cam = PoseMapping.Map(link.CameraPose(groundZ, 84), origin, groundZ, 1920, 1080);
        Near(cam.Z, groundZ + frame.RadiusM + 0.001 + frame.CameraMountM.Z, "resting camera at the mount, above the ground", 1e-6);
        var mount = body.ToWorld(frame.CameraMountM);
        Near(cam.X, mount.X, "resting camera x = mount", 1e-6); Near(cam.Y, mount.Y, "resting camera y = mount", 1e-6);

        // Turned, rolled 15 deg right and pitched 10 deg up in the air, servos not driven: the mount
        // moves with the body and the camera is fixed to it.
        body.Place(new Vec3(200, -930, 45), ned.GtaHeadingRad(70 * Math.PI / 180));
        double roll = 15 * Math.PI / 180, nose = 10 * Math.PI / 180;
        body.R = Vec3.Rotate(body.R, body.F, roll); body.U = Vec3.Rotate(body.U, body.F, roll);
        body.F = Vec3.Rotate(body.F, body.R, nose); body.U = Vec3.Rotate(body.U, body.R, nose);
        cam = PoseMapping.Map(link.CameraPose(groundZ, 84), origin, groundZ, 1920, 1080);
        mount = body.ToWorld(frame.CameraMountM);
        Near(cam.X, mount.X, "tilted camera x = mount", 1e-6); Near(cam.Y, mount.Y, "tilted camera y = mount", 1e-6);
        Near(cam.Z, mount.Z, "tilted camera z = mount", 1e-6);
        Check(Math.Abs(mount.Z - (45 + frame.CameraMountM.Z)) > 0.005, "the tilt moves the mount off its level height");
        GtaCameraAxes(cam, out var cr, out var cf, out var cu);
        Near((cr - body.R).Length + (cf - body.F).Length + (cu - body.U).Length, 0, "undriven gimbal: camera axes = body axes", 1e-5);

        // ArduPilot's servo mount levels the camera by turning the servos against the lean: roll
        // -15 (SERVO9 1300 us) undoes the body's 15 deg right roll.
        body.Place(new Vec3(200, -930, 45), ned.GtaHeadingRad(70 * Math.PI / 180));
        body.R = Vec3.Rotate(body.R, body.F, roll); body.U = Vec3.Rotate(body.U, body.F, roll);
        DriveGimbal(link, 1300, 1500, 1500);
        Near(link.GimbalRollDeg.Value, -15, "SERVO9 1300 us = roll -15"); Near(link.GimbalPitchDeg.Value, -45, "SERVO10 1500 us = pitch -45");
        GtaCameraAxes(PoseMapping.Map(link.CameraPose(groundZ, 84), origin, groundZ, 1920, 1080), out cr, out cf, out cu);
        Near(cr.Z, 0, "counter-rolled camera: right axis level", 1e-5);
        Near(Math.Asin(cf.Z) * 180 / Math.PI, -45, "camera looks 45 deg down", 1e-4);
        double bodyHeading = Math.Atan2(-body.F.X, body.F.Y), camHeading = Math.Atan2(-cf.X, cf.Y);
        Near(Math.IEEERemainder(camHeading - bodyHeading, 2 * Math.PI), 0, "camera faces the body's heading", 1e-5);

        // Pan 30 deg right (SERVO11 1575 us) on a level body: the camera heading turns clockwise.
        body.Place(new Vec3(200, -930, 45), ned.GtaHeadingRad(70 * Math.PI / 180));
        DriveGimbal(link, 1500, 1500, 1575);
        Near(link.GimbalYawDeg.Value, 30, "SERVO11 1575 us = yaw 30");
        var pose = link.CameraPose(groundZ, 84);
        Near(pose.YawDeg, 100, "NED camera yaw = body yaw 70 + pan 30", 1e-3);
        Near(pose.PitchDeg, -45, "NED camera pitch", 1e-3); Near(pose.RollDeg, 0, "NED camera roll", 1e-3);

        // Straight down (pitch -90): Euler yaw comes from the camera's right axis instead of its forward axis.
        body.Place(new Vec3(200, -930, 45), ned.GtaHeadingRad(70 * Math.PI / 180));
        DriveGimbal(link, 1500, 1300, 1500);
        pose = link.CameraPose(groundZ, 84);
        Near(pose.PitchDeg, -90, "nadir pitch", 1e-3); Near(pose.YawDeg, 70, "nadir camera keeps the body's yaw", 1e-3);
    }

    /// <summary>One servo frame (motors off) with the given SERVO9/10/11 pulse widths.</summary>
    static void DriveGimbal(PhysicsLink link, ushort roll, ushort pitch, ushort yaw)
    {
        var p = Pwm(16, 1000); p[8] = roll; p[9] = pitch; p[10] = yaw;
        bool sent = false;
        link.Pump(link.SimTimeS + 1, () => { if (sent) return null; sent = true; return new ServoFrame { FrameRate = 400, FrameCount = link.LastFrame + 1, Pwm = p }; }, _ => { }, () => false);
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
}
