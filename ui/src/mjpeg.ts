/**
 * Streaming multipart/x-mixed-replace JPEG reader using Fetch ReadableStream + createImageBitmap.
 * Decodes frames off the main thread for smooth hardware-accelerated <canvas> playback.
 */

export interface MjpegMeta {
  frame_id?: number;
  candidates?: Array<{
    id: number;
    label?: string;
    class_name?: string;
    confidence?: number;
    distance_m?: number;
    bearing_deg?: number;
  }>;
  width?: number;
  height?: number;
}

export interface StreamMjpegOptions {
  onFrame: (bitmap: ImageBitmap, meta: MjpegMeta | null) => void;
  onError?: (err: unknown) => void;
  retryDelayMs?: number;
}

function indexOfBytes(haystack: Uint8Array, needle: Uint8Array, fromIndex = 0): number {
  const limit = haystack.length - needle.length;
  if (limit < fromIndex) return -1;
  const first = needle[0];
  for (let i = fromIndex; i <= limit; i++) {
    if (haystack[i] !== first) continue;
    let match = true;
    for (let j = 1; j < needle.length; j++) {
      if (haystack[i + j] !== needle[j]) {
        match = false;
        break;
      }
    }
    if (match) return i;
  }
  return -1;
}

function appendBytes(a: Uint8Array, b: Uint8Array): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

export function streamMjpeg(url: string, token: string, options: StreamMjpegOptions): () => void {
  const controller = new AbortController();
  const retryDelayMs = options.retryDelayMs ?? 450;
  let stopped = false;

  const boundaryBytes = new TextEncoder().encode("--horusframe");
  const headerSepCrlf = new Uint8Array([13, 10, 13, 10]); // \r\n\r\n
  const headerSepLf = new Uint8Array([10, 10]); // \n\n
  const textDecoder = new TextDecoder();

  const run = async () => {
    while (!stopped) {
      try {
        const resp = await fetch(url, {
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
          signal: controller.signal,
          cache: "no-store",
        });
        if (!resp.ok || !resp.body) {
          throw new Error(`HTTP ${resp.status}`);
        }
        const reader = resp.body.getReader();
        let buf: Uint8Array<ArrayBuffer> = new Uint8Array(0);

        while (!stopped) {
          const { value, done } = await reader.read();
          if (done) break;
          if (!value || value.length === 0) continue;
          buf = appendBytes(buf, value);
          if (buf.length > 8 * 1024 * 1024) {
            buf = new Uint8Array(0);
            continue;
          }

          while (!stopped) {
            const bIdx = indexOfBytes(buf, boundaryBytes, 0);
            if (bIdx === -1) {
              if (buf.length > boundaryBytes.length) {
                buf = buf.slice(buf.length - boundaryBytes.length);
              }
              break;
            }
            if (bIdx > 0) {
              buf = buf.slice(bIdx);
            }

            const afterBoundary = boundaryBytes.length;
            let sepIdx = indexOfBytes(buf, headerSepCrlf, afterBoundary);
            let sepLen = 4;
            if (sepIdx === -1) {
              sepIdx = indexOfBytes(buf, headerSepLf, afterBoundary);
              sepLen = 2;
            }
            if (sepIdx === -1) {
              break;
            }

            const headerText = textDecoder.decode(buf.subarray(afterBoundary, sepIdx));
            let contentLength = -1;
            let meta: MjpegMeta | null = null;
            for (const line of headerText.split(/\r?\n/)) {
              const colon = line.indexOf(":");
              if (colon === -1) continue;
              const k = line.slice(0, colon).trim().toLowerCase();
              const v = line.slice(colon + 1).trim();
              if (k === "content-length") {
                const parsed = Number.parseInt(v, 10);
                if (!Number.isNaN(parsed) && parsed > 0) contentLength = parsed;
              } else if (k === "x-horus-meta") {
                try {
                  meta = JSON.parse(v) as MjpegMeta;
                } catch {
                  // ignore malformed meta
                }
              }
            }

            const bodyStart = sepIdx + sepLen;
            let bodyEnd = -1;
            if (contentLength > 0) {
              if (buf.length < bodyStart + contentLength) {
                break;
              }
              bodyEnd = bodyStart + contentLength;
            } else {
              const nextBoundary = indexOfBytes(buf, boundaryBytes, bodyStart);
              if (nextBoundary === -1) {
                break;
              }
              bodyEnd = nextBoundary;
              if (bodyEnd >= 2 && buf[bodyEnd - 2] === 13 && buf[bodyEnd - 1] === 10) {
                bodyEnd -= 2;
              } else if (bodyEnd >= 1 && buf[bodyEnd - 1] === 10) {
                bodyEnd -= 1;
              }
            }

            const jpegBytes = buf.slice(bodyStart, bodyEnd);
            buf = buf.slice(contentLength > 0 ? bodyEnd : bodyEnd);

            if (jpegBytes.length > 0) {
              const blob = new Blob([jpegBytes], { type: "image/jpeg" });
              try {
                const bitmap = await createImageBitmap(blob);
                if (stopped) {
                  bitmap.close();
                  return;
                }
                options.onFrame(bitmap, meta);
              } catch {
                // skip corrupt frame
              }
            }
          }
        }
      } catch (err) {
        if (stopped) return;
        options.onError?.(err);
      }

      if (!stopped) {
        await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
      }
    }
  };

  void run();
  return () => {
    stopped = true;
    controller.abort();
  };
}
