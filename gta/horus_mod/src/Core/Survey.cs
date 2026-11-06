using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

namespace HorusGta
{
    /// <summary>
    /// A survey request (<c>scripts\HorusGtaSurvey.request.json</c>, README "Survey"):
    /// <c>{"box": [min_n, min_e, max_n, max_e], "cell_m": 5, "output": "HorusGtaSurvey"}</c>, metres
    /// NED from SITL home as in the map tile index's <c>bounds</c>, plus optional
    /// <c>settle_s</c>, <c>load_timeout_s</c> and <c>load_attempts</c>. Unknown keys are refused.
    /// </summary>
    public sealed class SurveyRequest
    {
        public const double MaxAbsM = 20000;
        public double MinN, MinE, MaxN, MaxE;
        public double CellM = 5;
        /// <summary>Output folder, one plain name inside the scripts folder.</summary>
        public string Output = "HorusGtaSurvey";
        /// <summary>Seconds to keep the tile streamed after its probes pass, before casting.</summary>
        public double SettleS = 1.0;
        /// <summary>Seconds one load attempt waits for the probes.</summary>
        public double LoadTimeoutS = 10;
        /// <summary>Load attempts before the tile is listed as unloaded.</summary>
        public int LoadAttempts = 3;

        public static bool TryParse(string json, out SurveyRequest request, out string error)
        {
            request = null;
            error = null;
            object root;
            try { root = MiniJson.Parse(json); }
            catch (FormatException e) { error = "not JSON: " + e.Message; return false; }
            if (!(root is Dictionary<string, object> o)) { error = "not a JSON object"; return false; }
            var r = new SurveyRequest();
            foreach (var kv in o)
            {
                switch (kv.Key)
                {
                    case "box":
                        {
                            if (!(kv.Value is List<object> b) || b.Count != 4 || !b.TrueForAll(v => v is double d && Math.Abs(d) <= MaxAbsM))
                            {
                                error = $"box must be [min_n, min_e, max_n, max_e], metres within ±{MaxAbsM:g}";
                                return false;
                            }
                            r.MinN = (double)b[0]; r.MinE = (double)b[1]; r.MaxN = (double)b[2]; r.MaxE = (double)b[3];
                            if (!(r.MinN < r.MaxN && r.MinE < r.MaxE)) { error = "box must have min_n < max_n and min_e < max_e"; return false; }
                            break;
                        }
                    case "cell_m":
                        if (!(kv.Value is double c) || !SurveyGrid.TryCellsPerSide(c, out _))
                        {
                            error = $"cell_m must divide the {SurveyGrid.TileM:g} m tile into {SurveyGrid.MinCells}..{SurveyGrid.MaxCells} cells (5 is the survey's size)";
                            return false;
                        }
                        r.CellM = c;
                        break;
                    case "output":
                        if (!(kv.Value is string s) || !PlainName(s)) { error = "output must be one folder name of letters, digits, '.', '_' or '-' starting with a letter or digit"; return false; }
                        r.Output = s;
                        break;
                    case "settle_s":
                        if (!(kv.Value is double settle) || settle < 0 || settle > 30) { error = "settle_s must be 0..30"; return false; }
                        r.SettleS = settle;
                        break;
                    case "load_timeout_s":
                        if (!(kv.Value is double lt) || lt < SurveyRun.WaterWaitS || lt > 120) { error = $"load_timeout_s must be {SurveyRun.WaterWaitS:g}..120"; return false; }
                        r.LoadTimeoutS = lt;
                        break;
                    case "load_attempts":
                        if (!(kv.Value is double la) || la != Math.Floor(la) || la < 1 || la > 10) { error = "load_attempts must be a whole number 1..10"; return false; }
                        r.LoadAttempts = (int)la;
                        break;
                    default:
                        error = $"unknown key \"{kv.Key}\"";
                        return false;
                }
            }
            if (!o.ContainsKey("box")) { error = "box is required"; return false; }
            request = r;
            return true;
        }

        static bool PlainName(string s)
        {
            if (s.Length == 0 || s.Length > 64 || !char.IsLetterOrDigit(s[0]) || s[0] > 127) return false;
            foreach (char c in s)
                if (!(c < 128 && (char.IsLetterOrDigit(c) || c == '.' || c == '_' || c == '-'))) return false;
            return true;
        }
    }

