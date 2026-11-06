// Quad flight physics for ArduPilot's JSON SITL backend.
//
// Adapted from Los Santos FPV by zeitiv (https://github.com/zeitiv/LosSantosFPV, commit
// 15800d0d9a34954121797a11fba881edaa9f1dc0, src/Flight.cs and src/Tune.cs): the motor/prop thrust
// and max-rpm math, the rigid-body step (body axes, gyroscopic term, angular drag, rate limit) and
// the aerodynamic drag. Changed for HORUS: ArduPilot is the flight controller, so the stick input,
// rate loop and airmode mixer are gone; each motor follows its PWM through a first-order lag and
// pushes at its own arm position (ArduPilot quad X layout); yaw comes from prop drag torque; the
// state is double precision and steps at ArduPilot's frame rate.
//
// MIT License
//
// Copyright (c) 2026 zeitiv
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.

using System;

namespace HorusGta
{
    /// <summary>World or body vector, metres (GTA world: X east, Y north, Z up).</summary>
    public struct Vec3
    {
        public double X, Y, Z;
        public Vec3(double x, double y, double z) { X = x; Y = y; Z = z; }
        public static Vec3 operator +(Vec3 a, Vec3 b) => new Vec3(a.X + b.X, a.Y + b.Y, a.Z + b.Z);
        public static Vec3 operator -(Vec3 a, Vec3 b) => new Vec3(a.X - b.X, a.Y - b.Y, a.Z - b.Z);
        public static Vec3 operator -(Vec3 a) => new Vec3(-a.X, -a.Y, -a.Z);
        public static Vec3 operator *(Vec3 a, double s) => new Vec3(a.X * s, a.Y * s, a.Z * s);
        public static double Dot(Vec3 a, Vec3 b) => a.X * b.X + a.Y * b.Y + a.Z * b.Z;
        public static Vec3 Cross(Vec3 a, Vec3 b) => new Vec3(a.Y * b.Z - a.Z * b.Y, a.Z * b.X - a.X * b.Z, a.X * b.Y - a.Y * b.X);
        public double Length => Math.Sqrt(X * X + Y * Y + Z * Z);
        public Vec3 Normalized { get { double l = Length; return l < 1e-12 ? new Vec3(0, 0, 1) : this * (1.0 / l); } }

        /// <summary>Rotate v about unit axis k by angle a (Rodrigues).</summary>
        public static Vec3 Rotate(Vec3 v, Vec3 k, double a)
        {
            double c = Math.Cos(a), s = Math.Sin(a);
            return v * c + Cross(k, v) * s + k * (Dot(k, v) * (1 - c));
        }
    }

    /// <summary>The simulated quad's build. Defaults: an F450-class quad (1.5 kg, 10x4.5 two-blade props, 920 Kv on 4S).</summary>
    public sealed class Airframe
    {
        public double MassKg = 1.5;
        public double PropDiameterIn = 10, PropPitchIn = 4.5;
        public int PropBlades = 2;
        public double MotorKv = 920;
        public int BatteryCells = 4;
        /// <summary>First-order lag from PWM to rpm, seconds.</summary>
        public double MotorTimeConstantS = 0.04;
        /// <summary>Prop drag torque per newton of thrust (m): yaw authority.</summary>
        public double YawTorquePerThrustM = 0.016;
        /// <summary>Radius of the body's collision sphere (m).</summary>
        public double RadiusM = 0.12;
        /// <summary>Height of the resting body centre above the ground (HORUS up 0): one radius plus 1 mm.</summary>
        public double RestHeightM => RadiusM + 0.001;
        /// <summary>
        /// HORUS camera mount point in body axes (right, forward, up), m: Los Santos FPV's camera
        /// point on <c>ch_prop_casino_drone_02a</c>, at the nose above the body centre.
        /// </summary>
        public Vec3 CameraMountM = new Vec3(0, 0.07, 0.03);

        /// <summary>Motor centre offset along each body axis (m); the diagonal arm is this times √2.</summary>
        public double HubOffsetM => 0.0156 * PropDiameterIn;

