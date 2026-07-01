// Shared Supabase clients for Headroom Desktop edge functions.
// SUPABASE_URL / SUPABASE_ANON_KEY / SUPABASE_SERVICE_ROLE_KEY are injected
// automatically by the Supabase edge runtime.

import {
  createClient,
  type SupabaseClient,
  type User,
} from "npm:@supabase/supabase-js@2";

const supabaseUrl = Deno.env.get("SUPABASE_URL") ?? "";
const anonKey = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

/**
 * Service-role client: bypasses RLS. Use ONLY for server-side writes
 * (subscriptions mirror). Never expose results to a caller without
 * scoping them to that caller's user id.
 */
export function createAdminClient(): SupabaseClient {
  return createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}

/**
 * User-scoped client: forwards the caller's Authorization header so RLS
 * applies exactly as it would from the desktop app.
 */
export function createUserClient(req: Request): SupabaseClient {
  return createClient(supabaseUrl, anonKey, {
    global: { headers: { Authorization: req.headers.get("Authorization") ?? "" } },
    auth: { persistSession: false, autoRefreshToken: false },
  });
}

/** Resolve and verify the caller from the request JWT. Returns null if unauthenticated. */
export async function getUser(req: Request): Promise<User | null> {
  const authHeader = req.headers.get("Authorization");
  if (!authHeader?.startsWith("Bearer ")) return null;
  const client = createUserClient(req);
  const { data, error } = await client.auth.getUser();
  if (error || !data?.user) return null;
  return data.user;
}
