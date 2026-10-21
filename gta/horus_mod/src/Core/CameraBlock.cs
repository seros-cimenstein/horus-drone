using System;
using System.IO.MemoryMappedFiles;
using System.Threading;

namespace HorusGta
{
    /// <summary>
    /// Writer of the shared-memory block <c>Local\HorusGtaCamera</c> that horus_capture.asi reads at
    /// every Present to stamp frames with the camera state (layout and reader:
    /// gta/common/camera_block.hpp). Updated once per script tick under a seqlock: the sequence is
    /// odd while the fields change and grows by 2 per tick, so the reader also sees when the script
    /// stops ticking (game paused) even though the block still says active.
    /// </summary>
    public sealed class CameraBlock : IDisposable
    {
        public const string Name = @"Local\HorusGtaCamera";
        public const int Size = 64;
        public const uint Magic = 0x4D434748;   // "HGCM"
        public const uint Version = 1;
        public const int MagicOffset = 0, VersionOffset = 4, SequenceOffset = 8, ActiveOffset = 16,
            PoseSeqOffset = 24, GameFrameOffset = 32;

        readonly MemoryMappedFile file;
        readonly MemoryMappedViewAccessor view;

        /// <summary>
        /// Use <paramref name="file"/> (at least <see cref="Size"/> bytes; in the game
        /// <c>MemoryMappedFile.CreateOrOpen(Name, Size)</c>); takes ownership.
        /// </summary>
        public CameraBlock(MemoryMappedFile file)
        {
            this.file = file;
            view = file.CreateViewAccessor(0, Size);
            view.Write(MagicOffset, Magic);
            view.Write(VersionOffset, Version);
        }

        /// <summary>Times the block was written (sequence / 2).</summary>
        public ulong Ticks => view.ReadUInt64(SequenceOffset) / 2;

        /// <summary>One tick's state: whether the drone camera is on screen, the pose packet it shows, the game frame.</summary>
        public void Publish(bool active, ulong poseSeq, ulong gameFrame)
        {
            ulong sequence = view.ReadUInt64(SequenceOffset) | 1;   // odd: writing
            view.Write(SequenceOffset, sequence);
            Thread.MemoryBarrier();
            view.Write(ActiveOffset, active ? 1u : 0u);
            view.Write(PoseSeqOffset, poseSeq);
            view.Write(GameFrameOffset, gameFrame);
            Thread.MemoryBarrier();
            view.Write(SequenceOffset, sequence + 1);               // even: done
        }

        public void Dispose()
        {
            view.Dispose();
            file.Dispose();
        }
    }
}