    public struct SurveyTileKey
    {
        public int X, Y;
        public SurveyTileKey(int x, int y) { X = x; Y = y; }
        public override string ToString() => X.ToString(CultureInfo.InvariantCulture) + "_" + Y.ToString(CultureInfo.InvariantCulture);
    }

    /// <summary>
    /// The survey grid is the map tile grid (<c>horus/maps/tiles.py</c> level 0): tile (x, y) covers
    /// east [x·250, (x+1)·250) and north [y·250, (y+1)·250) metres from SITL home. Cell (i, j) of a
    /// tile is column i east from its west edge and row j north from its south edge; its centre is
    /// east x·250 + (i + ½)·cell, north y·250 + (j + ½)·cell, stored at index k = j·n + i.
    /// </summary>
    public static class SurveyGrid
    {
        public const double TileM = 250;
        public const int MinCells = 5, MaxCells = 250;
        /// <summary>Each cell's ray goes straight down from this GTA z to that one.</summary>
        public const double RayTopZ = 1500, RayBottomZ = -50;

        public static bool TryCellsPerSide(double cellM, out int n)
        {
            n = 0;
            if (!(cellM > 0) || double.IsInfinity(cellM)) return false;
            double q = TileM / cellM;
            n = (int)Math.Round(q);
            return Math.Abs(q - n) < 1e-9 && n >= MinCells && n <= MaxCells;
        }

        /// <summary>
        /// Every tile the box touches (as <c>tiles_touched</c>), row by row from the south, west to
        /// east and back on alternate rows, so the streaming focus moves one tile at a time.
        /// </summary>
        public static List<SurveyTileKey> Tiles(double minN, double minE, double maxN, double maxE)
        {
            int x0 = (int)Math.Floor(minE / TileM), x1 = (int)Math.Floor(maxE / TileM);
            int y0 = (int)Math.Floor(minN / TileM), y1 = (int)Math.Floor(maxN / TileM);
            var tiles = new List<SurveyTileKey>((x1 - x0 + 1) * (y1 - y0 + 1));
            for (int y = y0; y <= y1; y++)
            {
                bool east = (y - y0) % 2 == 0;
                for (int k = 0; k <= x1 - x0; k++) tiles.Add(new SurveyTileKey(east ? x0 + k : x1 - k, y));
            }
            return tiles;
        }

        public static void CellCentreNed(SurveyTileKey t, int i, int j, double cellM, out double northM, out double eastM)
        {
            eastM = t.X * TileM + (i + 0.5) * cellM;
            northM = t.Y * TileM + (j + 0.5) * cellM;
        }

        /// <summary>GTA world x, y of a cell centre (<see cref="PoseMapping.NedToGta"/>).</summary>
        public static void CellCentreGta(SurveyTileKey t, int i, int j, double cellM, Origin origin, out double x, out double y)
        {
            CellCentreNed(t, i, j, cellM, out double n, out double e);
            PoseMapping.NedToGta(n, e, origin, out x, out y);
        }

        /// <summary>GTA world x, y of a point given in metres east and north of the tile's south-west corner.</summary>
        public static void TilePointGta(SurveyTileKey t, double eastInM, double northInM, Origin origin, out double x, out double y) =>
            PoseMapping.NedToGta(t.Y * TileM + northInM, t.X * TileM + eastInM, origin, out x, out y);
    }

    /// <summary>
    /// One surveyed tile, <c>&lt;x&gt;_&lt;y&gt;.bin</c>, little-endian: magic "HSRV" | u16 version 1 |
    /// u16 n (cells per side) | i32 tile x | i32 tile y | f32 cell_m | u32 cells hit | n·n × i16 GTA z
    /// of the top surface in decimetres (k = j·n + i, <see cref="SurveyGrid"/>), <see cref="NoHit"/>
    /// where the ray hit nothing.
    /// </summary>
    public static class SurveyTile
    {
        public const int HeaderSize = 24;
        public const ushort Version = 1;
        public const short NoHit = short.MinValue;
        static readonly byte[] Magic = { (byte)'H', (byte)'S', (byte)'R', (byte)'V' };

        public static string FileName(SurveyTileKey t) => t + ".bin";

        /// <summary>GTA z in decimetres, rounded half away from zero and clamped to the i16 range above <see cref="NoHit"/>; null is <see cref="NoHit"/>.</summary>
        public static short Decimetres(double? z)
        {
            if (!z.HasValue || double.IsNaN(z.Value)) return NoHit;
            double dm = Math.Round(z.Value * 10, MidpointRounding.AwayFromZero);
            return (short)Math.Max(short.MinValue + 1, Math.Min(short.MaxValue, dm));
        }

