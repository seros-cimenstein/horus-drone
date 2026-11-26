import qrcode from "qrcode-generator";

/** Build a boolean QR matrix (true = dark module) using zero DOM innerHTML. */
export function buildQrMatrix(text: string): boolean[][] {
  const qr = qrcode(0, "M");
  qr.addData(text);
  qr.make();
  const count = qr.getModuleCount();
  const rows: boolean[][] = [];
  for (let r = 0; r < count; r += 1) {
    const row: boolean[] = [];
    for (let c = 0; c < count; c += 1) {
      row.push(qr.isDark(r, c));
    }
    rows.push(row);
  }
  return rows;
}
