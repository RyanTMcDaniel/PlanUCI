import { createServerClient } from "@supabase/ssr";
import { NextResponse } from "next/server";

// app_stats and auth.users are only reachable with the service key.
function serviceClient() {
  return createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_KEY!,
    { cookies: { getAll: () => [], setAll: () => {} } },
  );
}

// { schedules_saved, total_users } — GET route handlers are uncached by default
// in Next 16, so these numbers are always fresh.
export async function GET() {
  const supabase = serviceClient();

  const [statRes, usersRes] = await Promise.all([
    supabase.from("app_stats").select("value").eq("key", "schedules_saved").maybeSingle(),
    supabase.rpc("get_total_users"),
  ]);

  return NextResponse.json({
    schedules_saved: Number(statRes.data?.value ?? 0),
    total_users: Number(usersRes.data ?? 0),
  });
}