        /// <summary>Full-throttle rpm from the motor, battery and prop load (Los Santos FPV Tune.DeriveMaxRpm).</summary>
        public double MaxRpm
        {
            get
            {
                double D = PropDiameterIn, P = PropPitchIn, B = PropBlades;
                double K = 5.74345e-15 * Math.Pow(D, 3.39007) * Math.Pow(P, 1.38378) * Math.Pow(B, 0.654997) * Math.Pow(MotorKv, 3);
                double io = 0.45, rm = 0.1, n = BatteryCells, v = n * 3.7, rc = Math.Max(120.0 / Math.Pow(2, n), 5) / 1000.0;
                double r = n * rc + rm;
                double a = K * r * r, b = -(2 * K * v * r + 1), c = K * v * v + io;
                double disc = Math.Max(b * b - 4 * a * c, 0);
                double i = a > 1e-12 ? (-b - Math.Sqrt(disc)) / (2 * a) : c / -b;
                double vm = v - n * rc * i - i * rm;
                double rpm = Math.Max(vm, 1) * MotorKv;
                double tipCap = 340.0 / (Math.PI * PropDiameterIn * 0.0254) * 60;
                return Math.Min(rpm, tipCap);
            }
        }

        /// <summary>One prop's thrust (N) at <paramref name="rpm"/>, falling off with axial inflow speed (Los Santos FPV Tune.Thrust).</summary>
        public double Thrust(double rpm, double axialSpeed)
        {
            if (rpm < 5) return 0;
            double stat = 7.47958e-5 * Math.Pow(PropBlades, 4.70298) + 2.525e-11 * PropPitchIn * PropDiameterIn * PropDiameterIn * PropDiameterIn * rpm * rpm;
            double pitchSpeed = rpm * PropPitchIn * 0.0254 / 60;
            return stat * Math.Max(0, 1 - Math.Max(axialSpeed, 0) / (1.1 * pitchSpeed));
        }

        /// <summary>Per-motor throttle that hovers (static, no inflow).</summary>
        public double HoverThrottle()
        {
            double lo = 0, hi = 1, max = MaxRpm;
            for (int i = 0; i < 50; i++)
            {
                double t = (lo + hi) / 2;
                if (4 * Thrust(t * max, 0) > MassKg * QuadBody.G) hi = t; else lo = t;
            }
            return (lo + hi) / 2;
        }
    }

    /// <summary>Contact found by the host's world test: point on the surface, its normal and what was hit.</summary>
    public struct Contact
    {
        public Vec3 Position, Normal;
        /// <summary>Host handle of the entity hit; 0 for world collision (map, ground).</summary>
        public int Entity;
        /// <summary>
        /// True when the hit lies on the step's own path (the motion ray and its reach): the body
        /// stops before it. False for a hit found by a separate ray (the ground probe).
        /// </summary>
        public bool OnPath;
    }

    /// <summary>
    /// Rigid quad in the GTA world (X east, Y north, Z up). Body axes R (right), F (nose), U (up);
    /// body rates W = (about R: nose up, about F: roll right, about U: yaw left), rad/s.
    /// Motors follow ArduPilot's quad X order: 1 front right (CCW prop), 2 back left (CCW),
    /// 3 front left (CW), 4 back right (CW).
    /// </summary>
    public sealed class QuadBody
    {
        public const double G = 9.80665;
        // Motor positions as (right, forward) signs and yaw factors (+1 = CCW prop: yaws the body right).
        static readonly double[] MotorRight = { 1, -1, -1, 1 }, MotorForward = { 1, -1, 1, -1 }, MotorYaw = { 1, 1, -1, -1 };

        public readonly Airframe Frame;
        public Vec3 P, V, R = new Vec3(1, 0, 0), F = new Vec3(0, 1, 0), U = new Vec3(0, 0, 1), W;
        /// <summary>Specific force of the last step (acceleration minus gravity), world frame: what an accelerometer reads.</summary>
        public Vec3 SpecificForce = new Vec3(0, 0, G);
        public readonly double[] Rpm = new double[4];
        /// <summary>Height of flat ground under the body (m), used when <see cref="Sweep"/> is not set.</summary>
        public double FloorZ;
        /// <summary>
        /// Host world test: a point on the first surface met moving from a to b (looking the given
        /// reach past b), or under b within twice the reach, or null. The reach is the body radius
        /// plus <see cref="ContactSkinM"/>. Null host: flat ground at <see cref="FloorZ"/>. See <see cref="WorldProbe"/>.
        /// </summary>
        public Func<Vec3, Vec3, double, Contact?> Sweep;