        public static byte[] Encode(SurveyTileKey t, int n, double cellM, short[] heights)
        {
            if (heights.Length != n * n) throw new ArgumentException($"need {n * n} heights", nameof(heights));
            var b = new byte[HeaderSize + 2 * heights.Length];
            Array.Copy(Magic, b, Magic.Length);
            b[4] = (byte)Version; b[5] = (byte)(Version >> 8);
            b[6] = (byte)n; b[7] = (byte)(n >> 8);
            RangeProtocol.WriteU32(b, 8, unchecked((uint)t.X));
            RangeProtocol.WriteU32(b, 12, unchecked((uint)t.Y));
            RangeProtocol.WriteU32(b, 16, RangeProtocol.F32Bits((float)cellM));
            uint hits = 0;
            for (int k = 0; k < heights.Length; k++)
            {
                short h = heights[k];
                if (h != NoHit) hits++;
                b[HeaderSize + 2 * k] = (byte)h;
                b[HeaderSize + 2 * k + 1] = (byte)(h >> 8);
            }
            RangeProtocol.WriteU32(b, 20, hits);
            return b;
        }

        /// <summary>Decode a tile file; false (with the reason) for a wrong magic, version, size or hit count.</summary>
        public static bool TryDecode(byte[] b, out SurveyTileKey t, out int n, out double cellM, out short[] heights, out string error)
        {
            t = default; n = 0; cellM = 0; heights = null; error = null;
            if (b == null || b.Length < HeaderSize) { error = "shorter than the header"; return false; }
            for (int i = 0; i < Magic.Length; i++) if (b[i] != Magic[i]) { error = "not an HSRV tile"; return false; }
            int version = b[4] | b[5] << 8;
            if (version != Version) { error = $"version {version}, not {Version}"; return false; }
            n = b[6] | b[7] << 8;
            if (b.Length != HeaderSize + 2 * n * n) { error = $"{b.Length} bytes, not {HeaderSize + 2 * n * n} for {n}x{n} cells"; return false; }
            t = new SurveyTileKey(unchecked((int)RangeProtocol.ReadU32(b, 8)), unchecked((int)RangeProtocol.ReadU32(b, 12)));
            cellM = RangeProtocol.F32(RangeProtocol.ReadU32(b, 16));
            heights = new short[n * n];
            uint hits = 0;
            for (int k = 0; k < heights.Length; k++)
            {
                heights[k] = (short)(b[HeaderSize + 2 * k] | b[HeaderSize + 2 * k + 1] << 8);
                if (heights[k] != NoHit) hits++;
            }
            if (hits != RangeProtocol.ReadU32(b, 20)) { error = "hit count does not match the cells"; return false; }
            return true;
        }

        /// <summary>Write <paramref name="path"/> through a temporary file and a rename, so a reader (or a resumed survey) never sees half a file.</summary>
        public static void WriteAtomic(string path, byte[] data)
        {
            string tmp = path + ".tmp";
            using (var f = new FileStream(tmp, FileMode.Create, FileAccess.Write, FileShare.None))
            {
                f.Write(data, 0, data.Length);
                f.Flush(true);
            }
            if (File.Exists(path)) File.Replace(tmp, path, null);
            else File.Move(tmp, path);
        }
    }

    /// <summary>
    /// The survey, one script tick at a time (README "Survey"). Per tile: move the streaming focus
    /// there and request collision (<see cref="KeepLoaded"/>, every tick until the tile is written),
    /// wait until five probe rays (centre and quadrant centres) each hit something or stand on
    /// water, with at least one hit or <see cref="WaterWaitS"/> passed; keep it loaded
    /// <see cref="SurveyRequest.SettleS"/> longer; then cast one ray down per cell within the tick
    /// budget and write the tile atomically. Probes that do not pass within
    /// <see cref="SurveyRequest.LoadTimeoutS"/> start another attempt; after
    /// <see cref="SurveyRequest.LoadAttempts"/> the tile is listed as unloaded and gets no file, so
    /// the next run retries it. Valid tile files already in the folder are skipped (resume).
    /// The progress file is rewritten atomically after every tile and every state change.
    /// </summary>
    public sealed class SurveyRun
    {
        public const string FormatName = "horus-gta-survey";
        public const int FormatVersion = 1;
        public const string MetadataFile = "survey.json";
        public const string FlagsText = "Map|Objects|Foliage|Glass";
        /// <summary>Probe points that all stand on water count as loaded only after this long (land in the tile may still be loading).</summary>
        public const double WaterWaitS = 3.0;
        const double QuadrantM = SurveyGrid.TileM / 4;
        const double DefaultFocusZ = 50;

