using System;

namespace HorusGta
{
    /// <summary>
    /// Receive-side state, independent of sockets and the game: the newest valid packet by
    /// sequence, when it arrived, and how many datagrams were rejected.
    /// </summary>
    public sealed class PoseLink
    {
        readonly SeqTracker seq = new SeqTracker();
        readonly double timeoutS;
        double lastValidS = double.NegativeInfinity;
        double lastDrainS = double.NaN;
        bool discarding;

        public PoseLink(double timeoutS) { this.timeoutS = timeoutS; }

        public PosePacket Latest { get; private set; }
        public bool HasLatest { get; private set; }
        public long BadPackets { get; private set; }
        public PacketError LastError { get; private set; }

        /// <summary>Parse one datagram received at <paramref name="nowS"/> (any monotonic seconds).</summary>
        public PacketError Offer(byte[] data, int length, double nowS)
        {
            var error = PoseProtocol.TryParse(data, length, out var packet);
            if (error != PacketError.None)
            {
                BadPackets++;
                LastError = error;
                return error;
            }
            lastValidS = nowS;
            if (seq.Accept(packet.Seq))
            {
                Latest = packet;
                HasLatest = true;
            }
            return PacketError.None;
        }

        /// <summary>
        /// Read every queued datagram, oldest first, so the newest pose wins.
        /// <paramref name="receiveOne"/> fills the buffer and returns its length, or -1 when
        /// nothing is queued; <paramref name="overBudget"/> is checked after each datagram and
        /// ends the drain early (the rest stays queued for the next tick).
        /// <para>
        /// If the previous drain was more than timeout_s ago (the game was paused, so the script
        /// did not tick), everything queued arrived during that gap. A full socket also drops the
        /// newest datagrams, not the oldest, so the backlog holds poses from the start of the pause.
        /// It is read and discarded (<see cref="DrainResult.Discarded"/>, possibly over several
        /// budget-limited drains), and the link is then reset, so the first pose applied after the
        /// gap is one sent after it.
        /// </para>
        /// </summary>
        public DrainResult Drain(byte[] buffer, Func<byte[], int> receiveOne, double nowS, Func<bool> overBudget)
        {
            if (!double.IsNaN(lastDrainS) && nowS - lastDrainS > timeoutS) discarding = true;
            lastDrainS = nowS;
            var result = new DrainResult();
            for (;;)
            {
                int length = receiveOne(buffer);
                if (length < 0)
                {
                    if (discarding)
                    {
                        discarding = false;
                        Reset();
                    }
                    return result;
                }
                if (discarding) result.Discarded++;
                else Offer(buffer, length, nowS);
                result.Datagrams++;
                if (overBudget())
                {
                    result.BudgetHit = true;
                    return result;
                }
            }
        }

        /// <summary>A valid packet arrived within timeout_s.</summary>
        public bool IsFresh(double nowS) => HasLatest && nowS - lastValidS <= timeoutS;

        /// <summary>Forget the stream after a timeout, so a restarted bridge starts over at any seq.</summary>
        public void Reset()
        {
            seq.Reset();
            HasLatest = false;
            Latest = default;
            lastValidS = double.NegativeInfinity;
        }
    }

    /// <summary>
    /// One <see cref="PoseLink.Drain"/>: datagrams read, how many of them were discarded as queued
    /// during a tick gap, and whether the time budget ended it.
    /// </summary>
    public struct DrainResult
    {
        public int Datagrams;
        public int Discarded;
        public bool BudgetHit;
    }
}
