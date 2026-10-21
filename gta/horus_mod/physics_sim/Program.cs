using System;
using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using HorusGta;

/// <summary>
/// HorusGta's quad physics over flat ground, without GTA (gta/horus_mod/README.md "Physics").
/// Answers ArduPilot's JSON SITL backend on udp &lt;bind&gt;:9002 in real time and logs the state
/// every 5 s. Usage: horus-gta-physics-sim [--bind 127.0.0.1] [--port 9002] [--budget-ms 10]
/// [--tick-ms 33]: the tick and budget emulate GTA's script tick (pace once per tick, answer for
/// at most the budget), so the measured real-time factor carries over to the game.
/// </summary>
static class Program
{
    static int Main(string[] args)
    {
        string bind = "127.0.0.1";
        int port = 9002;
        double budgetS = 0.010, tickS = 0.033;
        for (int i = 0; i + 1 < args.Length; i += 2)
        {
            string v = args[i + 1];
            switch (args[i])
            {
                case "--bind": bind = v; break;
                case "--port": port = int.Parse(v, CultureInfo.InvariantCulture); break;
                case "--budget-ms": budgetS = double.Parse(v, CultureInfo.InvariantCulture) / 1000; break;
                case "--tick-ms": tickS = double.Parse(v, CultureInfo.InvariantCulture) / 1000; break;
                default: Console.Error.WriteLine($"unknown option {args[i]}"); return 2;
            }
        }

        var frame = new Airframe();
        var body = new QuadBody(frame) { FloorZ = 0 };
        var ned = new NedFrame { Origin = new Vec3(0, 0, frame.RadiusM + 0.001), HeadingDeg = 0 };
        var link = new PhysicsLink(body, ned, new GimbalServo(), ned.Origin, 0);
        Log($"airframe {frame.MassKg} kg, {frame.PropDiameterIn}x{frame.PropPitchIn}x{frame.PropBlades} props, {frame.MotorKv} Kv {frame.BatteryCells}S, " +
            $"max {frame.MaxRpm:F0} rpm, hover throttle {frame.HoverThrottle():F3}; udp {bind}:{port}, tick {tickS * 1000:F0} ms, budget {budgetS * 1000:F0} ms");

        using var socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
        socket.Bind(new IPEndPoint(IPAddress.Parse(bind), port));
        var buffer = new byte[256];
        EndPoint sender = new IPEndPoint(IPAddress.Any, 0);
        ServoFrame? Receive()
        {
            for (;;)
            {
                if (!socket.Poll(5000, SelectMode.SelectRead)) return null;
                int n = socket.ReceiveFrom(buffer, ref sender);
                if (SitlJson.TryDecode(buffer, n, out var f)) return f;
            }
        }
        void Reply(string text) => socket.SendTo(Encoding.ASCII.GetBytes(text), sender);

        var clock = Stopwatch.StartNew();
        double target = 0, lastTick = 0, nextLog = 5, logSim = 0, logReal = 0;
        long logSteps = 0;
        bool connected = false;
        for (;;)
        {
            double now = clock.Elapsed.TotalSeconds;
            double tickStart = now;
            if (connected) target = Math.Min(target + (now - lastTick), link.SimTimeS + 0.1);
            lastTick = now;
            var r = link.Pump(target, Receive, Reply, () => clock.Elapsed.TotalSeconds - tickStart >= budgetS);
            if (r.Steps > 0 && !connected)
            {
                connected = true;
                target = link.SimTimeS;
                Log($"SITL connected: frame rate {link.FrameRate} Hz");
            }
            if (r.Restarts > 0) { Log("SITL restarted: quad back at the start"); target = link.SimTimeS; }
            now = clock.Elapsed.TotalSeconds;
            if (now >= nextLog && connected)
            {
                double real = now - logReal, sim = link.SimTimeS - logSim;
                ned.Euler(body, out double roll, out double pitch, out double yaw);
                Vec3 p = ned.ToNed(body.P - ned.Origin);
                Log(string.Format(CultureInfo.InvariantCulture,
                    "sim {0:F1} s, rtf {1:F3}, {2:F0} steps/s; ned ({3:F2}, {4:F2}, {5:F2}) rpy ({6:F1}, {7:F1}, {8:F1}) rpm {9:F0} {10:F0} {11:F0} {12:F0}{13}",
                    link.SimTimeS, sim / real, (link.TotalSteps - logSteps) / real, p.X, p.Y, p.Z,
                    roll * 180 / Math.PI, pitch * 180 / Math.PI, yaw * 180 / Math.PI,
                    body.Rpm[0], body.Rpm[1], body.Rpm[2], body.Rpm[3], body.InContact ? ", on ground" : ""));
                logReal = now; logSim = link.SimTimeS; logSteps = link.TotalSteps; nextLog = now + 5;
            }
            double rest = tickS - (clock.Elapsed.TotalSeconds - tickStart);
            if (rest > 0) Thread.Sleep(TimeSpan.FromSeconds(rest));
        }
    }

    static void Log(string message) =>
        Console.WriteLine($"{DateTime.Now:HH:mm:ss.fff} {message}");
}