        /// <summary>GTA x, y: z of the first <see cref="FlagsText"/> hit straight down from <see cref="SurveyGrid.RayTopZ"/> to <see cref="SurveyGrid.RayBottomZ"/>, or null.</summary>
        public Func<double, double, double?> CastDown;
        /// <summary>GTA x, y: true when there is water there.</summary>
        public Func<double, double, bool> IsWater;
        /// <summary>GTA x, y, z of the tile centre (z: the ground the centre probe last hit): focus the streaming there and request collision.</summary>
        public Action<double, double, double> KeepLoaded;
        /// <summary>Wall clock in seconds: the cast budget and the ray cost.</summary>
        public Func<double> Clock;
        public Action<string> Log = _ => { };

        public readonly SurveyRequest Request;
        public readonly string Folder, ProgressPath;
        public readonly int CellsPerSide;
        public readonly List<SurveyTileKey> Tiles;
        public readonly List<SurveyTileKey> Unloaded = new List<SurveyTileKey>();
        public string State { get; private set; } = "starting";
        public string Message { get; private set; }
        public int TilesSkipped { get; private set; }
        public int TilesWritten { get; private set; }
        public long Rays { get; private set; }
        public long ProbeRays { get; private set; }
        public double CastSeconds { get; private set; }

        readonly Origin origin;
        readonly double homeGroundZ, restAboveGroundM;
        readonly string homeGroundSource;
        readonly bool[] done;
        readonly short[] heights;
        enum Phase { Start, Load, Settle, Cast }
        Phase phase;
        int index, attempt, cursor;
        double startedS, tileStartS, attemptStartS, settleStartS, tileSecondsSum;
        double focusZ = DefaultFocusZ;

        /// <param name="scriptsDir">The folder holding the request; <see cref="SurveyRequest.Output"/> and the progress file go there.</param>
        /// <param name="homeGroundZ">GTA z of the ground at [origin], as the physics link uses it.</param>
        /// <param name="homeGroundSource">"probe" (GET_GROUND_Z_FOR_3D_COORD) or "ini" ([origin] z).</param>
        /// <param name="restAboveGroundM">Height of the resting body centre (HORUS up 0) above that ground.</param>
        public SurveyRun(SurveyRequest request, Origin origin, double homeGroundZ, string homeGroundSource, double restAboveGroundM,
            string scriptsDir, string progressPath)
        {
            Request = request;
            this.origin = origin;
            this.homeGroundZ = homeGroundZ;
            this.homeGroundSource = homeGroundSource;
            this.restAboveGroundM = restAboveGroundM;
            Folder = Path.Combine(scriptsDir, request.Output);
            ProgressPath = progressPath;
            SurveyGrid.TryCellsPerSide(request.CellM, out CellsPerSide);
            Tiles = SurveyGrid.Tiles(request.MinN, request.MinE, request.MaxN, request.MaxE);
            done = new bool[Tiles.Count];
            heights = new short[CellsPerSide * CellsPerSide];
        }

        public int TilesDone { get { int c = 0; foreach (bool d in done) if (d) c++; return c; } }

