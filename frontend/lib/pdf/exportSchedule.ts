import { jsPDF } from "jspdf";

// Prepared, render-agnostic plan shape so this module stays pure (no DOM / app state).
export interface PdfCourse {
  code: string;
  title?: string | null;
  units?: number | null;
  difficulty?: number | null;
}
export interface PdfQuarter {
  label: string;            // "Fall", "Winter", "Spring", "Summer"
  courses: PdfCourse[];
  units: number;
}
export interface PdfYear {
  label: string;            // "Year 1"
  quarters: PdfQuarter[];
}
export interface SchedulePdfInput {
  majorName: string;
  totalUnits: number;
  years: PdfYear[];
}

// Difficulty → accent color (matches the planner's green/amber/orange/red scale).
function diffColor(score: number): [number, number, number] {
  if (score < 4) return [34, 197, 94];
  if (score < 6) return [202, 138, 4];
  if (score < 8) return [217, 119, 6];
  return [220, 38, 38];
}

function truncate(doc: jsPDF, text: string, maxWidth: number): string {
  if (doc.getTextWidth(text) <= maxWidth) return text;
  let t = text;
  while (t.length > 1 && doc.getTextWidth(t + "…") > maxWidth) t = t.slice(0, -1);
  return t + "…";
}

/**
 * Render a clean single-page (landscape A4) PDF of the schedule grid and trigger
 * a download.  Pure jsPDF drawing — no html2canvas — so it is unaffected by the
 * app's theme/colorspace and produces print-friendly output.
 */
export function exportScheduleToPDF(input: SchedulePdfInput): void {
  const { majorName, totalUnits, years } = input;
  const doc = new jsPDF({ orientation: "landscape", unit: "pt", format: "a4" });
  const pageW = doc.internal.pageSize.getWidth();
  const pageH = doc.internal.pageSize.getHeight();
  const margin = 28;

  // ── Black page background ────────────────────────────────────────────────────
  doc.setFillColor(0, 0, 0);
  doc.rect(0, 0, pageW, pageH, "F");

  // ── Title ──────────────────────────────────────────────────────────────────
  doc.setFont("helvetica", "bold");
  doc.setFontSize(15);
  doc.setTextColor(240, 240, 240);
  doc.text(truncate(doc, majorName || "Degree Plan", pageW - margin * 2 - 120), margin, margin + 4);

  doc.setFont("helvetica", "normal");
  doc.setFontSize(10);
  doc.setTextColor(160, 160, 168);
  doc.text(`${totalUnits} total units`, pageW - margin, margin + 4, { align: "right" });

  // ── Grid geometry ────────────────────────────────────────────────────────────
  const gridTop = margin + 20;
  const gutterW = 26;                 // vertical "Year N" gutter
  const maxQuarters = Math.max(1, ...years.map((y) => y.quarters.length));
  const gridLeft = margin + gutterW;
  const gridW = pageW - gridLeft - margin;
  const colW = gridW / maxQuarters;
  const rows = Math.max(1, years.length);
  const rowH = (pageH - gridTop - margin) / rows;

  const pad = 6;
  const headerH = 15;
  const lineH = 11;

  years.forEach((year, ri) => {
    const rowY = gridTop + ri * rowH;

    // Year gutter
    doc.setFillColor(26, 26, 26);
    doc.rect(margin, rowY, gutterW, rowH, "F");
    doc.setFont("helvetica", "bold");
    doc.setFontSize(7.5);
    doc.setTextColor(160, 160, 168);
    doc.text(year.label.toUpperCase(), margin + gutterW / 2 + 2.5, rowY + rowH / 2, {
      align: "center",
      angle: 90,
    });

    year.quarters.forEach((q, ci) => {
      const x = gridLeft + ci * colW;

      // Cell border + header band
      doc.setDrawColor(60, 60, 60);
      doc.setLineWidth(0.6);
      doc.rect(x, rowY, colW, rowH);
      doc.setFillColor(24, 24, 24);
      doc.rect(x, rowY, colW, headerH, "F");

      // Quarter header: label (left) + units (right)
      doc.setFont("helvetica", "bold");
      doc.setFontSize(8);
      doc.setTextColor(224, 224, 228);
      doc.text(q.label, x + pad, rowY + 10.5);
      doc.setFont("helvetica", "normal");
      doc.setTextColor(170, 170, 178);
      doc.text(`${q.units}u`, x + colW - pad, rowY + 10.5, { align: "right" });

      // Courses
      let cy = rowY + headerH + lineH;
      const maxLines = Math.floor((rowH - headerH - 4) / lineH);
      const shown = q.courses.slice(0, Math.max(0, maxLines));
      shown.forEach((c) => {
        // difficulty dot
        if (c.difficulty != null) {
          const [r, g, b] = diffColor(c.difficulty);
          doc.setFillColor(r, g, b);
          doc.circle(x + pad + 1.5, cy - 2.5, 1.6, "F");
        }
        const textX = x + pad + (c.difficulty != null ? 7 : 0);
        const avail = colW - (textX - x) - pad - 16;
        doc.setFont("helvetica", "bold");
        doc.setFontSize(7.5);
        doc.setTextColor(232, 232, 232);
        doc.text(truncate(doc, c.code, avail), textX, cy);
        if (c.units != null) {
          doc.setFont("helvetica", "normal");
          doc.setFontSize(6.5);
          doc.setTextColor(150, 150, 158);
          doc.text(`${c.units}u`, x + colW - pad, cy, { align: "right" });
        }
        if (c.title) {
          doc.setFont("helvetica", "normal");
          doc.setFontSize(6.5);
          doc.setTextColor(150, 150, 158);
          doc.text(truncate(doc, c.title, colW - pad - (textX - x)), textX, cy + 6.5);
          cy += lineH + 2.5;
        } else {
          cy += lineH;
        }
      });
      if (q.courses.length > shown.length) {
        doc.setFont("helvetica", "italic");
        doc.setFontSize(6.5);
        doc.setTextColor(160, 160, 168);
        doc.text(`+${q.courses.length - shown.length} more`, x + pad, cy);
      }
      if (q.courses.length === 0) {
        doc.setFont("helvetica", "italic");
        doc.setFontSize(7);
        doc.setTextColor(196, 196, 202);
        doc.text("—", x + pad, rowY + headerH + lineH);
      }
    });
  });

  const safe = (majorName || "schedule")
    .replace(/[^a-z0-9]+/gi, "-")
    .toLowerCase()
    .replace(/^-+|-+$/g, "");
  doc.save(`${safe || "schedule"}-plan.pdf`);
}
