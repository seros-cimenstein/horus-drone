using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net;

namespace HorusGta
{
    /// <summary>GTA world placement of SITL home.</summary>
    public struct Origin
    {
        public double X, Y;
        /// <summary>GTA heading of NED north (counter-clockwise from +Y).</summary>
        public double HeadingDeg;
        /// <summary>GTA height of home; null probes the ground height at X, Y.</summary>
        public double? Z;
    }

    /// <summary>Scene applied while the drone camera is active; null leaves the game's own.</summary>
    public struct Scene
    {
        /// <summary>GTA weather type name (upper case), e.g. EXTRASUNNY; null = the game's weather.</summary>
        public string Weather;
        /// <summary>Clock hour 0..23 set on activation; null = the game's time.</summary>
        public int? Hour;
        /// <summary>Stop the game clock while active.</summary>
        public bool FreezeTime;
    }

    /// <summary>The ArduPilot JSON physics link (README "Physics").</summary>
    public struct PhysicsSettings
    {
        /// <summary>UDP endpoint SITL's JSON backend sends its servo frames to.</summary>
        public string Bind;
        public int Port;
        /// <summary>Wall time per script tick spent answering SITL frames (ms).</summary>
        public double BudgetMs;
        /// <summary>Horizontal FOV of the drone camera (deg), as executor.camera_horizontal_fov_deg.</summary>
        public double HfovDeg;
        /// <summary>Rows per second of HorusGtaTrace.csv (camera and drone pose read back from the game); 0 = off.</summary>
        public double TraceHz;
    }

    /// <summary>
    /// HorusGta.ini: [pose] bind, port, timeout_s; [origin] x, y, heading_deg, optional z;
    /// [scene] weather, hour, freeze_time (empty = leave the game's); [physics] bind, port,
    /// budget_ms, hfov_deg, trace_hz.
    /// </summary>
    public sealed class ModConfig
    {
        /// <summary>Weather types SET_WEATHER_TYPE_NOW_PERSIST accepts.</summary>
        public static readonly string[] WeatherTypes =
        {
            "EXTRASUNNY", "CLEAR", "NEUTRAL", "SMOG", "FOGGY", "OVERCAST", "CLOUDS", "CLEARING", "RAIN",
            "THUNDER", "SNOW", "BLIZZARD", "SNOWLIGHT", "XMAS", "HALLOWEEN", "RAIN_HALLOWEEN", "SNOW_HALLOWEEN",
        };

        public string Bind = "127.0.0.1";
        public int Port = 5611;
        /// <summary>
        /// Release after this long without a valid pose. Below 2 s on purpose: the release happens on
        /// the first script tick past the timeout, and the packet's arrival waits for a tick too, so
        /// the screen comes back up to about two ticks (30–40 ms each) after timeout_s. 1.9 s keeps
        /// that within 2 s of the bridge's last packet (gta/horus_mod/README.md "Mod").
        /// </summary>
        public double TimeoutS = 1.9;
        /// <summary>Legion Square, Los Santos.</summary>
        public Origin Origin = new Origin { X = 195.0, Y = -933.0, HeadingDeg = 0.0, Z = null };
        /// <summary>Clear midday light that stays put, so perception sees the same scene every run.</summary>
        public Scene Scene = new Scene { Weather = "EXTRASUNNY", Hour = 12, FreezeTime = true };
        /// <summary>
        /// SITL's JSON default port; 10 ms of a ~33 ms tick, about 30 % of the game frame, for the
        /// lockstep answers (800 Hz needs ~27 per tick in real time); HORUS's 84° camera.
        /// </summary>
        public PhysicsSettings Physics = new PhysicsSettings { Bind = "127.0.0.1", Port = 9002, BudgetMs = 10, HfovDeg = 84, TraceHz = 0 };
        /// <summary>Problems found while parsing; each falls back to the default.</summary>
        public readonly List<string> Warnings = new List<string>();

        public static ModConfig Parse(string text)
        {
            var cfg = new ModConfig();
            string section = "";
            var lines = (text ?? "").Split('\n');
            for (int n = 0; n < lines.Length; n++)
            {
                string line = lines[n].Trim();
                if (line.Length == 0 || line[0] == ';' || line[0] == '#') continue;
                if (line[0] == '[')
                {
                    int end = line.IndexOf(']');
                    if (end < 0) { cfg.Warn(n, "unterminated section header", usesDefault: false); section = "?"; continue; }
                    section = line.Substring(1, end - 1).Trim().ToLowerInvariant();
                    continue;
                }
                int eq = line.IndexOf('=');
                if (eq <= 0) { cfg.Warn(n, $"expected key=value, got '{line}'", usesDefault: false); continue; }
                string key = line.Substring(0, eq).Trim().ToLowerInvariant();
                string value = StripComment(line.Substring(eq + 1)).Trim();
                cfg.Set(n, section, key, value);
            }
            return cfg;
        }