        /// <summary>Check or write the metadata and find the tiles already surveyed; false (state "error") when the folder holds a different survey.</summary>
        public bool Begin(double now)
        {
            startedS = now;
            try
            {
                Directory.CreateDirectory(Folder);
                string meta = Path.Combine(Folder, MetadataFile);
                if (File.Exists(meta))
                {
                    string why = Mismatch(File.ReadAllText(meta));
                    if (why != null)
                        return Fail(now, $"{Request.Output}/{MetadataFile} holds another survey ({why}); use a new output folder");
                }
                SurveyTile.WriteAtomic(meta, Encoding.UTF8.GetBytes(Metadata()));
                for (int i = 0; i < Tiles.Count; i++)
                {
                    string path = Path.Combine(Folder, SurveyTile.FileName(Tiles[i]));
                    if (!File.Exists(path)) continue;
                    if (SurveyTile.TryDecode(File.ReadAllBytes(path), out var t, out int n, out double cell, out _, out string error)
                        && t.X == Tiles[i].X && t.Y == Tiles[i].Y && n == CellsPerSide && cell == (float)Request.CellM)
                    {
                        done[i] = true;
                        TilesSkipped++;
                        continue;
                    }
                    Log($"survey: {SurveyTile.FileName(Tiles[i])} is not a valid tile ({error ?? "another tile or cell size"}); surveying it again");
                    File.Delete(path);
                }
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
            {
                return Fail(now, $"cannot use the output folder {Folder}: {e.Message}");
            }
            State = "running";
            Log(string.Format(CultureInfo.InvariantCulture,
                "survey: {0} tiles of {1:g} m at {2:g} m cells in box n {3:g}..{4:g} e {5:g}..{6:g} -> {7}; {8} already surveyed; home ground z {9:F2} ({10})",
                Tiles.Count, SurveyGrid.TileM, Request.CellM, Request.MinN, Request.MaxN, Request.MinE, Request.MaxE, Folder, TilesSkipped,
                homeGroundZ, homeGroundSource));
            WriteProgress(now);
            return true;
        }

        /// <summary>
        /// One script tick: load, settle or cast the current tile, at most <paramref name="budgetS"/>
        /// seconds of casting (at least one ray). False once the survey is over (state "done").
        /// </summary>
        public bool Step(double now, double budgetS)
        {
            if (State != "running") return State == "suspended";
            while (index < Tiles.Count && done[index]) index++;
            if (index >= Tiles.Count)
            {
                State = "done";
                Message = Unloaded.Count == 0 ? null : $"{Unloaded.Count} tile(s) unloaded; run the survey again to retry them";
                Log(string.Format(CultureInfo.InvariantCulture, "survey: done, {0} tiles written this run, {1} skipped, {2} unloaded, {3} rays ({4})",
                    TilesWritten, TilesSkipped, Unloaded.Count, Rays, RateText()));
                WriteProgress(now);
                return false;
            }
            var tile = Tiles[index];
            if (phase == Phase.Start)
            {
                phase = Phase.Load;
                attempt = 0;
                tileStartS = attemptStartS = now;
            }
            SurveyGrid.TilePointGta(tile, SurveyGrid.TileM / 2, SurveyGrid.TileM / 2, origin, out double cx, out double cy);
            KeepLoaded(cx, cy, focusZ);
            if (phase == Phase.Load)
            {
                if (Probe(tile, now))
                {
                    phase = Phase.Settle;
                    settleStartS = now;
                }
                else if (now - attemptStartS >= Request.LoadTimeoutS)
                {
                    attempt++;
                    if (attempt >= Request.LoadAttempts)
                    {
                        Unloaded.Add(tile);
                        Log(string.Format(CultureInfo.InvariantCulture, "survey: tile {0} not loaded after {1} attempts of {2:g} s; listed as unloaded",
                            tile, attempt, Request.LoadTimeoutS));
                        NextTile(now);
                    }
                    else attemptStartS = now;
                }
                return true;
            }
            if (phase == Phase.Settle)
            {
                if (now - settleStartS < Request.SettleS) return true;
                phase = Phase.Cast;
                cursor = 0;
            }
            Cast(tile, budgetS);
            if (cursor < heights.Length) return true;
            try { SurveyTile.WriteAtomic(Path.Combine(Folder, SurveyTile.FileName(tile)), SurveyTile.Encode(tile, CellsPerSide, Request.CellM, heights)); }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
            {
                Fail(now, $"cannot write tile {tile}: {e.Message}");
                return false;
            }
            done[index] = true;
            TilesWritten++;
            tileSecondsSum += now - tileStartS;
            NextTile(now);
            return true;
        }

        /// <summary>SITL connected (or anything else that takes the streaming focus): stop where it is; the current tile starts again on <see cref="Resume"/>.</summary>
        public void Suspend(double now, string why)
        {
            if (State != "running") return;
            State = "suspended";
            Message = why;
            phase = Phase.Start;
            Log($"survey: suspended ({why}); tile {(index < Tiles.Count ? Tiles[index].ToString() : "-")} starts again on resume");
            WriteProgress(now);
        }

        public void Resume(double now)
        {
            if (State != "suspended") return;
            State = "running";
            Message = null;
            Log("survey: resumed");
            WriteProgress(now);
        }

        /// <summary>End the run early (request removed, script aborted); written tiles stay, the next run resumes after them.</summary>
        public void Stop(double now, string why)
        {
            if (State != "running" && State != "suspended") return;
            State = "stopped";
            Message = why;
            Log($"survey: stopped ({why}) after {TilesDone} of {Tiles.Count} tiles");
            WriteProgress(now);
        }

        bool Probe(SurveyTileKey tile, double now)
        {
            int hits = 0;
            for (int p = 0; p < 5; p++)
            {
                double e = p == 0 ? 2 * QuadrantM : (p == 1 || p == 3 ? QuadrantM : 3 * QuadrantM);
                double n = p == 0 ? 2 * QuadrantM : (p <= 2 ? QuadrantM : 3 * QuadrantM);
                SurveyGrid.TilePointGta(tile, e, n, origin, out double x, out double y);
                ProbeRays++;
                double? z = CastDown(x, y);
                if (z.HasValue)
                {
                    hits++;
                    if (p == 0) focusZ = z.Value;
                }
                else if (!IsWater(x, y)) return false;
                else if (p == 0) focusZ = 0;
            }
            return hits > 0 || now - attemptStartS >= WaterWaitS;
        }

        void Cast(SurveyTileKey tile, double budgetS)
        {
            double t0 = Clock(), t;
            do
            {
                int i = cursor % CellsPerSide, j = cursor / CellsPerSide;
                SurveyGrid.CellCentreGta(tile, i, j, Request.CellM, origin, out double x, out double y);
                heights[cursor] = SurveyTile.Decimetres(CastDown(x, y));
                Rays++;
                cursor++;
                t = Clock();
            } while (cursor < heights.Length && t - t0 < budgetS);
            CastSeconds += t - t0;
        }

        void NextTile(double now)
        {
            index++;
            phase = Phase.Start;
            WriteProgress(now);
        }

        bool Fail(double now, string why)
        {
            State = "error";
            Message = why;
            Log("survey: " + why);
            WriteProgress(now);
            return false;
        }

        /// <summary>One line for the mod's log: tiles, unloaded, rays and their rate.</summary>
        public string Summary() => string.Format(CultureInfo.InvariantCulture,
            "{0}, {1} of {2} tiles done ({3} written this run, {4} skipped), {5} unloaded, {6} rays, {7}",
            State, TilesDone, Tiles.Count, TilesWritten, TilesSkipped, Unloaded.Count, Rays, RateText());

        string RateText() => Rays == 0 || CastSeconds <= 0 ? "no rays timed"
            : string.Format(CultureInfo.InvariantCulture, "{0:F0} rays/s while casting, {1:F3} ms per ray", Rays / CastSeconds, CastSeconds * 1000 / Rays);

        /// <summary>Null when the stored metadata describes the same grid, origin and rays; else what differs.</summary>
        string Mismatch(string json)
        {
            Dictionary<string, object> m, o, ray;
            try
            {
                m = MiniJson.Parse(json) as Dictionary<string, object>;
                o = m?["origin"] as Dictionary<string, object>;
                ray = m?["ray"] as Dictionary<string, object>;
            }
            catch (Exception e) when (e is FormatException || e is KeyNotFoundException) { return "unreadable: " + e.Message; }
            if (m == null || o == null || ray == null) return "unreadable";
            bool Same(Dictionary<string, object> d, string key, object want) =>
                d.TryGetValue(key, out object v) && (want is double w ? v is double x && x == w : Equals(v, want));
            if (!Same(m, "format", FormatName) || !Same(m, "version", (double)FormatVersion)) return "another format";
            if (!Same(m, "tile_m", SurveyGrid.TileM) || !Same(m, "cell_m", Request.CellM)) return "another cell size";
            if (!Same(o, "x", origin.X) || !Same(o, "y", origin.Y) || !Same(o, "heading_deg", origin.HeadingDeg)) return "another origin";
            if (!Same(ray, "top_z", SurveyGrid.RayTopZ) || !Same(ray, "bottom_z", SurveyGrid.RayBottomZ) || !Same(ray, "flags", FlagsText)) return "other rays";
            return null;
        }

        string Metadata()
        {
            string N(double v) => MiniJson.Number(v);
            return "{\n"
                + $"  \"format\": {MiniJson.Quote(FormatName)},\n  \"version\": {FormatVersion},\n"
                + $"  \"tile_m\": {N(SurveyGrid.TileM)},\n  \"cell_m\": {N(Request.CellM)},\n  \"cells_per_side\": {CellsPerSide},\n"
                + $"  \"box\": [{N(Request.MinN)}, {N(Request.MinE)}, {N(Request.MaxN)}, {N(Request.MaxE)}],\n"
                + $"  \"origin\": {{\"x\": {N(origin.X)}, \"y\": {N(origin.Y)}, \"heading_deg\": {N(origin.HeadingDeg)}}},\n"
                + $"  \"home_ground_z\": {N(homeGroundZ)},\n  \"home_ground_source\": {MiniJson.Quote(homeGroundSource)},\n"
                + $"  \"rest_above_ground_m\": {N(restAboveGroundM)},\n"
                + $"  \"ray\": {{\"top_z\": {N(SurveyGrid.RayTopZ)}, \"bottom_z\": {N(SurveyGrid.RayBottomZ)}, \"flags\": {MiniJson.Quote(FlagsText)}, \"ignores\": \"vehicles, peds, water, the player\"}},\n"
                + "  \"tile_file\": {\"name\": \"<x>_<y>.bin\", \"header_bytes\": 24, \"header\": \"HSRV, u16 version, u16 n, i32 x, i32 y, f32 cell_m, u32 cells hit\", "
                + "\"cells\": \"n*n int16 little-endian, k = j*n + i, i east from the west edge, j north from the south edge\", \"unit\": \"decimetre of GTA z\", "
                + $"\"no_hit\": {SurveyTile.NoHit}}},\n"
                + $"  \"started_utc\": {MiniJson.Quote(DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture))}\n"
                + "}\n";
        }

        void WriteProgress(double now)
        {
            string N(double v) => MiniJson.Number(v);
            int doneCount = TilesDone;
            double perTile = TilesWritten > 0 ? tileSecondsSum / TilesWritten : double.NaN;
            var unloaded = new StringBuilder();
            foreach (var t in Unloaded) unloaded.Append(unloaded.Length == 0 ? "" : ", ").Append('[').Append(t.X).Append(", ").Append(t.Y).Append(']');
            string current = State == "running" || State == "suspended" ? (index < Tiles.Count ? $"[{Tiles[index].X}, {Tiles[index].Y}]" : "null") : "null";
            string json = "{\n"
                + $"  \"state\": {MiniJson.Quote(State)},\n  \"message\": {(Message == null ? "null" : MiniJson.Quote(Message))},\n"
                + $"  \"output\": {MiniJson.Quote(Request.Output)},\n  \"cell_m\": {N(Request.CellM)},\n"
                + $"  \"box\": [{N(Request.MinN)}, {N(Request.MinE)}, {N(Request.MaxN)}, {N(Request.MaxE)}],\n"
                + $"  \"tiles_total\": {Tiles.Count},\n  \"tiles_done\": {doneCount},\n  \"tiles_written\": {TilesWritten},\n  \"tiles_skipped\": {TilesSkipped},\n"
                + $"  \"unloaded\": [{unloaded}],\n  \"current\": {current},\n"
                + $"  \"rays\": {Rays},\n  \"probe_rays\": {ProbeRays},\n  \"cast_s\": {N(CastSeconds)},\n"
                + $"  \"rays_per_s\": {N(CastSeconds > 0 ? Rays / CastSeconds : double.NaN)},\n"
                + $"  \"seconds_per_tile\": {N(perTile)},\n"
                + $"  \"remaining_s\": {N(perTile * (Tiles.Count - doneCount - Unloaded.Count))},\n"
                + $"  \"elapsed_s\": {N(now - startedS)},\n"
                + $"  \"updated_utc\": {MiniJson.Quote(DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture))}\n"
                + "}\n";
            try { SurveyTile.WriteAtomic(ProgressPath, Encoding.UTF8.GetBytes(json)); }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException) { Log($"survey: cannot write {ProgressPath}: {e.Message}"); }
        }

        /// <summary>Progress for a request that never started (unreadable request): state "error" with the reason.</summary>
        public static void WriteRejected(string progressPath, string why)
        {
            string json = "{\n  \"state\": \"error\",\n  \"message\": " + MiniJson.Quote(why) + ",\n  \"updated_utc\": "
                + MiniJson.Quote(DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture)) + "\n}\n";
            SurveyTile.WriteAtomic(progressPath, Encoding.UTF8.GetBytes(json));
        }
    }
}