        /// <summary>Called on every step that pushes into a surface, with the speed into it (m/s).</summary>
        public Action<Contact, double> Contacted;
        /// <summary>True when the last step ended resting on or pressed against a surface.</summary>
        public bool InContact { get; private set; }
        /// <summary>Contact of the last step, if any (world).</summary>
        public Contact? LastContact { get; private set; }

        readonly double ix, iy, iz, maxRpm;

        public QuadBody(Airframe frame)
        {
            Frame = frame;
            double w = 2 * frame.HubOffsetM * 0.96, h = 0.03;
            ix = frame.MassKg / 12 * (w * w + h * h);
            iy = ix;
            iz = frame.MassKg / 12 * (2 * w * w);
            maxRpm = frame.MaxRpm;
        }

        /// <summary>Put the body level at <paramref name="position"/> facing GTA heading <paramref name="headingRad"/> (counter-clockwise from +Y), at rest, motors stopped.</summary>
        public void Place(Vec3 position, double headingRad)
        {
            P = position; V = default; W = default;
            F = new Vec3(-Math.Sin(headingRad), Math.Cos(headingRad), 0);
            U = new Vec3(0, 0, 1);
            R = Vec3.Cross(F, U);
            SpecificForce = new Vec3(0, 0, G);
            Array.Clear(Rpm, 0, Rpm.Length);
            InContact = false;
            LastContact = null;
        }

        /// <summary>World position of a point fixed to the body at (right, forward, up) metres from its centre.</summary>
        public Vec3 ToWorld(Vec3 local) => P + R * local.X + F * local.Y + U * local.Z;

        /// <summary>
        /// Unit quaternion (x, y, z, w) turning GTA entity axes (+X right, +Y forward, +Z up) into the
        /// body axes, for SET_ENTITY_QUATERNION (Los Santos FPV DroneMod.FromBasis).
        /// </summary>
        public void Orientation(out double x, out double y, out double z, out double w)
        {
            double m00 = R.X, m01 = F.X, m02 = U.X, m10 = R.Y, m11 = F.Y, m12 = U.Y, m20 = R.Z, m21 = F.Z, m22 = U.Z;
            double tr = m00 + m11 + m22;
            if (tr > 0) { double s = Math.Sqrt(tr + 1) * 2; w = 0.25 * s; x = (m21 - m12) / s; y = (m02 - m20) / s; z = (m10 - m01) / s; }
            else if (m00 > m11 && m00 > m22) { double s = Math.Sqrt(1 + m00 - m11 - m22) * 2; w = (m21 - m12) / s; x = 0.25 * s; y = (m01 + m10) / s; z = (m02 + m20) / s; }
            else if (m11 > m22) { double s = Math.Sqrt(1 + m11 - m00 - m22) * 2; w = (m02 - m20) / s; x = (m01 + m10) / s; y = 0.25 * s; z = (m12 + m21) / s; }
            else { double s = Math.Sqrt(1 + m22 - m00 - m11) * 2; w = (m10 - m01) / s; x = (m02 + m20) / s; y = (m12 + m21) / s; z = 0.25 * s; }
        }