        void Set(int n, string section, string key, string value)
        {
            switch (section + "." + key)
            {
                case "pose.bind":
                    if (IPAddress.TryParse(value, out var ip) && ip.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork) Bind = value;
                    else Warn(n, $"pose.bind '{value}' is not an IPv4 address");
                    break;
                case "pose.port":
                    if (int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out int port) && port >= 1 && port <= 65535) Port = port;
                    else Warn(n, $"pose.port '{value}' is not 1..65535");
                    break;
                case "pose.timeout_s":
                    if (TryNumber(value, out double t) && t > 0) TimeoutS = t;
                    else Warn(n, $"pose.timeout_s '{value}' is not a positive number");
                    break;
                case "origin.x":
                    if (TryNumber(value, out double x)) Origin.X = x; else Warn(n, $"origin.x '{value}' is not a number");
                    break;
                case "origin.y":
                    if (TryNumber(value, out double y)) Origin.Y = y; else Warn(n, $"origin.y '{value}' is not a number");
                    break;
                case "origin.heading_deg":
                    if (TryNumber(value, out double hd)) Origin.HeadingDeg = hd; else Warn(n, $"origin.heading_deg '{value}' is not a number");
                    break;
                case "origin.z":
                    if (value.Length == 0) Origin.Z = null;
                    else if (TryNumber(value, out double z)) Origin.Z = z;
                    else Warn(n, $"origin.z '{value}' is not a number (leave it empty to probe the ground)");
                    break;
                case "scene.weather":
                    if (value.Length == 0) Scene.Weather = null;
                    else if (Array.IndexOf(WeatherTypes, value.ToUpperInvariant()) >= 0) Scene.Weather = value.ToUpperInvariant();
                    else Warn(n, $"scene.weather '{value}' is not one of {string.Join(", ", WeatherTypes)} (leave it empty for the game's weather)");
                    break;
                case "scene.hour":
                    if (value.Length == 0) Scene.Hour = null;
                    else if (int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out int hour) && hour >= 0 && hour <= 23) Scene.Hour = hour;
                    else Warn(n, $"scene.hour '{value}' is not 0..23 (leave it empty for the game's time)");
                    break;
                case "scene.freeze_time":
                    if (value.Length == 0 || value.Equals("false", StringComparison.OrdinalIgnoreCase)) Scene.FreezeTime = false;
                    else if (value.Equals("true", StringComparison.OrdinalIgnoreCase)) Scene.FreezeTime = true;
                    else Warn(n, $"scene.freeze_time '{value}' is not true or false");
                    break;
                case "physics.bind":
                    if (IPAddress.TryParse(value, out var pip) && pip.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork) Physics.Bind = value;
                    else Warn(n, $"physics.bind '{value}' is not an IPv4 address");
                    break;
                case "physics.port":
                    if (int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out int pport) && pport >= 1 && pport <= 65535) Physics.Port = pport;
                    else Warn(n, $"physics.port '{value}' is not 1..65535");
                    break;
                case "physics.budget_ms":
                    if (TryNumber(value, out double budget) && budget > 0 && budget <= 30) Physics.BudgetMs = budget;
                    else Warn(n, $"physics.budget_ms '{value}' is not in (0, 30]");
                    break;
                case "physics.hfov_deg":
                    if (TryNumber(value, out double hfov) && hfov >= 1 && hfov <= 170) Physics.HfovDeg = hfov;
                    else Warn(n, $"physics.hfov_deg '{value}' is not 1..170");
                    break;
                case "physics.trace_hz":
                    if (TryNumber(value, out double trace) && trace >= 0 && trace <= 100) Physics.TraceHz = trace;
                    else Warn(n, $"physics.trace_hz '{value}' is not 0..100");
                    break;
                default:
                    Warn(n, $"unknown setting [{section}] {key}", usesDefault: false);
                    break;
            }
        }

        static string StripComment(string v)
        {
            int i = v.IndexOfAny(new[] { ';', '#' });
            return i < 0 ? v : v.Substring(0, i);
        }

        static bool TryNumber(string v, out double d) =>
            double.TryParse(v, NumberStyles.Float, CultureInfo.InvariantCulture, out d) && !double.IsNaN(d) && !double.IsInfinity(d);

        void Warn(int n, string message, bool usesDefault = true) =>
            Warnings.Add($"HorusGta.ini line {n + 1}: {message}{(usesDefault ? "; using the default" : "; ignored")}");
    }
}
