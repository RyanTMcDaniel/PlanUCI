import { NextResponse } from "next/server";

const BACKEND = process.env.OPTIMIZER_BACKEND_URL ?? "http://localhost:8001";

export async function POST(req: Request) {
  try {
    const body = await req.json();
    const res = await fetch(`${BACKEND}/optimizer/whatif`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(90_000),
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Optimizer unavailable";
    return NextResponse.json({ error: msg }, { status: 503 });
  }
}