        /// <summary>Advance by <paramref name="h"/> seconds with per-motor throttle 0..1 (ArduPilot motor order).</summary>
        public void Step(double h, double[] throttle)
        {
            Vec3 v0 = V;
            double lag = Math.Min(1, h / Math.Max(Frame.MotorTimeConstantS, 1e-6));
            double axial = Vec3.Dot(V, U);
            double total = 0, tauR = 0, tauF = 0, tauD = 0, arm = Frame.HubOffsetM;
            for (int i = 0; i < 4; i++)
            {
                double t = i < throttle.Length ? Math.Max(0, Math.Min(1, throttle[i])) : 0;
                Rpm[i] += (t * maxRpm - Rpm[i]) * lag;
                double f = Frame.Thrust(Rpm[i], axial);
                total += f;
                // r x (0,0,f) for r = (right, forward, 0): about R = forward*f (nose up), about F = -right*f (roll right).
                tauR += MotorForward[i] * arm * f;
                tauF += -MotorRight[i] * arm * f;
                tauD += MotorYaw[i] * Frame.YawTorquePerThrustM * f;
            }
            var tau = new Vec3(tauR, tauF, -tauD);   // about U = -(about D)

            Vec3 force = new Vec3(0, 0, -G * Frame.MassKg) + U * total + Aero(V);

            // Rotation in body axes with the gyroscopic term, angular drag and a rate limit.
            var iw = new Vec3(ix * W.X, iy * W.Y, iz * W.Z);
            var gyro = Vec3.Cross(W, iw);
            double drag = 1 - 1.6 * h;
            W = new Vec3((W.X + (tau.X - gyro.X) / ix * h) * drag, (W.Y + (tau.Y - gyro.Y) / iy * h) * drag, (W.Z + (tau.Z - gyro.Z) / iz * h) * drag);
            double wl = W.Length;
            if (wl > 36) W = W * (36 / wl);

            V += force * (h / Frame.MassKg);
            Vec3 from = P, to = P + V * h;
            Vec3 wWorld = R * W.X + F * W.Y + U * W.Z;
            double ang = wWorld.Length * h;
            if (ang > 1e-12) { var k = wWorld.Normalized; F = Vec3.Rotate(F, k, ang); U = Vec3.Rotate(U, k, ang); }
            F = F.Normalized; U = (U - F * Vec3.Dot(U, F)).Normalized; R = Vec3.Cross(F, U);

            P = Collide(from, to);
            SpecificForce = (V - v0) * (1 / h) + new Vec3(0, 0, G);
        }

        /// <summary>
        /// Move the centre from <paramref name="from"/> towards <paramref name="to"/>, stopping where it
        /// would come closer to the surface found than one radius plus <see cref="RestGapM"/> (or than
        /// it already is), and constrain the velocity when touching. The centre only moves along the
        /// step's own segment, which the world test found clear, so it never crosses a surface. Until
        /// 2026-11-02 it was put one radius off the hit point along the surface normal instead: that
        /// moved it sideways, a landed quad walked down slopes, and on a Vinewood hillside one walked
        /// through a rock face into the ground and fell through the map.
        /// </summary>
        Vec3 Collide(Vec3 from, Vec3 to)
        {
            double r = Frame.RadiusM;
            Contact? hit = Sweep != null ? Sweep(from, to, r + ContactSkinM) : FloorContact(to, r);
            InContact = false;
            LastContact = null;
            if (!hit.HasValue) return to;
            var c = hit.Value;
            var n = c.Normal.Normalized;
            // Distances from the surface's plane at the start and end of the step; along the segment they change linearly.
            double sFrom = Vec3.Dot(from - c.Position, n), sTo = Vec3.Dot(to - c.Position, n);
            double keep = Math.Min(sFrom, r + RestGapM);
            double t = sTo >= keep ? 1 : Math.Max(0, (sFrom - keep) / (sFrom - sTo));
            bool blocked = false;
            if (c.OnPath)
            {
                // Never past a hit on the path, whatever its normal says: stop 1 mm before it.
                Vec3 d = to - from;
                double len = d.Length;
                double before = len > 1e-12 ? (Vec3.Dot(c.Position - from, d) / len - 0.001) / len : 0;
                if (before < t) { t = Math.Max(0, before); blocked = true; }
            }
            Vec3 p = from + (to - from) * t;
            double vn = Vec3.Dot(V, n);
            bool touching = blocked || sFrom + (sTo - sFrom) * t <= r + ContactSkinM;
            // Not touching, or moving away from the surface (taking off): no constraint.
            if (!touching || vn >= 0) return p;
            InContact = true;
            LastContact = c;
            Contacted?.Invoke(c, -vn);
            V = V - n * vn;                    // no bounce: the frame and props absorb the impact
            V = V * (n.Z > 0.7 ? 0.0 : 0.3);   // resting on the ground does not slide; walls keep 30 %
            W = W * 0.5;
            return p;
        }

        /// <summary>Surfaces closer than this count as touching (m); larger than <see cref="RestGapM"/>.</summary>
        public const double ContactSkinM = 0.002;
        /// <summary>Gap the collision step leaves between the sphere and a surface it rests on (m).</summary>
        public const double RestGapM = 0.001;

        Contact? FloorContact(Vec3 to, double r)
        {
            // The skin keeps a resting body in contact instead of hopping a millimetre each step.
            if (to.Z - r > FloorZ + ContactSkinM) return null;
            return new Contact { Position = new Vec3(to.X, to.Y, FloorZ), Normal = new Vec3(0, 0, 1) };
        }

