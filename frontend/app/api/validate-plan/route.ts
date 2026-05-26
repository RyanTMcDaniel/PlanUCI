import { NextResponse } from "next/server";

const BACKEND = process.env.OPTIMIZER_BACKEND_URL ?? "http://localhost:8001";
const OFFLINE = { valid: true, conflicts: [], online: false };

export async function POST(req: Request) {
  try {
    const body = await req.json();
    const res = await fetch(`${BACKEND}/optimizer/validate_locks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(3000),
    });
    if (!res.ok) return NextResponse.json({ valid: true, conflicts: [], online: true });
    const data = await res.json();
    return NextResponse.json({ ...data, online: true });
  } catch {
    return NextResponse.json(OFFLINE);
  }
}