        /// <summary>Body and rotor drag (Los Santos FPV Flight.Aero), world frame.</summary>
        Vec3 Aero(Vec3 v)
        {
            double speed = v.Length;
            if (speed < 0.01) return default;
            Vec3 vh = v * (1 / speed);
            double area = Math.Abs(Vec3.Dot(vh, U)) * 0.040 + Math.Abs(Vec3.Dot(vh, R)) * 0.013 + Math.Abs(Vec3.Dot(vh, F)) * 0.012;
            area *= (Frame.PropDiameterIn / 5) * (Frame.PropDiameterIn / 5);
            Vec3 fBody = vh * -(0.5 * 1.184 * area * speed * speed);
            double dm = Frame.PropDiameterIn * 0.0254;
            double cosA = Math.Max(-1, Math.Min(1, Vec3.Dot(-vh, U)));
            double vPerp = speed * Math.Sqrt(Math.Max(1 - cosA * cosA, 0));
            double meanRpm = (Rpm[0] + Rpm[1] + Rpm[2] + Rpm[3]) / 4;
            double j = Math.Max(0, Math.Min(1, speed / Math.Max(meanRpm / 60 * dm, 0.001)));
            double bladeF = (Frame.PropBlades - 3) * 0.2 + 3;
            Vec3 fRotor = vh * -(0.5 * 1.225 * (dm / 2 * bladeF) * vPerp * vPerp * (0.024 + (0.008 - 0.024) * j) * 4);
            return fBody + fRotor;
        }
    }

    /// <summary>
    /// <see cref="QuadBody.Sweep"/> from a synchronous ray test, the only synchronous world test GTA
    /// has. Each step casts one ray along the motion, extended by the reach. Ground support needs a
    /// ray down too, but casting one every step doubles the cost (each GTA ray costs ~0.25 ms in
    /// game). So a long ray down (<see cref="GroundProbeDepthM"/>) caches the ground height under
    /// the body, refreshed every <see cref="ReprobeSteps"/> steps or <see cref="ReprobeDistanceM"/>
    /// of horizontal travel, and the short down ray runs only within <see cref="NearGroundM"/> of
    /// that height. The short ray is <see cref="DownReachFactor"/> reaches long: on a slope the
    /// sphere touches above the ground point straight under it, at 60° twice the reach above it.
    /// Grazing contacts beside the path are not seen.
    /// </summary>
    public sealed class WorldProbe
    {
        public const double GroundProbeDepthM = 50, ReprobeDistanceM = 0.5, NearGroundM = 1.0, DownReachFactor = 2;
        public const int ReprobeSteps = 100;

        readonly Func<Vec3, Vec3, Contact?> ray;
        double groundZ = double.NegativeInfinity;
        Vec3 probedAt;
        int stepsSinceProbe = int.MaxValue - 1;

        public WorldProbe(Func<Vec3, Vec3, Contact?> ray) { this.ray = ray; }

        /// <summary>Rays cast so far.</summary>
        public long Rays { get; private set; }

        Contact? Ray(Vec3 a, Vec3 b) { Rays++; return ray(a, b); }

        public Contact? Sweep(Vec3 from, Vec3 to, double reach)
        {
            Vec3 d = to - from;
            double len = d.Length;
            if (len > 1e-9)
            {
                var hit = Ray(from, to + d * (reach / len));
                if (hit.HasValue)
                {
                    var onPath = hit.Value;
                    onPath.OnPath = true;
                    return onPath;
                }
            }
            double down = reach * DownReachFactor;
            double dx = to.X - probedAt.X, dy = to.Y - probedAt.Y;
            if (++stepsSinceProbe >= ReprobeSteps || dx * dx + dy * dy > ReprobeDistanceM * ReprobeDistanceM)
            {
                var ground = Ray(to, to + new Vec3(0, 0, -GroundProbeDepthM));
                groundZ = ground.HasValue ? ground.Value.Position.Z : double.NegativeInfinity;
                probedAt = to;
                stepsSinceProbe = 0;
                return ground.HasValue && to.Z - ground.Value.Position.Z <= down ? ground : null;
            }
            if (to.Z - down > groundZ + NearGroundM) return null;
            return Ray(to, to + new Vec3(0, 0, -down));
        }
    }
}
